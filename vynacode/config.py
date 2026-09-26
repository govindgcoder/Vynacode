import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Single source of truth for defaults. The module-level constants below and the
# `config` command's view both read from this, so a default can never drift
# between what the CLI prints and what the process actually uses.
SETTINGS = {
    "ollama_url": "http://localhost:11434",
    "planner_model": "qwen2.5-coder:1.5b",
    "coder_model": "qwen2.5-coder:1.5b",
    "context_window": 8192,
}


def _candidate_paths() -> list[Path]:
    """Config file locations in precedence order.

    An explicit VYNACODE_CONFIG short-circuits the chain so a single invocation
    can be pointed at an arbitrary file, which is what makes the file
    scriptable and testable without touching a real user config.
    """
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
TIER = "free"
TOKEN_BUDGET = 2048


def current_settings() -> dict:
    """Settled values with defaults filled in for keys the file omits."""
    return {key: config_data.get(key, default) for key, default in SETTINGS.items()}


def is_overridden(key: str) -> bool:
    return key in config_data


def save_config(updates: dict) -> Path:
    """Merge updates into the active config file and return its path.

    Read-modify-write rather than a full rewrite, so a key this build does not
    know about survives a save. Writes to the user-global file unless a
    project-local .vynarc or VYNACODE_CONFIG already exists, so setting a model
    once affects every project unless a repo deliberately pins its own.
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
