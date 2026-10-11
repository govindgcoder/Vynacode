# arcKode

**Local-first AI coding agent that indexes your codebase and operates within compact VRAM constraints.**

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](https://www.gnu.org/licenses/agpl-3.0.html)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-green.svg)](https://www.python.org/downloads/)
[![Built with Typer](https://img.shields.io/badge/CLI-Typer-009688.svg)](https://typer.tiangolo.com/)
[![Tree-sitter](https://img.shields.io/badge/parsing-Tree--sitter-FF6B35.svg)](https://tree-sitter.github.io/)

```
$ arckode index
Indexing complete. 247 files, 3,891 blocks, 3,891 summarised

$ arckode do "add structured logging to the parser module"
Keywords: parser, logging, structured, module, add

Planning...
New Plan (3 steps):
  1. arckode/backend/parser.py: add structured logging imports
  2. arckode/backend/parser.py: instrument parse_file with log calls
  3. arckode/backend/parser.py: add module-level logger configuration

Proceed with this plan? [y/N]: y

Step 1/3: arckode/backend/parser.py: add structured logging imports
--- File: arckode/backend/parser.py | Range: 1-15 ---
import logging
...
Applied edit to arckode/backend/parser.py

Step 2/3: arckode/backend/parser.py: instrument parse_file with log calls
--- File: arckode/backend/parser.py | Range: 47-62 ---
    logger.info("Parsing %s", path)
...
Applied edit to arckode/backend/parser.py

Step 3/3: arckode/backend/parser.py: add module-level logger configuration
--- File: arckode/backend/parser.py | Range: 15-18 ---
logger = logging.getLogger(__name__)
...
Applied edit to arckode/backend/parser.py

Re-indexing codebase...
Indexing complete.
```

---

## Core Value Proposition

- **Zero external dependencies** — Runs entirely on local models via Ollama; no API keys, no cloud calls, no data leaves your machine
- **Semantic code understanding** — Tree-sitter parsing across 11 languages produces precise symbol-level blocks with signatures, dependencies, and summaries
- **Token-aware context packing** — Reciprocal-rank fusion with exact-name boosting fits maximum signal into a fixed token budget
- **Two-mode operation** — Single-shot edits for surgical changes; multi-step planner+coder for complex refactors with confirmation at each step
- **Built-in safety** — Automatic snapshots before every write, anchored edits with occurrence control, path containment checks
- **Incremental indexing** — SHA-256 change detection re-processes only modified files; summaries persist across runs
- **Transparent reasoning** — Optional model thinking traces visible in-terminal; full run history logged for auditability

---

## Quick Start

**Prerequisites:** Python 3.12+, [Ollama](https://ollama.com/) running locally, [uv](https://docs.astral.sh/uv/)

```bash
# 1. Clone the repository
git clone https://github.com/your-org/arckode.git
cd arckode

# 2. Install runtime dependencies
uv sync

# 3. Pull required models (one-time)
ollama pull qwen2.5-coder:1.5b

# 4. Install as a tool
uv tool install .

# 5. Index your project and start coding
cd /path/to/your/project
arckode index
arckode do "your task description here"
```

> **Note:** The `uv tool install .` step is intentional friction — it ensures you run the exact code you audited.

---

## Usage Examples

### Basic: Single-file edit

```bash
arckode do "rename the variable 'count' to 'total' in utils/metrics.py"
```

### Intermediate: Cross-file refactor with planning

```bash
arckode do "extract the validation logic from handlers/auth.py into a new validators/auth.py module"
```

### Advanced: Multi-step feature with auto-confirm

```bash
arckode do "add request ID middleware and thread it through the logging context" --mode multi --yes
```

### Inspect the index

```bash
# View last execution plan
arckode show plan

# Search indexed symbols
arckode show context --query "authenticate"

# Full codebase JSON export
arckode show index
```

### Configuration

```bash
# View current settings
arckode config

# Override models
arckode config --coder-model "deepseek-coder:6.7b" --planner-model "qwen2.5:7b"

# Adjust thinking effort
arckode config --think-level high
```

---

## Architecture Overview

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  User Request   │────▶│  Query Expansion │────▶│  Keyword Search │
└─────────────────┘     └──────────────────┘     └────────┬────────┘
                                                          │
                                                          ▼
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Apply Changes  │◀────│  Coder Model     │◀────│  Context Packer │
│  (anchored,     │     │  (JSON patches)  │     │  (RRF + budget) │
│   snapshots)    │     └──────────────────┘     └─────────────────┘
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Re-index       │
│  (incremental)  │
└─────────────────┘
```

**Index structure** (`.vc/vcdb.db`):
- `files` — path, hash, language, size, imports
- `blocks` — symbol metadata, byte ranges, dependencies, summaries
- `blocks_fts` — FTS5 full-text search (parent_file unindexed)

---

## Supported Languages

| Language | Extensions | Parser |
|----------|------------|--------|
| Python | `.py` | tree-sitter-python |
| JavaScript | `.js` | tree-sitter-javascript |
| TypeScript | `.ts`, `.tsx` | tree-sitter-typescript |
| Go | `.go` | tree-sitter-go |
| Rust | `.rs` | tree-sitter-rust |
| Java | `.java` | tree-sitter-java |
| C# | `.cs` | tree-sitter-c-sharp |
| Kotlin | `.kt`, `.kts` | tree-sitter-kotlin |
| Swift | `.swift` | tree-sitter-swift |

---

## Configuration

Configuration file locations (precedence order):

1. `$ARKCODE_CONFIG` (environment variable)
2. `./.arkrc` (project-local)
3. `~/.config/arckode/config.json` (user global)

```json
{
  "ollama_url": "http://localhost:11434",
  "coder_model": "qwen2.5-coder:1.5b",
  "planner_model": "qwen2.5-coder:1.5b",
  "context_window": 8192,
  "reserved_output_tokens": 4096,
  "system_prompt_tokens": 2048,
  "think_level": "low",
  "think_overrides": {}
}
```

**Token budget** = `context_window - reserved_output_tokens - system_prompt_tokens` (default 2048)

---

## License

AGPL-3.0-only — see [LICENSE](LICENSE) for details.