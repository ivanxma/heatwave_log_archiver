"""Execution history and checkpoints in the shared archive control schema."""
from datetime import datetime, timezone
from . import control_store


def load_state():
    return control_store.load_state('job') or {'status': 'Not yet run', 'history': []}


def record(status, **details):
    event = {'time': datetime.now(timezone.utc).isoformat(), 'status': status, **details}
    def update(state):
        if status != 'Failed':
            state.pop('error', None)
        state['history'] = [event, *state.get('history', [])][:288]
        state.update(event)
        return state
    return control_store.update_state('job', update)
