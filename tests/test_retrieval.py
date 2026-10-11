"""Retrieval-layer regressions. Run: .venv/bin/pytest tests/test_retrieval.py -v"""
import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Add the repo root so vynacode package is importable
sys.path.insert(0, str(ROOT))

from vynacode.backend.parser import parse_file
from vynacode.backend.database import (
    get_paths_by_basename, init_db, load_code, search_blocks,
)
from vynacode.cli import (
    _correct_file_path, _estimate_block, _fuse, _pack_budget, _real_lines,
)


def _block(est_tokens: int, name: str, line: int = 1,
           parent_file: str = "/repo/f.py", block_id: str = "") -> dict:
    """A metadata row whose _estimate_block lands on est_tokens.

    est = (bytes + lines*8)//3, one line -> bytes = est*3 - 8.
    """
    return {
        "id": block_id or f"{name}:{line}",
        "name": name,
        "parent_file": parent_file,
        "line_range_start": line,
        "line_range_end": line,
        "byte_start": 0,
        "byte_end": est_tokens * 3 - 8,
        "dependencies": "[]",
        "summary": "",
        "code": "",
    }


# --- task 1: _pack_budget context nuke -------------------------------------
# The traced failure: a no-op eviction frees less than the overflow (dropped
# 56 tok, still 4 short), the post-append guard fired, and the whole packed
# context — including the 1422-tok target — was replaced by the one small
# newcomer. Reproduced here with the same numbers.

def test_noop_eviction_keeps_packed_context():
    budget = 2048
    target = _block(1422, "_llm_json_with_retry")
    small_a = _block(570, "FullWriteAction")
    small_b = _block(56, "FileMetaData")
    arrival = _block(60, "SystemStats", line=99)
    packed = _pack_budget([target, small_a, small_b, arrival], budget)
    assert target in packed, "target block was wiped by the overflow guard"
    assert arrival in packed
    assert len(packed) == 3


def test_oversized_block_stands_alone():
    big = _block(2049, "huge")
    small = _block(50, "tiny", line=5)
    packed = _pack_budget([small, big], budget=2048)
    assert packed == [big]


def test_estimate_uses_bytes_not_code():
    b = {"line_range_start": 10, "line_range_end": 19,
         "byte_start": 0, "byte_end": 300}
    assert _estimate_block(b) == (300 + 10 * 8) // 3


# --- task 2: exact-name boost survives first-seen --------------------------

def test_exact_name_boost_applies_later_keywords():
    def row(f, name, line):
        return {"parent_file": f, "line_range_start": line,
                "line_range_end": line, "name": name}
    # runner never equals a keyword here, so it only accumulates RRF.
    runner = row("/a.py", "runner", 1)
    target = row("/b.py", "target", 1)
    # "target" first surfaces under the keyword "run" (no boost there),
    # then reappears under its own name; it must still take the top slot.
    fused = _fuse([("run", [runner, target]), ("target", [runner, target])])
    assert fused[0]["name"] == "target"


def test_exact_name_boost_no_null_name_crash():
    b = {"parent_file": "/a.py", "line_range_start": 1,
         "line_range_end": 1, "name": None}
    assert _fuse([("x", [b])]) == [b]


# --- task 2/3: FTS migration unindexes parent_file -------------------------

_LEGACY_FTS = """
CREATE VIRTUAL TABLE blocks_fts USING fts5(
    id UNINDEXED, name, signature, summary, parent_file,
    content='blocks', content_rowid='rowid'
);
CREATE TRIGGER blocks_ai AFTER INSERT ON blocks BEGIN
    INSERT INTO blocks_fts (rowid, id, name, signature, summary, parent_file)
    VALUES (new.rowid, new.id, new.name, new.signature, new.summary, new.parent_file);
END;
"""


def _legacy_db(tmp_path: Path) -> Path:
    """A v1 database: parent_file is a real FTS column, user_version 0."""
    db = tmp_path / "vcdb.db"
    with sqlite3.connect(str(db)) as con:
        con.execute(
            "CREATE TABLE files (name TEXT, path TEXT PRIMARY KEY,"
            " language TEXT, size_bytes INTEGER, hash TEXT, imports JSON)"
        )
        con.execute(
            "CREATE TABLE blocks (id TEXT PRIMARY KEY, name TEXT, type TEXT,"
            " signature TEXT, line_range_start INTEGER, line_range_end INTEGER,"
            " dependencies TEXT, summary TEXT, byte_start INTEGER NOT NULL,"
            " byte_end INTEGER NOT NULL, parent_id TEXT, parent_file TEXT,"
            " chunk_boundary INTEGER NOT NULL DEFAULT 0,"
            " FOREIGN KEY (parent_file) REFERENCES files (path)"
            " ON DELETE CASCADE)"
        )
        con.executescript(_LEGACY_FTS)
        con.execute(
            "INSERT INTO files (path, hash, language, size_bytes)"
            " VALUES ('/repo/vynacode/cli.py', 'h', '.py', 100)"
        )
        con.execute(
            "INSERT INTO blocks (id, name, type, signature, line_range_start,"
            " line_range_end, dependencies, summary, byte_start, byte_end,"
            " parent_file) VALUES ('b1', 'MAX_RETRIES', 'module',"
            " 'MAX_RETRIES = 3', 50, 50, '[]', 'retry budget constant', 0,"
            " 18, '/repo/vynacode/cli.py')"
        )
    return db


