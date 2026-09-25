import sqlite3
import json
from collections import defaultdict
from pathlib import Path
from typing import List
from schema import Block, FileMetaData

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

def search_blocks(db_path: Path, query: str) -> List[dict]:
    """Search blocks using FTS5 with BM25 scoring."""
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
            (query,)
        )
        return [dict(row) for row in cursor.fetchall()]


def search_code(db_path: Path, query: str) -> List[dict]:
    """Search blocks and retrieve actual source code using line numbers."""
    blocks = search_blocks(db_path, query)
    # YAGNI: if no matches, fallback to all blocks so small projects don't get 0 results
    if not blocks:
        with sqlite3.connect(str(db_path)) as con:
            con.row_factory = sqlite3.Row
            blocks = [dict(r) for r in con.execute("SELECT * FROM blocks LIMIT 20").fetchall()]
    results = []
    file_cache = {}
    for block in blocks:
        file_path = block['parent_file']
        if file_path not in file_cache:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    file_cache[file_path] = f.readlines()
            except (FileNotFoundError, OSError):
                file_cache[file_path] = None
        lines = file_cache.get(file_path)
        if lines:
            # line_range is 0-indexed from tree-sitter; handle both 0 and 1-indexed
            start = max(0, block['line_range_start'] - 1) if block['line_range_start'] > 0 else 0
            end = block['line_range_end']
            # if end is 0 (empty function), fallback to byte range or full file
            if end <= start:
                # use byte range as fallback
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    code = content[block['byte_start']:block['byte_end']] if block['byte_start'] < len(content) else ''.join(lines[start:start+10])
                except (OSError, UnicodeDecodeError):
                    code = ''.join(lines[start:start+10]) if start < len(lines) else ''
            else:
                code = ''.join(lines[start:end]) if start < len(lines) else ''
        else:
            code = ''
        results.append({
            'parent_file': block['parent_file'],
            'name': block['name'],
            'type': block['type'],
            'line_range_start': block['line_range_start'],
            'line_range_end': block['line_range_end'],
            'summary': block['summary'],
            'code': code
        })
    return results

def upsert_file_metadata(db_path: Path, metadata: FileMetaData):
    with sqlite3.connect(str(db_path)) as con:
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
    # YAGNI: no-op if nothing stale; avoids rewriting JSON needlessly
    if not stale_paths:
        return
    # 1. Delete from DB with FK cascade (removes blocks + FTS entries via triggers)
    with sqlite3.connect(str(db_path)) as con:
        con.execute("PRAGMA foreign_keys = ON;")
        for p in stale_paths:
            con.execute("DELETE FROM files WHERE path=?", (str(p),))
        con.commit()

    # 2. Remove from codebase.json — tolerate missing/corrupt file
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

