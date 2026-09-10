#!/usr/bin/env python3
"""Systemd timer entry point."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.archive_service import run_archive_cycle
from modules.config import ArchiveConfig, config_file
from modules.job_state import record


def _interval(value: str) -> timedelta:
    text = value.strip().lower()
    units = {"min": 60, "mins": 60, "minute": 60, "minutes": 60, "h": 3600, "hour": 3600, "hours": 3600}
    for suffix, multiplier in units.items():
        if text.endswith(suffix):
            return timedelta(seconds=int(text[: -len(suffix)].strip()) * multiplier)
    raise ValueError("schedule must look like '5min' or '1hour'")


def _due(config: ArchiveConfig) -> bool:
    state_path = config_file().parent / "worker-state.json"
    try:
        last = datetime.fromisoformat(json.loads(state_path.read_text())["last_success"])
        if datetime.now(timezone.utc) < last + _interval(config.schedule):
            return False
    except FileNotFoundError:
        pass
    return True


def _mark_success() -> None:
    state_path = config_file().parent / "worker-state.json"
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"last_success": datetime.now(timezone.utc).isoformat()}) + "\n")
    state_path.chmod(0o600)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        # A mapping owns its source/archive credentials, so avoid retrieving
        # unused legacy credentials when mappings are configured.
        config = ArchiveConfig.from_env(resolve_source_secret=False, resolve_archive_secret=False)
        if not config.source_mappings:
            config = ArchiveConfig.from_env()
        if not config.enabled:
            logging.info("archive job disabled")
            record("Disabled")
            return 0
        if not _due(config):
            logging.info("archive cycle not due; schedule=%s", config.schedule)
            return 0
        result = run_archive_cycle(config)
        _mark_success()
        record("Succeeded", **result, schedule=config.schedule, log_type=config.log_type)
        logging.info("archive cycle complete: %s", json.dumps(result, default=str))
        return 0
    except Exception as exc:
        logging.exception("archive cycle failed")
        record("Failed", error=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