def test_migration_unindexes_parent_file(tmp_path: Path):
    db = _legacy_db(tmp_path)
    # Poisoned state: the path fragment matches via parent_file alone.
    assert search_blocks(db, "vynacode")
    init_db(db)
    with sqlite3.connect(str(db)) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 2
        fts_sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE name='blocks_fts'"
        ).fetchone()[0]
    assert "parent_file UNINDEXED" in fts_sql
    # Path words no longer retrieve; content words still do.
    assert search_blocks(db, "vynacode") == []
    hits = search_blocks(db, "MAX_RETRIES")
    assert [h["id"] for h in hits] == ["b1"]
    assert search_blocks(db, "retry") == [hits[0]]


def test_migration_is_idempotent(tmp_path: Path):
    db = _legacy_db(tmp_path)
    init_db(db)
    init_db(db)  # second run: user_version already current, no rebuild
    with sqlite3.connect(str(db)) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 2
    assert [h["id"] for h in search_blocks(db, "MAX_RETRIES")] == ["b1"]


# --- task 3: load_code materialises after packing --------------------------

def test_load_code_reads_bytes_and_dependencies(tmp_path: Path):
    src = tmp_path / "lib.py"
    src.write_text("def helper():\n    return 42\n\ndef main():\n    helper()\n")
    db = tmp_path / "vcdb.db"
    init_db(db)
    with sqlite3.connect(str(db)) as con:
        con.execute("PRAGMA foreign_keys = ON;")
        con.execute(
            "INSERT INTO files (path, hash, language, size_bytes)"
            " VALUES (?, 'h', '.py', 100)", (str(src),)
        )
        text = src.read_text()
        main_start = text.index("def main")
        helper_end = main_start - 1
        con.execute(
            "INSERT INTO blocks (id, name, type, signature, line_range_start,"
            " line_range_end, dependencies, summary, byte_start, byte_end,"
            " parent_file) VALUES ('m', 'main', 'function', 'def main', 4, 5,"
            " '[]', '', ?, ?, ?)", (main_start, len(text), str(src))
        )
        con.execute(
            "INSERT INTO blocks (id, name, type, signature, line_range_start,"
            " line_range_end, dependencies, summary, byte_start, byte_end,"
            " parent_file) VALUES ('h', 'helper', 'function', 'def helper', 1, 2,"
            " '[]', '', 0, ?, ?)", (helper_end, str(src))
        )
        # main references helper: give it the dependency, as resolve would.
        con.execute("UPDATE blocks SET dependencies = '[\"helper\"]' WHERE id = 'm'")
    rows = []
    with sqlite3.connect(str(db)) as con:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM blocks WHERE id = 'm'")]
    out = load_code(db, rows, budget=2000)
    assert [b["id"] for b in out] == ["m", "h"]
    assert out[0]["code"] == "def main():\n    helper()\n"
    assert out[1]["code"] == "def helper():\n    return 42\n"
    # Deps are appended only while the budget allows.
    out = load_code(db, rows, budget=1)
    assert [b["id"] for b in out] == ["m"]


# --- task 4: repair path reads disk, not FTS -------------------------------

def test_real_lines_reads_disk_not_fts(tmp_path: Path):
    root = tmp_path
    (root / ".vc").mkdir()
    db = root / ".vc" / "vcdb.db"
    init_db(db)
    f = root / "cli.py"
    f.write_text("MAX_RETRIES = 3\nAPP = 1\n")
    out = _real_lines(db, "cli.py")
    assert "    1 | MAX_RETRIES = 3" in out
    assert "    2 | APP = 1" in out
    # A missing file yields nothing rather than FTS fragments of another.
    assert _real_lines(db, "nope.py") == ""


def test_real_lines_windows_long_files(tmp_path: Path):
    root = tmp_path
    (root / ".vc").mkdir()
    db = root / ".vc" / "vcdb.db"
    init_db(db)
    f = root / "big.py"
    f.write_text("".join(f"x = {i}\n" for i in range(300)))
    out = _real_lines(db, "big.py")
    assert "  200 | x = 199" in out
    assert "  201 |" not in out


