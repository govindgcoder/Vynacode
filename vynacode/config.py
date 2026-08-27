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
        print("Error: .vynarc is not a valid JSON file.")
else:
    print("Warning: .vynarc file not found. Using empty or default settings.")

ROOT = config_data.get("root")
OLLAMA_URL = config_data.get("ollama_url", "http://localhost:11434")
PLANNER_MODEL = config_data.get("planner_model", "qwen3.5:0.8b")
CODER_MODEL = config_data.get("coder_model", "qwen2.5-coder:7b")
CONTEXT_WINDOW = config_data.get("context_window", 8192)
TIER = config_data.get("tier", "free")
TOKEN_BUDGET = config_data.get("token_budget", 2048)


def get_setting(key, default=None):
    return config_data.get(key, default)
