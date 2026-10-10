# SPDX-License-Identifier: AGPL-3.0-only
import asyncio
import json
import re #for regex
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "backend"))
sys.path.insert(0, str(_ROOT))
# editor.py and the schema models it needs import via the package path, so the
# repo root (the parent of the vynacode package) has to be importable too.
sys.path.insert(0, str(_ROOT.parent))

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.text import Text
from pydantic import BaseModel, ValidationError

from database import (
    init_db, upsert_file_metadata, upsert_block, delete_blocks_for_file,
    write_codebase_json, search_blocks, search_code, get_file_hashes,
    get_db_paths, index_stats, prune_deleted_files, ensure_indexed, resolve_all_dependencies,
    get_unsummarised_files,
)
from walker import walk, compute_sha256
from parser import parse_file, SUPPORTED_SUFFIXES
from client import OllamaClient, own_terms
from summarizer import summarize_and_store
from schema import DoResponse, FileMetaData, PlanResponse
from vynacode.backend.editor import AnchorError, apply_action, resolve_span
from vynacode.backend.schema import FreeResponse

from vynacode.config import (
    CODER_MODEL, PLANNER_MODEL, OLLAMA_URL, TOKEN_BUDGET, TIER,
    THINK_LEVELS, config_path, current_settings, is_overridden, save_config,
    think_for,
)

app = typer.Typer()
console = Console()
llm = OllamaClient(base_url=OLLAMA_URL)

MAX_RETRIES = 3
_SETTLED_THINK: dict[str, bool | str] = {}
_SCHEMALESS_MODELS: set[str] = set()
RULES = (
    "<rules>\n"
    "- Do exactly what the task says, nothing more.\n"
    "- Do not add files, helpers, config keys, or comments.\n"
    "- Use only names that already appear in the context.\n"
    "</rules>"
)
EDIT_HINT = (
    "<edit>\n"
    "- anchor: first line to replace. end_anchor: last line (omit if one line).\n"
    "- Copy lines exactly as shown, without the \"  12 | \" gutter.\n"
    "- If you cannot copy exact text, use the gutter number alone, e.g. \"12\".\n"
    "- new_text replaces whole lines; keep their indentation.\n"
    "- If the text repeats, set occurrence (1-based).\n"
    "</edit>"
)
FILE_RULE = (
    "<file>\n"
    "- file exists -> edit.\n"
    "- file does not exist -> write.\n"
    "- unsure -> edit.\n"
    "- Exactly one of edit or write, or neither.\n"
    "</file>"
)
FREE_SHAPE = (
    "<output>\n"
    "Return ONLY this JSON. No prose, no markdown fences:\n"
    '{"response": "<one line>", '
    '"edit": {"file_path": "<path relative to repo root>", "anchor": "<line>", '
    '"end_anchor": "<line>", "occurrence": 1, "new_text": "<lines>"}, '
    '"write": {"file_path": "<path relative to repo root>", "content": "<whole file>"}}\n'
    f"{FILE_RULE}"
    "Omit keys you do not use. Never emit a unified diff. response is a string.\n"
    "Example:\n"
    '{"response": "renamed x to count", "edit": {"file_path": "app/main.py", '
    '"anchor": "    x = 0", "new_text": "    count = 0"}}\n'
    "</output>"
)

async def _llm_json_with_retry(
    model: str,
    prompt: str,
    schema: type[BaseModel],
    max_retries: int = MAX_RETRIES,
    verbose: bool = False,
    think: bool | str | None = None,
):
    """Call LLM with schema-constrained JSON and retry on validation failure."""
    # verbose only shows the thinking; it does not enable it.
    # Falsy caller think wins (never rejected); a settled value beats config and
    # truthy guesses, since re-sending a rejected form re-probes a known 400.
    if think is None or (think and model in _SETTLED_THINK):
        think = _SETTLED_THINK.get(model, think_for(model))
    if isinstance(think, str) and think.lower() in ("true", "false"):
        think = think.lower() == "true"
    validate = schema.model_validate_json
    fmt = "json" if model in _SCHEMALESS_MODELS else schema.model_json_schema()
    raw_json = ""
    attempt = 0
    while attempt < max_retries:
        try:
            raw_json, thinking = await llm.complete_with_thinking(
                model, "user", prompt, think=think, format=fmt
            )
            if verbose and thinking.strip():
                console.print(
                    Panel(Text(thinking.strip()), title="think", title_align="left", border_style="dim")
                )
            if think and not raw_json.strip() and thinking.strip():
                # The trace ate the whole num_predict budget (done_reason:
                # length), leaving no tokens for the answer.
                _SETTLED_THINK[model] = False
                think = False
                console.print(
                    f"[yellow]{model}'s thinking consumed the output budget; retrying with think=False.[/yellow]"
                )
                continue
        except Exception as e:
            err = str(e).lower()
            if think and "think" in err:
                # string -> true -> false; "does not support thinking" means the
                # capability is absent, so skip the boolean step. No attempt spent.
                think = (
                    True
                    if isinstance(think, str) and "does not support thinking" not in err
                    else False
                )
                _SETTLED_THINK[model] = think
                console.print(
                    f"[yellow]{model} rejected that think form; retrying with think={think}.[/yellow]"
                )
                continue
            if fmt != "json" and ("format" in err or "schema" in err):
                _SCHEMALESS_MODELS.add(model)
                fmt = "json"
                console.print(
                    f"[yellow]{model} rejected the response schema; falling back to format=json.[/yellow]"
                )
                continue
            attempt += 1
            console.print(f"[red]LLM call failed (attempt {attempt}/{max_retries}): {e}[/red]")
            if attempt < max_retries:
                console.print("[yellow]Retrying...[/yellow]")
                await asyncio.sleep(1)
                continue
            return None
        try:
            return validate(raw_json)
        except ValidationError as e:
            attempt += 1
            console.print(f"[yellow]Validation error (attempt {attempt}/{max_retries}):[/yellow] {e}")
            if attempt < max_retries:
                console.print("[yellow]Retrying...[/yellow]")
                prompt += f"\n\nPREVIOUS ERROR: {e}\nFix the JSON structure and try again."
        except Exception as e:
            attempt += 1
            console.print(f"[red]Parse error: {e}[/red]")
            if attempt < max_retries:
                console.print("[yellow]Retrying...[/yellow]")
                prompt += f"\n\nPREVIOUS ERROR: {e}\nFix the JSON and try again."
    console.print(f"[red]Failed after {max_retries} attempts[/red]")
    if raw_json:
        console.print(f"[dim]Raw output:[/dim]\n{raw_json[:2000]}")
    return None