def test_correct_file_path_maps_basename(tmp_path: Path):
    root = tmp_path
    (root / ".vc").mkdir()
    db = root / ".vc" / "vcdb.db"
    init_db(db)
    real = root / "src" / "app.py"
    real.parent.mkdir()
    real.write_text("x = 1\n")
    with sqlite3.connect(str(db)) as con:
        con.execute(
            "INSERT INTO files (path, hash, language, size_bytes)"
            " VALUES (?, 'h', '.py', 5)", (str(real),)
        )
    # The model invented a directory; the unique basename wins.
    fixed = asyncio.run(_correct_file_path(db, root, "app/app.py"))
    assert fixed == "src/app.py"
    # Ambiguity: two files with the same basename -> no guess.
    other = root / "nested" / "app.py"
    other.parent.mkdir()
    other.write_text("y = 2\n")
    with sqlite3.connect(str(db)) as con:
        con.execute(
            "INSERT INTO files (path, hash, language, size_bytes)"
            " VALUES (?, 'h', '.py', 5)", (str(other),)
        )
    assert asyncio.run(_correct_file_path(db, root, "app/app.py")) is None


def test_get_paths_by_basename_escapes_wildcards(tmp_path: Path):
    db = tmp_path / "vcdb.db"
    init_db(db)
    with sqlite3.connect(str(db)) as con:
        for p in ("/r/a_b.py", "/r/axb.py", "/r/100%.py"):
            con.execute(
                "INSERT INTO files (path, hash, language, size_bytes)"
                " VALUES (?, 'h', '.py', 1)", (p,)
            )
    # '_' and '%' in a basename are LIKE wildcards; they must not match wider.
    assert get_paths_by_basename(db, "a_b.py") == ["/r/a_b.py"]
    assert get_paths_by_basename(db, "100%.py") == ["/r/100%.py"]


# --- task 5: module-level blocks cover the gaps ----------------------------

def test_module_block_covers_constants(tmp_path: Path):
    f = tmp_path / "app.py"
    f.write_text(
        "import os\n"
        "\n"
        "MAX_RETRIES = 3\n"
        "\n"
        "def go():\n"
        "    return MAX_RETRIES\n"
    )
    blocks = parse_file(f)
    kinds = {(b.type, b.name) for b in blocks}
    assert ("function", "go") in kinds
    mods = [b for b in blocks if b.type == "module"]
    assert mods, "uncovered span produced no module block"
    # The constant and import lines are inside a module block.
    covered = []
    for b in mods:
        covered.extend(range(b.line_range[0], b.line_range[1] + 1))
    assert 1 in covered and 3 in covered
    # The def's own lines belong to the function, not the module block.
    fn = next(b for b in blocks if b.name == "go")
    assert fn.line_range == (5, 6)
    assert 5 not in covered


def test_module_block_id_and_signature(tmp_path: Path):
    import hashlib
    f = tmp_path / "conf.py"
    f.write_text("LIMIT = 10\n")
    (b,) = parse_file(f)
    assert b.type == "module" and b.name == "conf"
    assert b.signature == "LIMIT = 10"
    expect = hashlib.sha256(f"{f}:module:1".encode()).hexdigest()
    assert b.id == expect


def test_module_blocks_chunk_long_gaps(tmp_path: Path):
    header = "".join(f"C{i} = {i}\n" for i in range(150))
    f = tmp_path / "bigmod.py"
    f.write_text(header + "def tail():\n    pass\n")
    mods = [b for b in parse_file(f) if b.type == "module"]
    assert len(mods) == 3  # 60 + 60 + 30 lines
    assert all(
        b.line_range[1] - b.line_range[0] + 1 <= 60 for b in mods
    )
    # Byte ranges stay contiguous and inside the file.
    text = f.read_text()
    for b in mods:
        assert b.byte_end <= len(text.encode())


def test_module_blocks_skip_blank_only_gaps(tmp_path: Path):
    f = tmp_path / "gap.py"
    f.write_text("def a():\n    pass\n\n\n\ndef b():\n    pass\n")
    blocks = parse_file(f)
    assert [b.type for b in blocks if b.type == "module"] == []


def test_module_block_for_fully_module_file(tmp_path: Path):
    f = tmp_path / "const.py"
    f.write_text("A = 1\nB = 2\n")
    (b,) = parse_file(f)
    assert b.type == "module"
    assert b.line_range == (1, 2)
    assert "A = 1" in b.signature or "B = 2" in b.signature
