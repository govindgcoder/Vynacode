# SPDX-License-Identifier: AGPL-3.0-only
import asyncio
import json
import re #for regex
import sys
from pathlib import Path
from typing import List, Optional, Tuple

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
    prune_deleted_files, ensure_indexed, resolve_all_dependencies
)
from walker import walk
from parser import parse_python_file
from client import OllamaClient, own_terms
from summarizer import summarize_and_store
from schema import DoResponse, PlanResponse

from vynacode.config import (
    CODER_MODEL, PLANNER_MODEL, OLLAMA_URL, TOKEN_BUDGET, TIER,
    config_path, current_settings, is_overridden, save_config,
)

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
    "Context code is shown with a line-number gutter like '   12 | code'.\n"
    "Use those numbers for the @@ header. Never copy the gutter into the patch;\n"
    "hunk lines start with ' ', '+' or '-' followed by the bare source line."
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
    for p in [start, *start.parents]:
        if (p / ".vc" / "vcdb.db").exists():
            return p
        if (p / "codebase.json").exists():
            return p
    return None

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def validate_patch(patch: str, root: Path) -> List[str]:
    """Check a unified diff's hunks against the real file on disk.

    A small model will happily emit a well-formed diff whose @@ line numbers do
    not point at the code it claims to edit. Returns a list of human-readable
    problems; an empty list means the hunks line up. The old side of a hunk
    (context plus removed lines) must equal the file at that position, which
    catches both a wrong offset and a wrong line count.
    """
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


def _execute_coder_result(res: DoResponse, root: Path):
    """Print-only: display proposed patches/commands, do not apply or execute."""
    console.print(f"\n[bold cyan]Thought:[/bold cyan] {res.thought}")
    if res.write:
        for action in res.write:
            problems = validate_patch(action.patch, root)
            if problems:
                console.print(
                    f"\n[bold red]Patch for {action.file_path} FAILED validation:[/bold red]")
                for p in problems:
                    console.print(f"[red]  - {p}[/red]")
                console.print("[red]Line numbers are likely wrong; do not apply as-is.[/red]")
            else:
                console.print(
                    f"\n[bold green]Proposed patch for {action.file_path} (validated):[/bold green]")
            console.print(action.patch)
    if res.run:
        for action in res.run:
            console.print(f"\n[bold magenta]Proposed command: {action.command}[/bold magenta]")
    console.print(f"\n[bold green]Response:[/bold green] {res.response}")

def _block_meta(b: dict) -> str:
    """One metadata line per block: signature, async marker, return type, deps.

    Every part is conditional on purpose. `returns` is NULL on most blocks and
    `dependencies` is empty for blocks that reference nothing, so rendering them
    unconditionally would spend context on empty fields in every block.
    Tolerates both index generations: older rows stored the whole annotation in
    `name` with no `annotation` key, newer rows split the two.
    """
    kind = "class" if b.get("type") == "class" else ("async def" if b.get("is_async") else "def")
    params = ", ".join(
        p["name"] + (f": {p['annotation']}" if p.get("annotation") else "")
        for p in (b.get("params") or [])
    )
    line = f"{kind} {b['name']}({params})"
    if b.get("returns"):
        line += f" -> {b['returns']}"
    deps = [d for d in (b.get("dependencies") or []) if d]
    if deps:
        line += f" | deps: {', '.join(deps)}"
    return line

_GUTTER_PREFIX = 8  # width of f"{n:>5} | " -> 5 number + 1 space + bar + 1 space


def _step_keywords(db_path: Path, step: str, limit: int = 4) -> Tuple[List[str], List[dict]]:
    """Resolve a plan step into keywords that actually retrieve something.

    The previous rule took the first four words longer than three characters,
    which for a step like "add a docstring to the parse_params function" yields
    add/docstring/parse_params/function: three words of prose and one symbol. So
    candidates are ordered by specificity, an underscore or a longer token first,
    and each is kept only if it returns blocks. The search is not wasted because
    these are the same results the caller goes on to use.
    """
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
    """Prefix each code line with its real file line number.

    The model must otherwise count lines to map the excerpt back to the file,
    which small models do badly. The gutter is the only reliable way to give it
    that mapping; PATCH_HINT tells it not to copy the gutter into a hunk.
    """
    start = b['line_range_start']
    return "\n".join(
        f"{start + i:>5} | {line}"
        for i, line in enumerate(b['code'].splitlines())
    )

def _pack_budget(blocks: List[dict], budget: int) -> List[dict]:
    """Cap a multi-keyword result set to a token budget, keeping whole blocks.

    search_code bounds a single query, but _run_coder and _run_multi issue one
    query per keyword, so it is the aggregate that reaches the prompt. The
    estimate reuses the 3-bytes-per-token heuristic from Block.token_estimate,
    measured on the returned code so this needs nothing from the DB layer.
    A block that does not fit is not merely skipped: it may evict cheaper
    lower-ranked blocks, because a skipped block is gone for good while the
    tokens it needed are stranded. Without this, a rank-3 block of 1891 tokens
    is dropped while 13 smaller blocks occupy 1450 of 2048.

    The gutter is charged here, not in the DB layer, because it exists only when
    the code is rendered into a prompt. database.py prices raw bytes; without
    this the budget would silently undercount by the width of the gutter.
    """

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
    """Fuse per-keyword result lists into one ranked, deduplicated list.

    Reciprocal rank fusion: each keyword contributes 1/(_RRF_K + rank) to a
    block, so a block that several keywords agree on outranks one that a single
    keyword loved. Raw BM25 scores are not comparable across queries with
    different match counts, so they cannot be summed directly.

    An exact name match is what the caller usually means, so it is lifted above
    everything else. The boost only has to exceed the most RRF can accrue, which
    is one full contribution per keyword, so it stays independent of keyword
    count and corpus size.
    """
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

