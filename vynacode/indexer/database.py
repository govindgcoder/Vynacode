import sqlite3


def init_db(db_path: Path):
    with sqlite3.connect(db_path) as con:
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
    FOREIGN KEY (parent_file) REFERENCES files (path) ON  DELETE CASCADE,
    chunk_boundary INTEGER NOT NULL DEFAULT 0, 
    FOREIGN KEY (parent_id) REFERENCES blocks (id) ON DELETE SET NULL
    );

                    """)


def upsert_file_metadata(db_path: Path, metadata: FileMetaData):
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO files (path, hash, language, size_bytes) VALUES (?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET hash = excluded.hash, language = excluded.language, size_bytes = excluded.size_bytes",
            (str(metadata.path), metadata.hash, metadata.language, metadata.size_bytes),
        )
