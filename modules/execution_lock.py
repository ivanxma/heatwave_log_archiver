"""A nonblocking MySQL execution lock shared by every Compute instance."""
from .control_store import execution_lock


def archive_execution_lock(state_directory=None):
    # Argument retained for callers upgrading from the former host file lock.
    return execution_lock()
