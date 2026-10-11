import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

SETTINGS = {
    "ollama_url": "http://localhost:11434",
    "planner_model": "qwen2.5-coder:1.5b",
    "coder_model": "qwen2.5-coder:1.5b",
    "context_window": 8192,
    "reserved_output_tokens": 4096,
    "system_prompt_tokens": 2048,
    "think_level": "low",
    "think_overrides": {},
}

THINK_LEVELS = ("low", "medium", "high", "true", "false")


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
def think_for(model: str, role: str | None = None) -> str:
    """Effective think level: role override, model override, bare-name override, else global.

    Role keys ("coder"/"planner") come first: both roles may share one model,
    which a model-keyed override cannot differentiate.
    """
    settings = current_settings()
    level = settings["think_level"]
    if level not in THINK_LEVELS:
        level = SETTINGS["think_level"]
    overrides = settings.get("think_overrides")
    if not isinstance(overrides, dict):
        return level
    if role and overrides.get(role) in THINK_LEVELS:
        return overrides[role]
    if overrides.get(model) in THINK_LEVELS:
        return overrides[model]
    bare = model.split(":", 1)[0]
    for name, lv in overrides.items():
        if lv in THINK_LEVELS and name.split(":", 1)[0] == bare:
            return lv
    return level


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