def _find_index_root(start: Path) -> Path | None:
    for p in [start, *start.parents]:
        if (p / ".vc" / "vcdb.db").exists():
            return p
        if (p / "codebase.json").exists():
            return p
    return None

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def validate_patch(patch: str, root: Path) -> List[str]:
    """Check a unified diff's hunks against the real file. Empty list = hunks line up."""
    target = None
    hunks = []
    cur = None
    for line in patch.splitlines():
        if target is None and line.startswith("+++ "):
            p = line[4:].strip()
            target = p[2:] if p.startswith(("a/", "b/")) else p
            continue
        m = _HUNK_RE.match(line)
        if m:
            cur = (int(m.group(1)), int(m.group(2) or 1), [])
            hunks.append(cur)
        elif cur is not None:
            if line.startswith("\\"):  # "\ No newline at end of file"
                continue
            if line.startswith("-"):
                cur[2].append(line[1:])
            elif line.startswith(" "):
                cur[2].append(line[1:])

    if not target:
        return ["patch has no '+++ b/<path>' target header"]
    fpath = Path(target)
    if not fpath.is_absolute():
        fpath = root / fpath
    if not fpath.exists():
        return [f"target file does not exist: {target}"]
    if not hunks:
        return [f"{target}: patch contains no @@ hunks"]

    lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
    problems = []
    for old_start, old_count, old_side in hunks:
        if old_count != len(old_side):
            problems.append(
                f"{target}: @@ -{old_start},{old_count} @@ declares {old_count} old line(s) "
                f"but the hunk body has {len(old_side)}"
            )
        if old_start < 1 or old_start - 1 + len(old_side) > len(lines):
            problems.append(
                f"{target}: @@ -{old_start} @@ is outside the file (1..{len(lines)})"
            )
            continue
        actual = lines[old_start - 1: old_start - 1 + len(old_side)]
        if actual != old_side:
            for i, (a, b) in enumerate(zip(actual, old_side)):
                if a != b:
                    problems.append(
                        f"{target}: @@ -{old_start} @@ does not match at line {old_start + i} "
                        f"-- file has {a.strip()!r}, patch expects {b.strip()!r}"
                    )
                    break
    return problems


def _proposed_diff(root: Path, response: FreeResponse) -> str:
    """The diff an action would produce, without writing anything.

    apply_action is the only thing that writes, so the preview runs the same
    resolvers against a throwaway copy in a temp dir instead of a second,
    possibly divergent, implementation of them.
    """
    action = response.edit or response.write
    with tempfile.TemporaryDirectory() as tmp:
        shadow = Path(tmp)
        target = root / action.file_path
        # file_path may be absolute, and root / "/abs" is that same path, so both
        # the shadow copy and the preview action need it made relative first.
        try:
            rel = target.relative_to(root)
        except ValueError:
            rel = Path(action.file_path)
        dest = shadow / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.copy2(target, dest)
        retargeted = action.model_copy(update={"file_path": str(rel)})
        preview = FreeResponse(edit=retargeted) if response.edit else FreeResponse(write=retargeted)
        try:
            return apply_action(shadow, preview)
        except (AnchorError, PermissionError, OSError) as e:
            return f"(cannot apply: {e})"


