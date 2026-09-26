import sqlite3
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import List
from schema import Block, FileMetaData
from config import TOKEN_BUDGET

def init_db(db_path: Path):
    with sqlite3.connect(str(db_path)) as con:
        con.execute("PRAGMA foreign_keys = ON;")
        con.execute(
            (
                """CREATE TABLE IF NOT EXISTS files (
        name TEXT,
        path TEXT PRIMARY KEY,
        language TEXT,
        size_bytes INTEGER,
        hash TEXT,
        imports JSON
        )"""
            )
        )
        con.execute("""
                    CREATE TABLE IF NOT EXISTS blocks (
    id TEXT PRIMARY KEY,
    name TEXT,
    type TEXT,
    params TEXT,
    returns TEXT,
    line_range_start INTEGER,
    line_range_end INTEGER,
    dependencies TEXT,
    summary TEXT,
    byte_start INTEGER NOT NULL,
    byte_end INTEGER NOT NULL,
    parent_id TEXT,
    is_async INTEGER NOT NULL DEFAULT 0, 
    parent_file TEXT,
    chunk_boundary INTEGER NOT NULL DEFAULT 0, 
    FOREIGN KEY (parent_file) REFERENCES files (path) ON  DELETE CASCADE
    );

                    """)
        con.executescript("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(
                        id UNINDEXED,
                        name,
                        summary,
                        parent_file,
                        content='blocks',
                        content_rowid='rowid'
                    );
                    CREATE TRIGGER IF NOT EXISTS blocks_ai AFTER INSERT ON blocks BEGIN
                        INSERT INTO blocks_fts (rowid, id, name, summary, parent_file)
                        VALUES (new.rowid, new.id, new.name, new.summary, new.parent_file);
                    END;
                    CREATE TRIGGER IF NOT EXISTS blocks_ad AFTER DELETE ON blocks BEGIN
                        INSERT INTO blocks_fts (blocks_fts, rowid, id, name, summary, parent_file)
                        VALUES ('delete', old.rowid, old.id, old.name, old.summary, old.parent_file);
                    END;
                    CREATE TRIGGER IF NOT EXISTS blocks_au AFTER UPDATE ON blocks BEGIN
                        INSERT INTO blocks_fts (blocks_fts, rowid, id, name, summary, parent_file)
                        VALUES ('delete', old.rowid, old.id, old.name, old.summary, old.parent_file);
                        INSERT INTO blocks_fts (rowid, id, name, summary, parent_file)
                        VALUES (new.rowid, new.id, new.name, new.summary, new.parent_file);
                    END;
                    """)

def _fts_term(query: str) -> str:
    """Quote a raw term as a single FTS5 token, with prefix matching enabled.

    FTS5 treats . + ` ( ) * and - as query syntax, so an unquoted keyword such
    as `user.id` or `C++` raises OperationalError instead of returning nothing.
    Doubling embedded quotes is required: a bare `a"b` is an unterminated
    string. The trailing * is prefix search, kept outside the closing quote so
    it is a prefix query rather than part of the phrase.
    """
    query = query.strip()
    if not query:
        return ""
    return '"' + query.replace('"', '""') + '"*'


def search_blocks(db_path: Path, query: str) -> List[dict]:
    """Search blocks using FTS5 with BM25 scoring."""
    term = _fts_term(query)
    if not term:
        return []
    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        cursor = con.execute(
            """
            SELECT b.*
            FROM blocks b
            JOIN blocks_fts f ON b.id = f.id
            WHERE blocks_fts MATCH ?
            ORDER BY bm25(blocks_fts)
            """,
            (term,)
        )
        return [dict(row) for row in cursor.fetchall()]


def _block_result(block: dict) -> dict:
    """Read a block's byte range and return every metadata field plus its code."""
    try:
        with open(block['parent_file'], 'rb') as f:
            f.seek(block['byte_start'])
            code = f.read(block['byte_end'] - block['byte_start']).decode('utf-8', errors='replace')
    except OSError:
        code = ''
    return {
        # Pass the whole row through so search_code never silently drops a
        # metadata field again; only params/is_async need retyping.
        **block,
        'is_async': bool(block['is_async']),
        'params': json.loads(block['params']) if block['params'] else [],
        'dependencies': json.loads(block['dependencies']) if block['dependencies'] else [],
        'code': code,
        'token_estimate': (block['byte_end'] - block['byte_start']) // 3,
    }


