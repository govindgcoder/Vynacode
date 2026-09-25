# SPDX-License-Identifier: AGPL-3.0-only
import asyncio
import json
import sys
from pathlib import Path
from typing import List

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "backend"))
sys.path.insert(0, str(_ROOT))

import typer
from rich.console import Console
from rich.prompt import Confirm
from pydantic import ValidationError

from database import (
    init_db, upsert_file_metadata, upsert_block, delete_blocks_for_file,
    write_codebase_json, search_blocks, search_code, get_file_hashes,
    prune_deleted_files, ensure_indexed
)
from walker import walk
from parser import parse_python_file
from client import OllamaClient
from summarizer import summarize_and_store
from schema import DoResponse, PlanResponse

from vynacode.config import CODER_MODEL, PLANNER_MODEL, OLLAMA_URL

app = typer.Typer()
console = Console()
llm = OllamaClient(base_url=OLLAMA_URL)

MAX_RETRIES = 3
PATCH_HINT = (
    "Patch must be valid unified diff with headers. Example:\n"
    "--- a/path/to/file.py\n"
    "+++ b/path/to/file.py\n"
    "@@ -1,3 +1,4 @@\n"
    " def foo():\n"
    "+    \"\"\"Add docstring.\"\"\"\n"
    "     pass\n"
)

async def _llm_json_with_retry(model: str, prompt: str, validator, max_retries: int = MAX_RETRIES):
    """Call LLM with format=json and retry on validation failure."""
    raw_json = ""
    for attempt in range(max_retries):
        try:
            raw_json = await llm.complete(model, "user", prompt, format="json")
        except Exception as e:
            console.print(f"[red]LLM call failed (attempt {attempt + 1}/{max_retries}): {e}[/red]")
            if attempt < max_retries - 1:
                console.print("[yellow]Retrying...[/yellow]")
                await asyncio.sleep(1)
                continue
            return None
        try:
            return validator(raw_json)
        except ValidationError as e:
            console.print(f"[yellow]Validation error (attempt {attempt + 1}/{max_retries}):[/yellow] {e}")
            if attempt < max_retries - 1:
                console.print("[yellow]Retrying...[/yellow]")
                prompt += f"\n\nPREVIOUS ERROR: {e}\nFix the JSON structure and try again."
        except Exception as e:
            console.print(f"[red]Parse error: {e}[/red]")
            if attempt < max_retries - 1:
                console.print("[yellow]Retrying...[/yellow]")                                                                                                     
                prompt += f"\n\nPREVIOUS ERROR: {e}\nFix the JSON and try again."
    console.print(f"[red]Failed after {max_retries} attempts[/red]")
    if raw_json:
        console.print(f"[dim]Raw output:[/dim]\n{raw_json[:2000]}")
    return None

def _find_index_root(start: Path) -> Path | None:
    """Walk up from start looking for .vc/vcdb.db — YAGNI: no config needed."""
    for p in [start, *start.parents]:
        if (p / ".vc" / "vcdb.db").exists():
            return p
        if (p / "codebase.json").exists():
            return p
    return None

def _execute_coder_result(res: DoResponse):
    """Print-only: display proposed patches/commands, do not apply or execute."""
    console.print(f"\n[bold cyan]Thought:[/bold cyan] {res.thought}")
    if res.write:
        for action in res.write:
            console.print(f"\n[bold yellow]Proposed patch for {action.file_path}:[/bold yellow]")
            console.print(action.patch)
    if res.run:
        for action in res.run:
            console.print(f"\n[bold magenta]Proposed command: {action.command}[/bold magenta]")
    console.print(f"\n[bold green]Response:[/bold green] {res.response}")

async def _run_coder(db_path: Path, keywords: List[str], task: str):
    console.print("[dim]Retrieving context (code)...[/dim]")
    all_blocks = []
    for kw in keywords:
        blocks = search_code(db_path, kw)
        all_blocks.extend(blocks)

    unique_blocks = {}
    for b in all_blocks:
        key = (b['parent_file'], b['line_range_start'], b['line_range_end'])
        if key not in unique_blocks:
            unique_blocks[key] = b

    console.print(f"[cyan]Retrieved {len(unique_blocks)} unique blocks from {len(set(b['parent_file'] for b in unique_blocks.values()))} files[/cyan]")

    context_str = ""
    for b in unique_blocks.values():
        context_str += f"--- File: {b['parent_file']} | Range: {b['line_range_start']}-{b['line_range_end']} ---\n"
        context_str += f"Summary: {b['summary']}\n"
        if b.get('code'):
            context_str += f"Code:\n```python\n{b['code']}```\n"
        context_str += "\n"

    prompt = (
        f"Context:\n{context_str}\n\n"
        f"Task: {task}\n\n"
        f"{PATCH_HINT}\n"
        "Output EXACTLY this JSON structure:\n"
        '{"thought": "your reasoning", "response": "summary string for user", '
        '"write": [{"file_path": "absolute path", "patch": "unified diff with ---/+++ headers"}], '
        '"run": [{"command": "bash cmd"}]}'
        "All fields required. response must be a STRING, not an object. "
        "If no patches/commands, use empty lists. No extra keys. No fluff."
    )

    console.print("[dim]Thinking...[/dim]")
    res = await _llm_json_with_retry(CODER_MODEL, prompt, DoResponse.model_validate_json)
    if res:
        _execute_coder_result(res)
    else:
        console.print("[red]Coder failed to produce valid JSON[/red]")