def _apply_one(root: Path, label: str, response: FreeResponse) -> bool:
    """Show the diff, confirm, then apply. False if refused or failed."""
    action = response.edit or response.write
    preview = _proposed_diff(root, response)
    if preview.startswith("(cannot apply"):
        console.print(f"\n[bold]{label} {action.file_path}[/bold]")
        console.print(f"[yellow]{preview}[/yellow]")
        return False

    console.print(f"\n[bold]{label} {action.file_path}[/bold]")
    console.print(preview)

    # No TTY means no human to answer; declining keeps an unattended run from
    # writing anything, instead of dying on EOFError mid-pipeline.
    if not sys.stdin.isatty():
        console.print("[yellow]Not a terminal: declined automatically.[/yellow]")
        return False

    if not Confirm.ask("Apply this change?"):
        console.print("[yellow]Skipped (not applied).[/yellow]")
        return False

    try:
        diff = apply_action(root, response)
    except AnchorError as e:
        console.print(f"\n[bold red]{label} of {action.file_path} FAILED:[/bold red] {e}")
        for line_no, text in getattr(e, "candidates", []):
            console.print(f"[dim]  candidate line {line_no}: {text}[/dim]")
        return False
    except (PermissionError, OSError) as e:
        console.print(f"\n[bold red]{label} of {action.file_path} REFUSED:[/bold red] {e}")
        return False

    console.print(f"\n[bold green]Applied {label} to {action.file_path}:[/bold green]")
    console.print(diff)
    console.print(f"[dim]Previous version saved under .vc/undo/[/dim]")
    return True


def _real_lines(db_path: Path, file_path: str, limit: int = 40) -> str:
    """A file's real numbered lines, from the same FTS store the context came from."""
    stem = Path(file_path).stem
    out: List[str] = []
    for b in search_code(db_path, stem):
        if not b["parent_file"].endswith(file_path):
            continue
        for i, line in enumerate(b["code"].splitlines()):
            out.append(f"{b['line_range_start'] + i:>5} | {line}")
    return "\n".join(out[:limit])


def _rejected(root: Path, res: FreeResponse) -> Optional[str]:
    """Why apply_action would refuse this response, or None if it would apply."""
    # Catches both verb mistakes before the user has to decline a bad diff.
    if res.write and (root / res.write.file_path).exists():
        return f"{res.write.file_path} already exists; use edit to change it"
    if not res.edit:
        return None

    action = res.edit
    target = root / action.file_path
    if not target.exists():
        return f"{action.file_path} does not exist; use write to create it"
    try:
        with open(target, "r", encoding="utf-8") as f:
            resolve_span(f.readlines(), action.anchor, action.end_anchor, action.occurrence)
    except (AnchorError, OSError) as e:
        return str(e)
    return None


async def _apply_with_anchor_retry(
    res: FreeResponse, db_path: Path, root: Path, prompt: str, verbose: bool
) -> Optional[FreeResponse]:
    """The response that was applied (the retry's, if one replaced it), else None."""
    reason = _rejected(root, res)
    if reason is None:
        return res if _execute_coder_result(res, root) else None

    action = res.write or res.edit
    lines = _real_lines(db_path, action.file_path)
    if not lines:
        return res if _execute_coder_result(res, root) else None

    console.print(f"[dim]{reason}; retrying against the file's real lines...[/dim]")
    retry_prompt = prompt + (
        f"\n<retry>\n"
        f"The action for {action.file_path} was rejected: {reason}.\n"
        f"Real lines of that file:\n{lines}\n"
        f"{FILE_RULE}\n"
        "Resend ONE action. Copy an anchor from the lines above with no line\n"
        "numbers, or use the bare line number.\n"
        "</retry>"
    )
    again = await _llm_json_with_retry(
        CODER_MODEL, retry_prompt, FreeResponse, verbose=verbose,
        think=think_for(CODER_MODEL, "coder"),
    )
    if again and _execute_coder_result(again, root):
        return again
    return None


def _refresh_step_files(db_path: Path, res: FreeResponse) -> None:
    """Re-parse applied files so the next step retrieves post-edit code."""
    root = db_path.parent.parent
    for action in (res.edit, res.write):
        if action is None:
            continue
        path = (root / action.file_path).resolve()
        if path.suffix not in SUPPORTED_SUFFIXES or not path.exists():
            continue
        str_path = str(path)
        # blocks FK to files; a write-created file has no row yet.
        upsert_file_metadata(db_path, FileMetaData(
            name=path.name, path=path, language=path.suffix,
            size_bytes=path.stat().st_size, hash=compute_sha256(path),
        ))
        delete_blocks_for_file(db_path, str_path)
        upsert_block(db_path, str_path, parse_file(path))


def _execute_coder_result(res: FreeResponse, root: Path) -> bool:
    """Apply the coder's actions. False if an action failed."""
    ok = True
    if res.thought:
        console.print(f"\n[bold cyan]Thought:[/bold cyan] {res.thought}")
    # One action per call: apply_action reads a single edit or write, so passing
    # both would silently drop one.
    if res.edit and not _apply_one(root, "edit", FreeResponse(edit=res.edit)):
        ok = False
    if res.write and not _apply_one(root, "write", FreeResponse(write=res.write)):
        ok = False
    if res.run:
        for action in res.run:
            console.print(f"\n[bold magenta]Proposed command: {action.command}[/bold magenta]")
    if res.response:
        console.print(f"\n[bold green]Response:[/bold green] {res.response}")
    return ok

def _block_meta(b: dict) -> str:
    """One metadata line per block: the raw signature plus its dependencies."""
    line = f"{b.get('type') or 'def'} {b['name']} :: {b.get('signature') or ''}".rstrip()
    deps = [d for d in (b.get("dependencies") or []) if d]
    if deps:
        line += f" | deps: {', '.join(deps)}"
    return line

_GUTTER_PREFIX = 8  # width of f"{n:>5} | " -> 5 number + 1 space + bar + 1 space


