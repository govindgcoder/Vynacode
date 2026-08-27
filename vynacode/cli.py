# SPDX-License-Identifier: AGPL-3.0-only OR Commercial
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "backend"))
sys.path.insert(0, str(_ROOT))

import typer
from database import init_db, upsert_file_metadata, upsert_block, write_codebase_json
from walker import walk
from parser import parse_python_file
from client import OllamaClient
from summarizer import summarize_and_store

app = typer.Typer()

llm = OllamaClient()

@app.command()
def index(path: str):
    dir_path = Path(path).resolve()
    db_path = dir_path / ".vc" / "vcdb.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(db_path)

    async def _run_pipeline():
        for file_metadata in walk(dir_path):
            upsert_file_metadata(db_path, file_metadata)
            if file_metadata.language == ".py":
                blocks = parse_python_file(file_metadata.path)
                upsert_block(db_path, str(file_metadata.path), blocks)
                with open(file_metadata.path, "rb") as f:
                    file_bytes = f.read()
                await summarize_and_store(llm, db_path, blocks, file_bytes)
        write_codebase_json(db_path, dir_path / "codebase.json")

    asyncio.run(_run_pipeline())

@app.command()
def run(
    message: str,
    mode_name: str = "single",
    keep_loaded=False,
    context_window=None,
    no_approval=False,
    max_heal=4,
):
    print("Running! to be implemented")
    pass


@app.command()
def show(option: str = "plan"):
    if option == "plan":
        print("Show plan to be implemented")
    elif option == "context":
        print("Show context to be implemented")
    elif option == "index":
        print("Show index to be implemented")
    else:
        print(f"Invalid option {option}")
    pass


@app.command()
def log():
    print("Log! to be implemented")
    pass


@app.command()
def models():
    print("Active models: ..... [to be implemented]")
    pass


@app.command()
def doctor():
    print("Doctor! to be implemented")
    pass


if __name__ == "__main__":
    app()
