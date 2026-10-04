import shutil
import time
import difflib
import re
from pathlib import Path
from typing import List, Tuple, Optional
from vynacode.backend.schema import FreeResponse

class AnchorError(Exception):
    """Base class for all anchor-related errors."""
    pass

class Ambiguous(AnchorError):
    """Raised when an anchor matches 2+ lines and no occurrence was chosen."""
    def __init__(self, candidates: List[Tuple[int, str]]):
        self.candidates = candidates
        super().__init__(f"Ambiguous match: {len(candidates)} candidates found.")

def _normalise(s: str) -> str:
    s = s.strip()
    return " ".join(s.split())

def _matches(line: str, needle: str) -> bool:
    """Substring test on the normalised line; find() == -1 is the only miss."""
    return needle in _normalise(line)

_GUTTER = re.compile(r"(?m)^[ \t]*\d+[ \t]*\|[ \t]?")


def _strip_gutter(text: str) -> str:
    """Drops the prompt's '   9 | ' line-number prefix when the model copies it."""
    return _GUTTER.sub("", text)


def _check_containment(root: Path, target: Path):
    """Rejects any resolved path outside the index root."""
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        raise PermissionError(f"Path {target} is outside the root {root}")

def _snapshot(root: Path, file_path: Path):
    """Byte-copies the target file to .vc/undo/<relpath>.<timestamp>.bak before any write."""
    undo_dir = root / ".vc" / "undo"
    undo_dir.mkdir(parents=True, exist_ok=True)
    
    rel_path = file_path.relative_to(root)
    timestamp = int(time.time())
    snapshot_path = undo_dir / f"{rel_path.name}.{timestamp}.bak"
    
    if file_path.exists():
        shutil.copy2(file_path, snapshot_path)
    return snapshot_path

def render_diff(old_lines: List[str], new_lines: List[str]) -> str:
    """Unified diff. Input lines may or may not carry their own newline."""
    def _keepends(lines: List[str]) -> List[str]:
        return [line if line.endswith("\n") else line + "\n" for line in lines]
    return "".join(difflib.unified_diff(_keepends(old_lines), _keepends(new_lines), lineterm="\n"))

def resolve_anchor(lines: List[str], needle: str, occurrence: int = 1) -> int:
    """1-based line number of the needle's occurrence-th match. Raises otherwise."""
    needle = _normalise(_strip_gutter(needle))
    if needle == "":
        raise AnchorError("String to search is empty")

    candidates = [
        (index + 1, line.strip())
        for index, line in enumerate(lines)
        if _matches(line, needle)
    ]

    if not candidates:
        raise AnchorError(f"Anchor not found: {needle!r}")
    if not 1 <= occurrence <= len(candidates):
        if len(candidates) == 1:
            raise AnchorError(
                f"Anchor {needle!r} matches once (line {candidates[0][0]}), "
                f"occurrence {occurrence} is out of range"
            )
        raise Ambiguous(candidates)
    return candidates[occurrence - 1][0]

def resolve_span(
    lines: List[str], 
    anchor: str, 
    end_anchor: Optional[str] = None,
    occurrence: int = 1,
) -> Tuple[int, int]:
    """1-based inclusive (start, end). end_anchor is searched from the anchor down."""
    anchor = _normalise(_strip_gutter(anchor))
    if anchor == "":
        raise AnchorError("String to search is empty")

    indexes = [
        index for index, line in enumerate(lines)
        if _matches(line, anchor)
    ]

    if not indexes:
        raise AnchorError(f"Anchor not found: {anchor!r}")
    if not 1 <= occurrence <= len(indexes):
        if len(indexes) == 1:
            raise AnchorError(
                f"Anchor {anchor!r} matches once (line {indexes[0] + 1}), "
                f"occurrence {occurrence} is out of range"
            )
        raise Ambiguous([(index + 1, lines[index].strip()) for index in indexes])

    start = indexes[occurrence - 1]

    if end_anchor is None:
        return (start + 1, start + 1)

    end_anchor = _normalise(_strip_gutter(end_anchor))
    if end_anchor == "":
        raise AnchorError("End anchor to search is empty")

    for index in range(start, len(lines)):
        if _matches(lines[index], end_anchor):
            return (start + 1, index + 1)
    raise AnchorError(f"End anchor not found: {end_anchor!r}")

def apply_edit(lines: List[str], start: int, end: int, new_text: str) -> List[str]:
    """Replaces the 1-based inclusive range [start, end] with new_text."""
    new_text = _strip_gutter(new_text)
    if new_text.endswith("\n"):
        new_text = new_text[:-1]

    new_lines = new_text.split("\n") if new_text else []
    result = list(lines)
    result[start - 1 : end] = new_lines
    return result


def apply_action(root: Path, response: FreeResponse) -> str:

    if response.edit:
        action = response.edit
        target_path = root / action.file_path
        _check_containment(root, target_path)
        
        with open(target_path, "r", encoding="utf-8") as f:
            old_lines = f.readlines()

        start, end = resolve_span(old_lines, action.anchor, action.end_anchor, action.occurrence)
        new_lines = apply_edit(old_lines, start, end, action.new_text)
        
        _snapshot(root, target_path)
        with open(target_path, "wb") as f:
            f.write(take_snapshot(new_lines))

        return render_diff(old_lines, new_lines)
    elif response.write:
        action = response.write
        target_path = root / action.file_path
        _check_containment(root, target_path)
        # write means "new file"; edit means "change this". Without this a bad
        # write replaces a real file, which the undo snapshot does not undo for you.
        if target_path.exists():
            raise AnchorError(f"{action.file_path} already exists; use edit to change it")
        
        _snapshot(root, target_path)
        
        content = _strip_gutter(action.content)
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(content)
            
        return render_diff([], content.splitlines(keepends=True))
    
    return ""

def take_snapshot(lines: List[str]) -> bytes:
    content = "\n".join(line.rstrip("\n") for line in lines)
    if lines:
        content += "\n"
    return content.encode('utf-8')