def _step_keywords(db_path: Path, step: str, limit: int = 4) -> Tuple[List[str], List[dict]]:
    """Keywords from a plan step that actually retrieve something, most specific first."""
    keywords: List[str] = []
    blocks: List[dict] = []
    for w in sorted(own_terms(step), key=lambda t: ("_" in t, len(t)), reverse=True):
        if len(keywords) >= limit:
            break
        hits = search_code(db_path, w)
        if hits:
            keywords.append(w)
            blocks.extend(hits)
    return keywords, blocks


def _numbered_code(b: dict) -> str:
    """Prefix each code line with its real file line number so the model need not count."""
    start = b['line_range_start']
    return "\n".join(
        f"{start + i:>5} | {line}"
        for i, line in enumerate(b['code'].splitlines())
    )

def _pack_budget(blocks: List[dict], budget: int) -> List[dict]:
    """Cap results to a token budget, keeping whole blocks and evicting cheaper ones."""

    packed: List[dict] = []
    for b in blocks:
        est = _estimate_block(b)
        if not packed:
            packed.append(b)
            continue
        # Recomputed rather than incremented, because a block admitted as the
        # oversized floor earlier may have been displaced since.
        used = sum(_estimate_block(x) for x in packed)
        if used + est <= budget:
            packed.append(b)
            continue
        # Too big for the headroom left. Displace, but only blocks strictly
        # smaller than this one: evicting an equal or larger block frees no room
        # worth the churn and would drop a higher-ranked block for nothing.
        drop: set = set()
        for i in sorted(range(len(packed)), key=lambda j: _estimate_block(packed[j])):
            if used + est <= budget:
                break
            if _estimate_block(packed[i]) >= est:
                break
            used -= _estimate_block(packed[i])
            drop.add(i)
        if not drop:
            # No displacement makes room; keep what already fits.
            continue
        packed = [x for j, x in enumerate(packed) if j not in drop]
        packed.append(b)
        if sum(_estimate_block(x) for x in packed) > budget:
            # The floor block plus this one still overflows, so the floor block
            # loses: a lone oversized block is the only state allowed past budget.
            packed = [b]
    return packed


def _estimate_block(b: dict) -> int:
    code = b.get("code") or ""
    # The join in _numbered_code preserves code's own newlines and only adds
    # one fixed prefix per line, so this is exact, not a fudge.
    n_lines = code.count("\n") + 1 if code else 0
    return (len(code) + n_lines * _GUTTER_PREFIX) // 3


_RRF_K = 60
_EXACT_NAME_BOOST = 1000.0


def _fuse(per_keyword: List[Tuple[str, List[dict]]]) -> List[dict]:
    """Reciprocal rank fusion; an exact name match outranks everything."""
    scores: dict = {}
    best: dict = {}
    for kw, blocks in per_keyword:
        for rank, b in enumerate(blocks):
            key = (b['parent_file'], b['line_range_start'], b['line_range_end'])
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank)
            if key not in best:
                best[key] = b
                if b['name'].lower() == kw.lower():
                    scores[key] += _EXACT_NAME_BOOST
    return [best[k] for k in sorted(scores, key=scores.get, reverse=True)]

async def _run_coder(db_path: Path, keywords: List[str], task: str, verbose: bool = False):
    console.print("[dim]Retrieving context (code)...[/dim]")
    fused = _fuse([(kw, search_code(db_path, kw)) for kw in keywords])

    console.print(f"[cyan]Retrieved {len(fused)} unique blocks from {len(set(b['parent_file'] for b in fused))} files[/cyan]")

    selected = _pack_budget(fused, TOKEN_BUDGET)
    if len(selected) < len(fused):
        console.print(f"[yellow]Budget {TOKEN_BUDGET} tok: kept {len(selected)}/{len(fused)} blocks[/yellow]")

    context_str = ""
    for b in selected:
        context_str += f"--- File: {b['parent_file']} | Range: {b['line_range_start']}-{b['line_range_end']} ---\n"
        context_str += f"{_block_meta(b)}\n"
        if b.get('summary'):
            context_str += f"Summary: {b['summary']}\n"
        if b.get('code'):
            context_str += f"Code:\n```\n{_numbered_code(b)}```\n"
        context_str += "\n"

    prompt = (
        f"<context>\n{context_str}</context>\n\n"
        f"<task>\n{task}\n</task>\n\n"
        f"{RULES}\n"
        f"{EDIT_HINT}\n"
        f"{FREE_SHAPE}"
    )

    console.print("[dim]Thinking...[/dim]")
    res = await _llm_json_with_retry(
        CODER_MODEL, prompt, FreeResponse, verbose=verbose,
        think=think_for(CODER_MODEL, "coder"),
    )
    if not res:
        console.print("[red]Coder failed to produce valid JSON[/red]")
        return False
    return (await _apply_with_anchor_retry(res, db_path, db_path.parent.parent, prompt, verbose)) is not None