async def _run_multi(db_path: Path, keywords: List[str], task: str):
    console.print("[dim]Retrieving context (summaries)...[/dim]")
    all_blocks = []
    for kw in keywords:
        blocks = search_blocks(db_path, kw)
        all_blocks.extend(blocks)

    unique_blocks = {}
    for b in all_blocks:
        key = (b['parent_file'], b['line_range_start'], b['line_range_end'])
        if key not in unique_blocks:
            unique_blocks[key] = b

    console.print(f"[cyan]Retrieved {len(unique_blocks)} unique blocks from {len(set(b['parent_file'] for b in unique_blocks.values()))} files[/cyan]")

    summary_str = ""
    for b in unique_blocks.values():
        summary_str += f"- {b['parent_file']}:{b['name']} ({b['line_range_start']}-{b['line_range_end']}): {b['summary']}\n"

    root = _find_index_root(Path.cwd().resolve())
    plan_path = root / "last_plan.json" if root else None
    plan: PlanResponse | None = None

    if plan_path and plan_path.exists():
        use_prev = await asyncio.to_thread(Confirm.ask, "Found a previous plan. Load it?", default=False)
        if use_prev:
            try:
                with plan_path.open("r", encoding="utf-8") as f:
                    plan = PlanResponse.model_validate(json.load(f))
                console.print("[green]Loaded previous plan.[/green]")
            except Exception as e:
                console.print(f"[red]Failed to load plan: {e}[/red]")

    if not plan:
        planner_prompt = (
            f"Codebase summaries:\n{summary_str}\n\n"
            f"Task: {task}\n\n"
            "Output EXACTLY this JSON structure:\n"
            '{"steps": ["step 1 text", "step 2 text", "..."]}'
            "\n\nCRITICAL: steps MUST be an array of PLAIN STRINGS only. "
            "Each element is a detailed, self-contained instruction for a coder. "
            "NO objects, NO 'instruction' keys, NO 'description' keys, NO code blocks. "
            "Just plain text strings. Example: "
            '["Add docstring to function foo in file.py", "Fix bug in bar at line 42"]'
            "\nNo fluff. No extra keys. No objects."
        )

        console.print("[dim]Planning...[/dim]")
        plan = await _llm_json_with_retry(PLANNER_MODEL, planner_prompt, PlanResponse.model_validate_json)
        
        if not plan or not plan.steps:
            console.print("[yellow]Planner returned empty steps[/yellow]")
            return

        console.print(f"[green]New Plan ({len(plan.steps)} steps):[/green]")
        for i, step in enumerate(plan.steps, 1):
            console.print(f"  {i}. {step}")
            
        if plan_path:
            try:
                with plan_path.open("w", encoding="utf-8") as f:
                    json.dump(plan.model_dump(), f, indent=4, ensure_ascii=False)
            except OSError as e:
                console.print(f"[red]Error saving plan: {e}[/red]")

        confirmation = await asyncio.to_thread(Confirm.ask, "Proceed with this new plan?")
        if not confirmation:
            console.print("[yellow]Plan aborted. View it with: [bold]show plan[/bold][/yellow]")
            return
    else:
        # Plan was loaded
        console.print(f"[green]Loaded Plan ({len(plan.steps)} steps):[/green]")
        for i, step in enumerate(plan.steps, 1):
            console.print(f"  {i}. {step}")
        
        confirmation = await asyncio.to_thread(Confirm.ask, "Proceed with loaded plan?")
        if not confirmation:
            console.print("[yellow]Plan aborted. View it with: [bold]show plan[/bold][/yellow]")
            return

    # Execution loop
    for i, step in enumerate(plan.steps, 1):
        console.print(f"\n[dim]Step {i}/{len(plan.steps)}: {step}[/dim]")
        step_kws = [w.strip('.,') for w in step.lower().split() if len(w) > 3][:4]
        step_blocks = []
        for kw in step_kws:
            step_blocks.extend(search_code(db_path, kw))
        if not step_blocks:
            fallback_blocks = list(unique_blocks.values())
            step_blocks = fallback_blocks[:3]
            
        step_context = ""
        seen = set()
        for b in step_blocks:
            key = (b['parent_file'], b['line_range_start'])
            if key in seen:
                continue
            seen.add(key)
            step_context += f"--- File: {b['parent_file']} | Range: {b['line_range_start']}-{b['line_range_end']} ---\n"
            step_context += f"Summary: {b['summary']}\n"
            if b.get('code'):
                step_context += f"Code:\n```python\n{b['code']}```\n"
            step_context += "\n"
            if len(step_context) > 4000:
                break
        if not step_context.strip():
            step_context = summary_str

        coder_prompt = (
            f"Context:\n{step_context}\n\n"
            f"Task: {step}\n\n"
            f"{PATCH_HINT}\n"
            "Output EXACTLY this JSON structure:\n"
            '{"thought": "your reasoning", "response": "summary string for user", '
            '"write": [{"file_path": "absolute path", "patch": "unified diff with ---/+++ headers"}], '
            '"run": [{"command": "bash cmd"}]}'
            "All fields required. response must be a STRING, not an object. "
            "If no patches/commands, use empty lists. No extra keys. No fluff."
        )
        res = await _llm_json_with_retry(CODER_MODEL, coder_prompt, DoResponse.model_validate_json)
        if res:
            _execute_coder_result(res)


