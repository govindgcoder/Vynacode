"""One case per fixed bug. Run: .venv/bin/pytest tests/test_editor.py -v"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vynacode.backend.editor import (
    Ambiguous,
    AnchorError,
    apply_action,
    apply_edit,
    render_diff,
    resolve_anchor,
    resolve_span,
    take_snapshot,
)
from vynacode.backend.schema import EditAction, FreeResponse, FullWriteAction

LINES = ["def alpha():\n", "    return 1\n", "def beta():\n", "    return 2\n"]


# --- bug 4: off-by-one occurrence -----------------------------------------

def test_occurrence_is_one_based():
    assert resolve_anchor(LINES, "return") == 2          # 1st match, not the 2nd
    assert resolve_anchor(LINES, "return", occurrence=2) == 4


def test_occurrence_out_of_range_raises_ambiguous():
    with pytest.raises(Ambiguous) as exc:
        resolve_anchor(LINES, "return", occurrence=3)
    assert exc.value.candidates == [(2, "return 1"), (4, "return 2")]


def test_single_match_out_of_range_is_not_called_ambiguous():
    with pytest.raises(AnchorError, match="out of range") as exc:
        resolve_anchor(LINES, "alpha", occurrence=2)
    assert not isinstance(exc.value, Ambiguous)


# --- bug 2/3: raise-only contract ----------------------------------------

def test_empty_needle_raises_with_message():
    with pytest.raises(AnchorError, match="empty"):
        resolve_anchor(LINES, "   ")


def test_missing_anchor_raises_with_context():
    with pytest.raises(AnchorError, match="gamma"):
        resolve_anchor(LINES, "gamma")


def test_resolvers_return_ints_not_errors():
    assert isinstance(resolve_anchor(LINES, "alpha"), int)


# --- bug 6: candidates are (line_number, stripped_text) tuples ------------

def test_candidates_are_stripped_tuples():
    with pytest.raises(Ambiguous) as exc:
        resolve_anchor(["  def a():\n", "  def b():\n"], "def", occurrence=9)
    assert exc.value.candidates == [(1, "def a():"), (2, "def b():")]
    assert all(isinstance(c, tuple) for c in exc.value.candidates)


# --- bug 1: resolve_span -------------------------------------------------

def test_span_matches_anchor_at_column_zero():
    # find() returns 0 here: the old truthiness check dropped it.
    assert resolve_span(LINES, "def alpha") == (1, 1)


def test_span_matches_anchor_mid_line():
    assert resolve_span(LINES, "return 2") == (4, 4)


def test_span_with_end_anchor():
    assert resolve_span(LINES, "def alpha", "return 1") == (1, 2)
    assert resolve_span(LINES, "def alpha", "return 2") == (1, 4)


def test_span_end_anchor_not_present_raises():
    with pytest.raises(AnchorError, match="End anchor"):
        resolve_span(LINES, "def alpha", "return 99")


def test_span_does_not_run_on_past_first_anchor():
    lines = ["x = 1\n", "x = 1\n", "x = 1\n"]
    assert resolve_span(lines, "x = 1") == (1, 1)
    assert resolve_span(lines, "x = 1", occurrence=3) == (3, 3)


# --- 1-based contract across apply_edit -----------------------------------

def test_apply_edit_uses_one_based_inclusive():
    # apply_edit strips the trailing newline off new_text before splitting, so the
    # replaced lines come back without a keepend; take_snapshot normalises on write.
    out = apply_edit(LINES, 3, 4, "def beta():\n    return 42")
    assert out == ["def alpha():\n", "    return 1\n", "def beta():", "    return 42"]
    assert take_snapshot(out) == "".join(LINES).replace("return 2", "return 42").encode()


def test_apply_edit_can_delete_lines():
    out = apply_edit(LINES, 2, 2, "")
    assert out == ["def alpha():\n", "def beta():\n", "    return 2\n"]


# --- bug 5: the edit branch actually edits -------------------------------

def _root(tmp_path: Path) -> Path:
    (tmp_path / "m.py").write_text("".join(LINES), encoding="utf-8")
    return tmp_path


def test_apply_action_edit_writes_the_file(tmp_path):
    root = _root(tmp_path)
    # new_text replaces the WHOLE matched line, so indentation must be included
    # by the caller -- the anchor locates the line, it does not merge text.
    diff = apply_action(root, FreeResponse(
        edit=EditAction(file_path="m.py", anchor="return 1", new_text="    return 99")
    ))
    assert (root / "m.py").read_text() == "".join(LINES).replace("return 1", "return 99")
    assert "-    return 1" in diff and "+    return 99" in diff


def test_apply_action_edit_span_and_occurrence(tmp_path):
    root = _root(tmp_path)
    apply_action(root, FreeResponse(
        edit=EditAction(
            file_path="m.py",
            anchor="def",
            end_anchor="return 1",
            new_text="def alpha():\n    return 7\n",
        )
    ))
    assert (root / "m.py").read_text() == "".join(LINES).replace("return 1", "return 7")


def test_apply_action_snapshots_before_writing(tmp_path):
    root = _root(tmp_path)
    apply_action(root, FreeResponse(
        edit=EditAction(file_path="m.py", anchor="return 1", new_text="return 99")
    ))
    backups = list((root / ".vc" / "undo").glob("*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text() == "".join(LINES)


def test_apply_action_ambiguous_edit_writes_nothing(tmp_path):
    root = _root(tmp_path)
    with pytest.raises(Ambiguous):
        apply_action(root, FreeResponse(
            edit=EditAction(file_path="m.py", anchor="return", occurrence=5, new_text="x")
        ))
    assert (root / "m.py").read_text() == "".join(LINES)
    assert not (root / ".vc" / "undo").exists()  # no snapshot for a failed resolve


def test_apply_action_write_path_still_works(tmp_path):
    root = _root(tmp_path)
    diff = apply_action(root, FreeResponse(write=FullWriteAction(file_path="n.py", content="print(1)\n")))
    assert (root / "n.py").read_text() == "print(1)\n"
    assert "+print(1)" in diff


def test_apply_action_write_refuses_existing_file(tmp_path):
    root = _root(tmp_path)
    with pytest.raises(AnchorError, match="already exists"):
        apply_action(root, FreeResponse(write=FullWriteAction(file_path="m.py", content="clobber")))
    assert (root / "m.py").read_text() == "".join(LINES)
    assert not (root / ".vc" / "undo").exists()


def test_gutter_copied_from_prompt_is_stripped(tmp_path):
    root = _root(tmp_path)
    apply_action(root, FreeResponse(
        edit=EditAction(file_path="m.py", anchor="return 1", new_text="    2 |     return 99")
    ))
    assert (root / "m.py").read_text() == "".join(LINES).replace("return 1", "return 99")


def test_gutter_stripped_from_new_file(tmp_path):
    root = _root(tmp_path)
    apply_action(root, FreeResponse(write=FullWriteAction(
        file_path="g.py", content="   1 | def a():\n   2 |     return 1\n")))
    assert (root / "g.py").read_text() == "def a():\n    return 1\n"


def test_gutter_in_anchor_is_stripped():
    # Anchor copied out of the prompt gutter still matches the real line.
    assert resolve_anchor(LINES, "   2 |     return 1") == 2
    assert resolve_span(LINES, "   1 | def alpha", "   2 |     return 1") == (1, 2)


def test_anchor_still_must_be_verbatim_after_strip():
    with pytest.raises(AnchorError, match="not found"):
        resolve_anchor(LINES, "   2 |     return 99")


def test_gutter_regex_spares_ordinary_code():
    out = apply_edit(LINES, 4, 4, "    x = a | b\n    s = '| 9 |'\n")
    assert out[3] == "    x = a | b"


def test_apply_action_rejects_paths_outside_root(tmp_path):
    root = _root(tmp_path)
    with pytest.raises(PermissionError):
        apply_action(root, FreeResponse(write=FullWriteAction(file_path="../evil.py", content="x")))


# --- render_diff ---------------------------------------------------------

def test_render_diff_has_newlines_between_headers():
    d = render_diff(["a\n"], ["b\n"])
    assert d.startswith("--- \n+++ \n@@")
    assert "-a\n+b\n" in d


def test_render_diff_accepts_lines_without_newlines():
    assert "-a\n" in render_diff(["a"], ["b"])


def test_take_snapshot_does_not_double_newlines():
    assert take_snapshot(["a\n", "b\n"]) == b"a\nb\n"