"""Runtime configuration and paths for Token-Monitor."""

from __future__ import annotations

import json
import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
STATIC_DIR = REPO_ROOT / "static"
PRICING_FILE = REPO_ROOT / "pricing.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "events.db"

CLAUDE_PROJECTS_DIR = Path(
    os.environ.get(
        "TOKEN_MONITOR_CLAUDE_PROJECTS_DIR",
        str(Path.home() / ".claude" / "projects"),
    )
)

SERVER_HOST = os.environ.get("TOKEN_MONITOR_HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("TOKEN_MONITOR_PORT", "8765"))

JSONL_POLL_INTERVAL_SECONDS = int(os.environ.get("TOKEN_MONITOR_JSONL_POLL", "30"))


def load_pricing() -> dict:
    if not PRICING_FILE.exists():
        return {"models": {}, "fallback": "claude-sonnet-4-6"}
    with PRICING_FILE.open("r", encoding="utf-8") as fh:
        return json.load(fh)