async def _run_multi(db_path: Path, keywords: List[str], task: str, verbose: bool = False) -> bool:
    applied_any = False
    console.print("[dim]Retrieving context (summaries)...[/dim]")
    # search_code, not search_blocks: this list is rendered by _block_meta and
    # _pack_budget, which need deserialised params and the code body. Raw rows
    # carry params as a JSON string, so rendering one raises TypeError.
    fused = _fuse([(kw, search_code(db_path, kw)) for kw in keywords])

    console.print(f"[cyan]Retrieved {len(fused)} unique blocks from {len(set(b['parent_file'] for b in fused))} files[/cyan]")

    summary_str = ""
    for b in fused:
        summary_str += f"- {b['parent_file']}:{b['name']} ({b['line_range_start']}-{b['line_range_end']}): {b['summary']}\n"

    root = _find_index_root(Path.cwd().resolve())
    plan_path = root / "last_plan.json" if root else None
    plan: PlanResponse | None = None

    if plan_path and plan_path.exists():
        try:
            with plan_path.open("r", encoding="utf-8") as f:
                plan_data = json.load(f)
                if plan_data.get("original_prompt") == task:
                    plan = PlanResponse.model_validate(plan_data)
                    console.print("[green]Loaded previous plan (matched prompt).[/green]")
                else:
                    plan = None
                    console.print("[yellow]Found a previous plan, but it doesn't match the current task. Creating new plan...[/yellow]")
        except Exception as e:
            console.print(f"[red]Failed to load plan: {e}[/red]")
            plan = None
    else:
        plan = None

    if not plan:
        planner_prompt = (
            f"<summaries>\n{summary_str}</summaries>\n\n"
            f"<task>\n{task}\n</task>\n\n"
            "<rules>\n"
            "- Output an ordered list of steps. Each step changes ONE thing.\n"
            "- A step is ONE file and ONE symbol. Never bundle two changes in a step.\n"
            "- Name the exact file path and symbol from the summaries, verbatim.\n"
            "- Ideal maximum: 8 steps; use fewer for a small task.\n"
            "- Use only symbols present in the summaries. Never invent a name.\n"
            "</rules>\n"
            "<output>\n"
            'Return ONLY {"steps": ["step", "step"]}. Plain strings, no objects, no code.\n'
            "Example:\n"
            '{"steps": ["In app/main.py, rename x to count in index", '
            '"In app/main.py, add a log call in save"]}\n'
            "</output>"
        )

        console.print("[dim]Planning...[/dim]")
        plan = await _llm_json_with_retry(
            PLANNER_MODEL, planner_prompt, PlanResponse, verbose=verbose,
            think=think_for(PLANNER_MODEL, "planner"),
        )
        
        if not plan or not plan.steps:
            console.print("[yellow]Planner returned empty steps[/yellow]")
            return False

        console.print(f"[green]New Plan ({len(plan.steps)} steps):[/green]")
        for i, step in enumerate(plan.steps, 1):
            console.print(f"  [cyan]{i}.[/cyan] {step}")
            
        if plan_path:
            try:
                with plan_path.open("w", encoding="utf-8") as f:
                    plan_to_save = plan.model_dump()
                    plan_to_save["original_prompt"] = task
                    json.dump(plan_to_save, f, indent=4, ensure_ascii=False)
            except OSError as e:
                console.print(f"[red]Error saving plan: {e}[/red]")

        confirmation = await asyncio.to_thread(Confirm.ask, "Proceed with this new plan?")
        if not confirmation:
            console.print("[yellow]Plan aborted. View it with: [bold]show plan[/bold][/yellow]")
            return False
    else:
        # Plan was loaded
        console.print(f"[green]Loaded Plan ({len(plan.steps)} steps):[/green]")
        for i, step in enumerate(plan.steps, 1):
            console.print(f"  [cyan]{i}.[/cyan] {step}")
        
        confirmation = await asyncio.to_thread(Confirm.ask, "Proceed with loaded plan?")
        if not confirmation:
            console.print("[yellow]Plan aborted. View it with: [bold]show plan[/bold][/yellow]")
            return False

    # Execution loop
    for i, step in enumerate(plan.steps, 1):
        console.print(f"\n[dim]Step {i}/{len(plan.steps)}: {step}[/dim]")
        step_kws, step_blocks = _step_keywords(db_path, step)
        if not step_blocks:
            step_blocks = fused[:3]
            
        seen = set()
        deduped = []
        for b in step_blocks:
            key = (b['parent_file'], b['line_range_start'])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(b)

        step_context = ""
        for b in _pack_budget(deduped, TOKEN_BUDGET):
            step_context += f"--- File: {b['parent_file']} | Range: {b['line_range_start']}-{b['line_range_end']} ---\n"
            step_context += f"{_block_meta(b)}\n"
            if b.get('summary'):
                step_context += f"Summary: {b['summary']}\n"
            if b.get('code'):
                step_context += f"Code:\n```\n{_numbered_code(b)}```\n"
            step_context += "\n"
        if not step_context.strip():
            step_context = summary_str

        step_files = sorted({b['parent_file'] for b in deduped})
        scope = ""
        if step_files:
            scope += f"Files in context: {', '.join(step_files)}\n"
        if step_kws:
            scope += f"Keywords: {', '.join(step_kws)}\n"

        coder_prompt = (
            f"<context>\n{step_context}</context>\n\n"
            f"<task>\n{step}\n</task>\n\n"
            f"{scope}\n"
            f"{RULES}\n"
            f"{EDIT_HINT}\n"
            f"{FREE_SHAPE}"
        )
        res = await _llm_json_with_retry(
            CODER_MODEL, coder_prompt, FreeResponse, verbose=verbose,
            think=think_for(CODER_MODEL, "coder"),
        )
        if not res:
            continue
        applied = await _apply_with_anchor_retry(res, db_path, db_path.parent.parent, coder_prompt, verbose)
        if applied:
            applied_any = True
            _refresh_step_files(db_path, applied)
    return applied_any


