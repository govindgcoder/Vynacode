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
    FOREIGN KEY (parent_file) REFERENCES files (path) ON  DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES blocks (id) ON DELETE SET NULL
    );

                    """)


def upsert_file_metadata(db_path: Path, metadata: FileMetaData):
    with sqlite3.connect(str(db_path)) as con:
        con.execute(
            "INSERT INTO files (path, hash, language, size_bytes) VALUES (?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET hash = excluded.hash, language = excluded.language, size_bytes = excluded.size_bytes",
            (str(metadata.path), metadata.hash, metadata.language, metadata.size_bytes),
        )

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

def write_codebase_json(db_path: Path, codebase_json: str):
    with sqlite3.connect(str(db_path)) as con:
        rows = con.execute("SELECT files.path, blocks.name, blocks.summary FROM files JOIN blocks ON files.path = blocks.parent_file ").fetchall()
        codebase_data = defaultdict(list)
        for file_path, block_name, summary in rows:
            codebase_data[file_path].append({
                "name": block_name,
                "summary": summary
            })
        with open(codebase_json, "w", encoding="utf-8") as f:
            json.dump(codebase_data, f, indent=4, ensure_ascii=False)