async def _run_coder(db_path: Path, keywords: List[str], task: str):
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
            context_str += f"Code:\n```python\n{_numbered_code(b)}```\n"
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
        _execute_coder_result(res, db_path.parent.parent)
    else:
        console.print("[red]Coder failed to produce valid JSON[/red]")

async def _run_multi(db_path: Path, keywords: List[str], task: str):
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
            f"Codebase summaries:\n{summary_str}\n\n"
            f"Task: {task}\n\n"
            "Output EXACTLY this JSON structure:\n"
            '{"steps": ["step 1 text", "step 2 text", "..."]}'
            "\n\nCRITICAL: steps MUST be an array of PLAIN STRINGS only. "
            "NO objects, NO 'instruction' keys, NO 'description' keys, NO code blocks. "
            "Just plain text strings.\n"
            "EVERY step MUST name the exact file path and the exact symbol it changes, "
            "copied from the summaries above, e.g. "
            '"Extend parse_params in vynacode/backend/parser.py to accept keyword-only args". '
            "Never write a vague step like 'update the parser' or 'add tests': a step "
            "that names no symbol cannot be located in the codebase.\n"
            "Order steps so each one is independently applicable, and keep each step to "
            "a single file where possible.\n"
            "No fluff. No extra keys. No objects."
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
                    plan_to_save = plan.model_dump()
                    plan_to_save["original_prompt"] = task
                    json.dump(plan_to_save, f, indent=4, ensure_ascii=False)
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
                step_context += f"Code:\n```python\n{_numbered_code(b)}```\n"
            step_context += "\n"
        if not step_context.strip():
            step_context = summary_str

        # Named explicitly so the coder does not have to infer its target from
        # the excerpt alone, and so it can tell which file a write belongs to.
        step_files = sorted({b['parent_file'] for b in deduped})
        scope = ""
        if step_files:
            scope += f"Files in context: {', '.join(step_files)}\n"
        if step_kws:
            scope += f"Keywords: {', '.join(step_kws)}\n"

        coder_prompt = (
            f"Context:\n{step_context}\n\n"
            f"Task: {step}\n\n"
            f"{scope}\n"
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
            _execute_coder_result(res, db_path.parent.parent)


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
                # Blocks are already committed above, so a summarizer failure
                # (LLM timeout, malformed JSON) must cost only this file's
                # summaries -- not abort the run and leave a partial index.
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
    asyncio.run(_run_index_pipeline(Path(path).resolve()))

@app.command(help="Act on a request: retrieve context, then propose patches and commands.")
def do(
    message: str = typer.Argument(..., help="Task description in plain English"),
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
            console.print("[yellow]No codebase.json found.[/yellow]")

@app.command(help="Show run history.")
def log():
    console.print("Log! to be implemented")

@app.command(help="Show or update configuration.")
def config(
    coder_model: Optional[str] = typer.Option(None, "--coder-model", "-c", help="Model that writes code"),
    planner_model: Optional[str] = typer.Option(None, "--planner-model", "-p", help="Model that plans and summarises"),
    context_window: Optional[int] = typer.Option(None, "--context-window", "-w", help="Model context window, in tokens"),
    ollama_url: Optional[str] = typer.Option(None, "--ollama-url", "-u", help="Ollama base URL"),
):
    updates: dict = {}
    if coder_model is not None:
        updates["coder_model"] = coder_model
    if planner_model is not None:
        updates["planner_model"] = planner_model
    if ollama_url is not None:
        updates["ollama_url"] = ollama_url
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
        console.print(f"[green]Saved {changed}[/green] -> {path}")
        console.print("[dim]Applies to the next command; this process keeps its loaded values.[/dim]")

    active = config_path()
    exists = active.exists()
    console.print(f"\n[bold]Config file:[/bold] {active}")
    console.print(
        "[dim]in use[/dim]" if exists else "[yellow]not created yet - defaults in use[/yellow]"
    )

    console.print("\n[bold]Settings[/bold]")
    for key, value in current_settings().items():
        origin = "file" if is_overridden(key) else "default"
        console.print(f"  {key:<15} {value}  [dim]({origin})[/dim]")

    console.print("\n[bold]Read-only[/bold]")
    console.print(f"  {'tier':<15} {TIER}")
    console.print(f"  {'token_budget':<15} {TOKEN_BUDGET}")

@app.command(help="Check the environment: Ollama reachability, models, index state.")
def doctor():
    console.print("Doctor! to be implemented")

if __name__ == "__main__":
    app()