def view_plan(path: Path):
    try:
        with path.open("r", encoding="utf-8") as f:
            plan = PlanResponse.model_validate(json.load(f))
            if not plan.steps:
                return
            console.print(f"[green]Plan ({len(plan.steps)} steps):[/green]")
            for i, step in enumerate(plan.steps, 1):
                console.print(f"  [cyan]{i}.[/cyan] {step}")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        console.print("[yellow]No valid previous plan found.[/yellow]")

async def _run_index_pipeline(dir_path: Path):
    db_path = dir_path / ".vc" / "vcdb.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(db_path)
    stored_hashes = get_file_hashes(db_path)
    unsummarised = get_unsummarised_files(db_path)
    db_paths = set(stored_hashes.keys())
    active_paths: set[str] = set()

    async def _run_pipeline():
        for file_metadata in walk(dir_path):
            str_path = str(file_metadata.path)
            active_paths.add(str_path)
            if stored_hashes.get(str_path) == file_metadata.hash and str_path not in unsummarised:
                continue
            upsert_file_metadata(db_path, file_metadata)
            if file_metadata.language in SUPPORTED_SUFFIXES:
                if str_path in stored_hashes:
                    delete_blocks_for_file(db_path, str_path)
                blocks = parse_file(file_metadata.path)
                upsert_block(db_path, str_path, blocks)
                with open(file_metadata.path, "rb") as f:
                    file_bytes = f.read()
                try:
                    await summarize_and_store(llm, db_path, blocks, source_code=file_bytes)
                except Exception as e:
                    console.print(f"[yellow]Skipped summaries for {file_metadata.name}: {e}[/yellow]")
        write_codebase_json(db_path, dir_path / "codebase.json")

    await _run_pipeline()
    # Second pass: cross-block references need every file parsed first.
    resolve_all_dependencies(db_path)
    stale_paths = db_paths - active_paths
    prune_deleted_files(db_path, dir_path / "codebase.json", stale_paths)
    console.print("[green]Indexing complete.[/green]")

@app.command(help="Index a directory: parse, summarise and store code blocks.")
def index(
    path: str = typer.Argument(".", help="Directory to index"),
):
    root = Path(path).resolve()
    asyncio.run(_run_index_pipeline(root))

def _record_run(root: Path, task: str, mode: str, applied: bool) -> None:
    """Append one line to .vc/history.jsonl for `vynacode log`."""
    entry = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "task": task,
        "mode": mode,
        "applied": applied,
    }
    try:
        path = root / ".vc" / "history.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        # History is a convenience; losing an entry must not fail the run.
        console.print(f"[yellow]Could not record run history: {e}[/yellow]")


@app.command(help="Act on a request: retrieve context, then propose patches and commands.")
def do(
    message: str = typer.Argument(..., help="Task description in plain English"),
    mode: str = typer.Option("single", help="single (coder JSON) or multi (planner + coder JSON)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show the model's <think> blocks"),
):
    async def _run_do():
        root = _find_index_root(Path.cwd().resolve())
        if not root:
            console.print("[red]Error: No index found. Run 'vynacode index' first.[/red]")
            return

        db_path = root / ".vc" / "vcdb.db"
        if not ensure_indexed(db_path):
            console.print("[red]Error: Index not found. Run 'vynacode index' first.[/red]")
            return

        console.print("[dim]Expanding query...[/dim]")
        try:
            keywords = await llm.expand_query(
                CODER_MODEL, message, retry=_llm_json_with_retry, verbose=verbose
            )
        except Exception as e:
            console.print(f"[red]Query expansion failed: {e}[/red]")
            import traceback as _tb
            _tb.print_exc()
            return
        console.print(f"[cyan]Keywords:[/cyan] {', '.join(keywords)}")

        if mode == "single":
            applied = await _run_coder(db_path, keywords, message, verbose)
        elif mode == "multi":
            applied = await _run_multi(db_path, keywords, message, verbose)
        else:
            console.print(f"[red]Unknown mode: {mode}. Use 'single' or 'multi'[/red]")
            return

        # Before the re-index, so a crash there still records the run.
        _record_run(root, message, mode, applied)

        # Re-index
        console.print("[dim]Re-indexing codebase...[/dim]")
        await _run_index_pipeline(root)

    try:
        asyncio.run(_run_do())
    except Exception as e:
        console.print(f"[red]Fatal error: {e}[/red]")
        import traceback as _tb
        _tb.print_exc()

