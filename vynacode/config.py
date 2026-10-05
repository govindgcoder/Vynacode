import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Single source of truth for defaults; the module constants below and the
# `config` command both read from it, so a default cannot drift from what the
# process actually uses.
SETTINGS = {
    "ollama_url": "http://localhost:11434",
    "planner_model": "qwen2.5-coder:1.5b",
    "coder_model": "qwen2.5-coder:1.5b",
    "context_window": 8192,
    # Must stay equal to client.py's num_predict, which is what actually caps
    # the model's output.
    "reserved_output_tokens": 4096,
    # Fixed prompt overhead: task line, EDIT_HINT, FREE_SHAPE, framing text.
    "system_prompt_tokens": 2048,
    # Effort level, not a token count: an unbounded trace is what makes some
    # models ruminate past the point of answering.
    "think_level": "low",
}

# What Ollama's `think` field accepts. Booleans are listed because they are the
# documented on/off form; GPT-OSS ignores them and takes only the levels.
THINK_LEVELS = ("low", "medium", "high", "max", "true", "false")


def _candidate_paths() -> list[Path]:
    """Config file locations in precedence order; VYNACODE_CONFIG wins."""
    env = os.environ.get("VYNACODE_CONFIG")
    if env:
        return [Path(env).expanduser()]
    return [Path.cwd() / ".vynarc", Path.home() / ".config" / "vynacode" / "config.json"]


def config_path() -> Path:
    """First existing config file, else where a new one would be written."""
    for path in _candidate_paths():
        if path.exists():
            return path
    return _candidate_paths()[-1]


def _read(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # A malformed file must not stop the CLI from starting; the user gets
        # defaults and can overwrite the bad file with `vynacode config --...`.
        return {}


config_data = _read(config_path())

OLLAMA_URL = config_data.get("ollama_url", SETTINGS["ollama_url"])
PLANNER_MODEL = config_data.get("planner_model", SETTINGS["planner_model"])
CODER_MODEL = config_data.get("coder_model", SETTINGS["coder_model"])
CONTEXT_WINDOW = config_data.get("context_window", SETTINGS["context_window"])
RESERVED_OUTPUT_TOKENS = config_data.get("reserved_output_tokens", SETTINGS["reserved_output_tokens"])
SYSTEM_PROMPT_TOKENS = config_data.get("system_prompt_tokens", SETTINGS["system_prompt_tokens"])
THINK_LEVEL = config_data.get("think_level", SETTINGS["think_level"])
if THINK_LEVEL not in THINK_LEVELS:
    # An unknown level makes Ollama reject the whole request, so a typo in the
    # config file degrades to the default instead of breaking every call.
    THINK_LEVEL = SETTINGS["think_level"]
TIER = "free"
# What is left for retrieved code. Clamped: a window smaller than the two
# reserves would otherwise hand _pack_budget a negative budget.
TOKEN_BUDGET = max(0, CONTEXT_WINDOW - RESERVED_OUTPUT_TOKENS - SYSTEM_PROMPT_TOKENS)


def current_settings() -> dict:
    """Settled values with defaults filled in for keys the file omits."""
    return {key: config_data.get(key, default) for key, default in SETTINGS.items()}


def is_overridden(key: str) -> bool:
    return key in config_data


def save_config(updates: dict) -> Path:
    """Merge updates into the active config file and return its path.
    """
    path = config_path()
    data = _read(path) if path.exists() else {}
    data.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
        f.write("\n")
    # Kept in sync so a `config --flag` that prints the resulting values
    # reports them as file-sourced rather than as stale defaults.
    config_data.update(updates)
    return path