def view_plan(path: Path):
    try:
        with path.open("r", encoding="utf-8") as f:
            plan = PlanResponse.model_validate(json.load(f))
            if not plan.steps:
                return
            console.print(f"[green]Plan ({len(plan.steps)} steps):[/green]")
            for i, step in enumerate(plan.steps, 1):
                console.print(f"  {i}. {step}")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        console.print("[yellow]No valid previous plan found.[/yellow]")

async def _run_index_pipeline(dir_path: Path):
    db_path = dir_path / ".vc" / "vcdb.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(db_path)
    stored_hashes = get_file_hashes(db_path)
    db_paths = set(stored_hashes.keys())
    active_paths: set[str] = set()

    async def _run_pipeline():
        for file_metadata in walk(dir_path):
            str_path = str(file_metadata.path)
            active_paths.add(str_path)
            if stored_hashes.get(str_path) == file_metadata.hash:
                continue
            upsert_file_metadata(db_path, file_metadata)
            if file_metadata.language == ".py":
                if str_path in stored_hashes:
                    delete_blocks_for_file(db_path, str_path)
                blocks = parse_python_file(file_metadata.path)
                upsert_block(db_path, str_path, blocks)
                with open(file_metadata.path, "rb") as f:
                    file_bytes = f.read()
                await summarize_and_store(llm, db_path, blocks, source_code=file_bytes)
        write_codebase_json(db_path, dir_path / "codebase.json")

    await _run_pipeline()
    stale_paths = db_paths - active_paths
    prune_deleted_files(db_path, dir_path / "codebase.json", stale_paths)
    console.print("[green]Indexing complete.[/green]")

@app.command()
def index(path: str):
    asyncio.run(_run_index_pipeline(Path(path).resolve()))

@app.command()
def do(
    message: str,
    mode: str = typer.Option("single", help="single (coder JSON) or multi (planner + coder JSON)"),
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
            keywords = await llm.expand_query(CODER_MODEL, message)
        except Exception as e:
            console.print(f"[red]Query expansion failed: {e}[/red]")
            import traceback as _tb
            _tb.print_exc()
            return
        console.print(f"[cyan]Keywords:[/cyan] {', '.join(keywords)}")

        if mode == "single":
            await _run_coder(db_path, keywords, message)
        elif mode == "multi":
            await _run_multi(db_path, keywords, message)
        else:
            console.print(f"[red]Unknown mode: {mode}. Use 'single' or 'multi'[/red]")

        # Re-index
        console.print("[dim]Re-indexing codebase...[/dim]")
        await _run_index_pipeline(root)

    try:
        asyncio.run(_run_do())
    except Exception as e:
        console.print(f"[red]Fatal error: {e}[/red]")
        import traceback as _tb
        _tb.print_exc()

@app.command()
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
                console.print("No results found!")
            for i, block in enumerate(results, 1):
                console.print(f"\n[bold]Result {i}:[/bold] {block['name']} in {block['parent_file']}")
                console.print(f"  Lines: {block['line_range_start']}-{block['line_range_end']}")
                console.print(f"  Summary: {block['summary']}")
        else:
            console.print("Error: query is required.")
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
            console.print("[yellow]No codebase.json found.[/yellow]")")

@app.command()
def log():
    console.print("Log! to be implemented")

@app.command()
def models():
    console.print(f"Active model: {CODER_MODEL}")

@app.command()
def doctor():
    console.print("Doctor! to be implemented")

if __name__ == "__main__":
    app()