@app.command(help="Inspect the index: last plan, search results, or the full codebase.json.")
def show(
    option: str = typer.Argument(default="plan", help="Option to show: plan, context, index"),
    query: str = typer.Option(None, "--query", "-q", help="Search query for context"),
):
    root = _find_index_root(Path.cwd().resolve())
    if root is None or not (root / ".vc" / "vcdb.db").exists():
        console.print("[red]Error: codebase not indexed.[/red]")
        return
    db_path = root / ".vc" / "vcdb.db"

    if option == "plan":
        view_plan(root / "last_plan.json")
    elif option == "context":
        if query:
            results = search_blocks(db_path, query)
            if not results:
                console.print("[yellow]No results found![/yellow]")
            for i, block in enumerate(results, 1):
                console.print(f"\n[bold cyan]Result {i}:[/bold cyan] [bold]{block['name']}[/bold] in [magenta]{block['parent_file']}[/magenta]")
                console.print(f"  [dim]Lines:[/dim] {block['line_range_start']}-{block['line_range_end']}")
                console.print(f"  [dim]Summary:[/dim] {block['summary']}")
        else:
            console.print("[red]Error: query is required.[/red]")
    elif option == "index":
        index_path = root / "codebase.json"
        if index_path.exists():
            try:
                with index_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                    console.print(json.dumps(data, indent=2))
            except Exception as e:
                console.print(f"[red]Error reading index: {e}[/red]")
        else:
            console.print("[yellow]No codebase.json found.[/yellow]")

@app.command(help="Show run history: what was asked, what was applied.")
def log(
    limit: int = typer.Option(20, "--limit", "-n", help="How many most recent runs to show"),
):
    root = _find_index_root(Path.cwd().resolve())
    if not root:
        console.print("[red]Error: No index found. Run 'vynacode index' first.[/red]")
        return
    history_path = root / ".vc" / "history.jsonl"
    if not history_path.exists():
        console.print("[yellow]No run history yet.[/yellow]")
        return
    entries: List[dict] = []
    with history_path.open("r", encoding="utf-8") as f:
        # Newest first, so limit cuts the tail the user actually reads.
        for line in reversed(f.readlines()):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(entries) >= limit:
                break
    if not entries:
        console.print("[yellow]No readable run history.[/yellow]")
        return
    for e in entries:
        mark = "[green]ok[/green]" if e.get("applied") else "[red]none[/red]"
        console.print(
            f"[dim]{e.get('time', '?')}[/dim]  [cyan]{e.get('mode', '?')}[/cyan]  {mark}  {e.get('task', '')}"
        )

@app.command(help="Show or update configuration.")
def config(
    coder_model: Optional[str] = typer.Option(None, "--coder-model", "-c", help="Model that writes code"),
    planner_model: Optional[str] = typer.Option(None, "--planner-model", "-p", help="Model that plans and summarises"),
    context_window: Optional[int] = typer.Option(None, "--context-window", "-w", help="Model context window, in tokens"),
    ollama_url: Optional[str] = typer.Option(None, "--ollama-url", "-u", help="Ollama base URL"),
    think_level: Optional[str] = typer.Option(None, "--think-level", "-t", help="Thinking effort: " + ", ".join(THINK_LEVELS)),
    coder_think: Optional[str] = typer.Option(None, "--coder-think", help="Thinking effort for the coder model only"),
    planner_think: Optional[str] = typer.Option(None, "--planner-think", help="Thinking effort for the planner model only"),
):
    def _valid_think(value: str, flag: str) -> None:
        # Validated here rather than at load time so a typo is reported to the
        # user, instead of being silently replaced by the default.
        if value not in THINK_LEVELS:
            console.print(f"[red]Error: {flag} must be one of {', '.join(THINK_LEVELS)}.[/red]")
            raise typer.Exit(1)

    updates: dict = {}
    if coder_model is not None:
        updates["coder_model"] = coder_model
    if planner_model is not None:
        updates["planner_model"] = planner_model
    if ollama_url is not None:
        updates["ollama_url"] = ollama_url
    if think_level is not None:
        _valid_think(think_level, "--think-level")
        updates["think_level"] = think_level
    if coder_think is not None or planner_think is not None:
        # Role keys, not model names: both roles may share one model.
        overrides = dict(current_settings().get("think_overrides") or {})
        if coder_think is not None:
            _valid_think(coder_think, "--coder-think")
            overrides["coder"] = coder_think
        if planner_think is not None:
            _valid_think(planner_think, "--planner-think")
            overrides["planner"] = planner_think
        updates["think_overrides"] = overrides
    if context_window is not None:
        # Guards the budget arithmetic rather than the value: a zero or
        # negative window would make every later context cap collapse.
        if context_window < 1:
            console.print("[red]Error: --context-window must be a positive integer.[/red]")
            raise typer.Exit(1)
        updates["context_window"] = context_window

    if updates:
        path = save_config(updates)
        changed = ", ".join(sorted(updates))
        console.print(f"[green]Saved[/green] [bold]{changed}[/bold] [dim]->[/dim] {path}")
        console.print("[dim]Applies to the next command; this process keeps its loaded values.[/dim]")

    active = config_path()
    exists = active.exists()
    console.print(f"\n[bold]Config file:[/bold] [cyan]{active}[/cyan]")
    console.print(
        "  [green]in use[/green]" if exists else "  [yellow]not created yet - defaults in use[/yellow]"
    )

    console.print("\n[bold]Settings[/bold]")
    for key, value in current_settings().items():
        origin = "file" if is_overridden(key) else "default"
        console.print(f"  [bold]{key:<15}[/bold] [cyan]{value}[/cyan]  [dim]({origin})[/dim]")

    console.print("\n[bold]Effective think[/bold]")
    settled = current_settings()
    for role in ("coder", "planner"):
        m = settled[f"{role}_model"]
        console.print(f"  [bold]{role:<15}[/bold] [cyan]{think_for(m, role)}[/cyan]  [dim]({m})[/dim]")

    console.print("\n[bold]Read-only[/bold]")
    console.print(f"  [bold]{'tier':<15}[/bold] [magenta]{TIER}[/magenta]")
    console.print(f"  [bold]{'token_budget':<15}[/bold] [magenta]{TOKEN_BUDGET}[/magenta]")

