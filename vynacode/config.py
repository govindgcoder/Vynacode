import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
VYNARC_PATH = BASE_DIR / ".vynarc"

config_data = {}

if VYNARC_PATH.exists():
    try:
        with open(VYNARC_PATH, "r") as f:
            config_data = json.load(f)
    except json.JSONDecodeError:
        pass

ROOT = config_data.get("root")
OLLAMA_URL = config_data.get("ollama_url", "http://localhost:11434")
PLANNER_MODEL = config_data.get("planner_model", "reecdev/qwen3.5-lowvram:9b")
CODER_MODEL = config_data.get("coder_model", "reecdev/qwen3.5-lowvram:9b")
CONTEXT_WINDOW = config_data.get("context_window", 8192)
TIER = "free"
TOKEN_BUDGET = 2048


def get_setting(key, default=None):
    return config_data.get(key, default)