def resolve_all_dependencies(db_path: Path):
    """Fill blocks.dependencies with the other block names each block references.

    Runs as a second pass over the finished index so resolution is
    order-independent: a block indexed first must still see blocks from files
    parsed later. Records direct references only, so nothing here implies a
    transitive walk.
    """
    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "SELECT id, name, parent_file, byte_start, byte_end FROM blocks")]
        names = {r['name'] for r in rows}
        by_file = defaultdict(list)
        for r in rows:
            by_file[r['parent_file']].append(r)

        updates = []
        for r in rows:
            try:
                with open(r['parent_file'], 'rb') as f:
                    f.seek(r['byte_start'])
                    src = f.read(r['byte_end'] - r['byte_start']).decode('utf-8', errors='replace')
            except OSError:
                continue
            # \b keeps a block named `run` from matching inside `runner`.
            found = (names & set(re.findall(r"\b\w+\b", src))) - {r['name']}
            # A block nested inside this one (a class naming its own methods) is
            # already present in its code, so counting it as a dependency would
            # re-send code the caller already has.
            found -= {
                o['name'] for o in by_file[r['parent_file']]
                if o['name'] != r['name'] and r['byte_start'] <= o['byte_start'] < r['byte_end']
            }
            updates.append((json.dumps(sorted(found)), r['id']))
        con.executemany("UPDATE blocks SET dependencies = ? WHERE id = ?", updates)


def search_code(db_path: Path, query: str, token_budget: int = TOKEN_BUDGET) -> List[dict]:
    """Search blocks and retrieve byte-accurate source code within a token budget.

    search_blocks yields BM25 rank order, so exhausting the budget degrades to
    "the best N matches" rather than an arbitrary subset. The budget is only
    enforced once one block is in hand, because a caller holding an empty
    context patches blind.
    """
    blocks = search_blocks(db_path, query)
    results = []
    tokens_used = 0
    for block in blocks:
        est_tokens = (block['byte_end'] - block['byte_start']) // 3
        if results and tokens_used + est_tokens > token_budget:
            break
        result = _block_result(block)
        tokens_used += est_tokens
        results.append(result)

    have = {r['id'] for r in results}
    dep_names = []
    for r in results:
        for name in r['dependencies']:
            if name not in have:
                have.add(name)
                dep_names.append(name)
    if dep_names:
        marks = ",".join("?" * len(dep_names))
        with sqlite3.connect(str(db_path)) as con:
            con.row_factory = sqlite3.Row
            dep_rows = [dict(x) for x in con.execute(
                f"SELECT * FROM blocks WHERE name IN ({marks})", dep_names)]
        for row in dep_rows:
            if row['id'] in have:
                continue
            est_tokens = (row['byte_end'] - row['byte_start']) // 3
            if results and tokens_used + est_tokens > token_budget:
                break
            result = _block_result(row)
            tokens_used += est_tokens
            results.append(result)
    return results

def upsert_file_metadata(db_path: Path, metadata: FileMetaData):
    with sqlite3.connect(str(db_path)) as con:
        # Per-connection and OFF by default in SQLite. init_db sets it, but the
        # pragma is not inherited, so every connection that writes must re-enable
        # it or blocks can be inserted against a files row that does not exist.
        con.execute("PRAGMA foreign_keys = ON;")
        con.execute(
            "INSERT INTO files (path, hash, language, size_bytes) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET hash = excluded.hash, language = excluded.language, size_bytes = excluded.size_bytes",
            (str(metadata.path), metadata.hash, metadata.language, metadata.size_bytes),
        )

def delete_blocks_for_file(db_path: Path, parent_file: str):
    """Remove stale blocks for a file before re-inserting updated ones."""
    with sqlite3.connect(str(db_path)) as con:
        con.execute("PRAGMA foreign_keys = ON;")
        con.execute("DELETE FROM blocks WHERE parent_file = ?", (parent_file,))

