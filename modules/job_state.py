"""Durable, non-secret status and recent execution history for the scheduler."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import config_file


def _path() -> Path:
    return config_file().parent / "job-state.json"


def load_state() -> dict[str, object]:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"status": "Not yet run", "history": []}


def record(status: str, **details: object) -> dict[str, object]:
    state = load_state()
    if status != "Failed":
        state.pop("error", None)
    event = {"time": datetime.now(timezone.utc).isoformat(), "status": status, **details}
    # A five-minute schedule needs 288 entries for one complete day of activity.
    history = [event, *list(state.get("history", []))][:288]
    state.update(event)
    state["history"] = history
    path = _path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, default=str) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(path)
    return state
