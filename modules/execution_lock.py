"""A non-blocking host lock shared by timer and web-triggered executions."""
from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def archive_execution_lock(state_directory: Path):
    lock_path = state_directory / "archive-execution.lock"
    state_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