@app.command(help="Check the environment: Ollama reachability, models, index state.")
def doctor():
    problems = 0

    async def _run_doctor():
        nonlocal problems
        # First, because every probe below uses the URL it reports.
        console.print(f"[bold]Ollama[/bold] [dim]{OLLAMA_URL}[/dim]")
        try:
            reachable = await llm.ping()
            installed = await llm.list_models() if reachable else []
        except Exception as e:
            console.print(f"  [red]unreachable[/red] [dim]{e}[/dim]")
            return
        if not reachable:
            problems += 1
            console.print("  [red]unreachable[/red] [dim]is ollama running?[/dim]")
            return
        console.print("  [green]reachable[/green]")
        # Match on bare name: Ollama reports installed models tagged.
        for label, model in (("coder", CODER_MODEL), ("planner", PLANNER_MODEL)):
            hit = next((n for n in installed if n == model or n.split(":")[0] == model.split(":")[0]), None)
            if hit:
                console.print(f"  [green]ok[/green] {label} model [cyan]{model}[/cyan] [dim]({hit})[/dim]")
            else:
                problems += 1
                console.print(f"  [red]missing[/red] {label} model [cyan]{model}[/cyan] [dim]ollama pull {model}[/dim]")

    asyncio.run(_run_doctor())

    console.print("\n[bold]Index[/bold]")
    root = _find_index_root(Path.cwd().resolve())
    if not root:
        problems += 1
        console.print("  [red]no index[/red] [dim]run 'vynacode index'[/dim]")
        return
    db_path = root / ".vc" / "vcdb.db"
    if not ensure_indexed(db_path):
        problems += 1
        console.print(f"  [red]no database[/red] [dim]{db_path}[/dim]")
    else:
        try:
            stats = index_stats(db_path)
            console.print(f"  [green]ok[/green] [dim]{root}[/dim]")
            console.print(
                f"       {stats['files']} files, {stats['blocks']} blocks, "
                f"{stats['summarised']} summarised"
            )
            # Warning, not failure: retrieval still works without summaries.
            if stats["blocks"] and stats["summarised"] < stats["blocks"]:
                console.print(
                    f"  [yellow]partial[/yellow] {stats['blocks'] - stats['summarised']} block(s) unindexed for summaries"
                )
        except Exception as e:
            problems += 1
            console.print(f"  [red]unreadable[/red] [dim]{e}[/dim]")

    raise typer.Exit(1 if problems else 0)


@app.command(help="Show index size, models in use and the last plan.")
def status():
    root = _find_index_root(Path.cwd().resolve())
    console.print(f"[bold]Repo[/bold]  [cyan]{root or 'not found (cwd is outside an index)'}[/cyan]")

    console.print("\n[bold]Models[/bold]")
    for key in ("coder_model", "planner_model"):
        value = current_settings()[key]
        origin = "file" if is_overridden(key) else "default"
        console.print(f"  [bold]{key:<14}[/bold] [cyan]{value}[/cyan] [dim]({origin})[/dim]")
    for role in ("coder", "planner"):
        m = current_settings()[f"{role}_model"]
        console.print(f"  [bold]{'think (' + role + ')':<14}[/bold] [cyan]{think_for(m, role)}[/cyan]")
    console.print(f"  [bold]{'token_budget':<14}[/bold] [cyan]{TOKEN_BUDGET}[/cyan] [dim]({TIER} tier)[/dim]")

    console.print("\n[bold]Index[/bold]")
    if not root:
        console.print("  [yellow]not indexed[/yellow]")
        return
    db_path = root / ".vc" / "vcdb.db"
    if not ensure_indexed(db_path):
        console.print("  [yellow]not indexed[/yellow] [dim]run 'vynacode index'[/dim]")
        return
    stats = index_stats(db_path)
    console.print(f"  [dim]{db_path}[/dim]")
    console.print(
        f"  {stats['files']} files, {stats['blocks']} blocks, {stats['summarised']} summarised"
    )
    unsummarised = stats["blocks"] - stats["summarised"]
    if unsummarised:
        console.print(f"  [yellow]{unsummarised} block(s) still lack summaries[/yellow]")

    console.print("\n[bold]Last plan[/bold]")
    plan_path = root / "last_plan.json"
    if not plan_path.exists():
        console.print("  [yellow]none[/yellow]")
        return
    try:
        with plan_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        steps = data.get("steps") or []
        console.print(f"  [dim]{len(steps)} step(s) for:[/dim] {data.get('original_prompt', '?')}")
    except (json.JSONDecodeError, OSError) as e:
        console.print(f"  [red]unreadable:[/red] {e}")

if __name__ == "__main__":
    app()