def upsert_block(db_path: Path, parent_file: str, blocks: List[Block]):
    with sqlite3.connect(str(db_path)) as con:
        # Load-bearing: blocks.parent_file references files(path) ON DELETE CASCADE.
        # Without this the FK is unenforced, which is how orphaned blocks for
        # deleted files accumulated. upsert_file_metadata must run first.
        con.execute("PRAGMA foreign_keys = ON;")
        for block in blocks:
            params_json = json.dumps(
                [p.model_dump() for p in block.params] if block.params else []
            )
            deps_json = json.dumps(block.dependencies if block.dependencies else [])
            con.execute(
                """INSERT INTO blocks (id, name, type, params, returns, line_range_start, line_range_end, dependencies, summary, byte_start, byte_end, parent_id, is_async, parent_file, chunk_boundary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       name=excluded.name,
                       type=excluded.type,
                       params=excluded.params,
                       returns=excluded.returns,
                       line_range_start=excluded.line_range_start,
                       line_range_end=excluded.line_range_end,
                       dependencies=excluded.dependencies,
                       summary=excluded.summary,
                       byte_start=excluded.byte_start,
                       byte_end=excluded.byte_end,
                       parent_id=excluded.parent_id,
                       is_async=excluded.is_async,
                       parent_file=excluded.parent_file,
                       chunk_boundary=excluded.chunk_boundary""",
                (block.id, block.name, block.type, params_json, block.returns, block.line_range[0], block.line_range[1], deps_json, block.summary, block.byte_start, block.byte_end, block.parent_id, block.is_async, block.parent_file, block.chunk_boundary),
            )

def write_codebase_json(db_path: Path, codebase_json: Path):
    with sqlite3.connect(str(db_path)) as con:
        rows = con.execute(
            "SELECT files.path, blocks.name, blocks.summary, blocks.line_range_start, blocks.line_range_end "
            "FROM files "
            "INNER JOIN blocks ON files.path = blocks.parent_file "
            "ORDER BY files.path, blocks.line_range_start"
        ).fetchall()
        codebase_data = defaultdict(list)
        for file_path, block_name, summary, line_range_start, line_range_end in rows:
            codebase_data[file_path].append({
                "name": block_name,
                "line_start": line_range_start,
                "line_end": line_range_end,
                "summary": summary
            })
        with open(codebase_json, "w", encoding="utf-8") as f:
            json.dump(codebase_data, f, indent=4, ensure_ascii=False)

def get_db_paths(db_path: Path):
    with sqlite3.connect(str(db_path)) as con:
        rows = con.execute("SELECT path FROM files").fetchall()
    return {row[0] for row in rows} if rows else set()

def get_file_hashes(db_path: Path) -> dict[str, str]:
    """Return mapping path -> hash for incremental skip."""
    with sqlite3.connect(str(db_path)) as con:
        rows = con.execute("SELECT path, hash FROM files").fetchall()
    return {row[0]: row[1] for row in rows} if rows else {}


def ensure_indexed(db_path: Path) -> bool:
    """Check if the index exists."""
    return db_path.exists()

def prune_deleted_files(db_path: Path, codebase_json: Path, stale_paths):
    if not stale_paths:
        return
    with sqlite3.connect(str(db_path)) as con:
        con.execute("PRAGMA foreign_keys = ON;")
        for p in stale_paths:
            con.execute("DELETE FROM files WHERE path=?", (str(p),))
        con.commit()

    try:
        with open(codebase_json, "r", encoding="utf-8") as f:
            codebase_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return

    changed = False
    for p in stale_paths:
        # JSON keys are stored as strings; pop both str and original form
        if codebase_data.pop(str(p), None) is not None:
            changed = True

    if changed:
        with open(codebase_json, "w", encoding="utf-8") as f:
            json.dump(codebase_data, f, indent=4, ensure_ascii=False)

    print(f"Pruned {len(stale_paths)} path(s): {sorted(str(p) for p in stale_paths)}")

