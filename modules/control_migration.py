"""Explicit, verified import of legacy Compute JSON into a control schema."""
import json
from pathlib import Path
from . import control_store


def import_existing(directory):
    directory = Path(directory)
    files = [('settings.json', None), ('job-state.json', 'job'), ('worker-state.json', 'worker')]
    values = {name: json.loads((directory / name).read_text()) for name, _ in files if (directory / name).exists()}
    settings = values.get('settings.json', {})
    if not settings:
        raise ValueError('No existing settings.json was found to import.')
    if control_store.public_settings(settings) != settings:
        raise ValueError('Remove legacy plaintext passwords before importing control settings.')
    with control_store.connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT payload FROM {control_store.table('control_settings')} WHERE id=1 FOR UPDATE")
            row = cur.fetchone()
            if not row or control_store.decode(row[0]):
                raise ValueError('Control schema already contains settings or is not initialized; import was not applied.')
            cur.execute(f"SELECT COUNT(*) FROM {control_store.table('control_entities')}")
            if cur.fetchone()[0]:
                raise ValueError('Control schema already contains connection records; import was not applied.')
            cur.execute(f"SELECT COUNT(*) FROM {control_store.table('control_state')} WHERE name IN ('job','worker')")
            if cur.fetchone()[0]:
                raise ValueError('Control schema already contains job state; import was not applied.')
            cur.execute(f"UPDATE {control_store.table('control_settings')} SET payload=%s WHERE id=1", (json.dumps({k: v for k, v in settings.items() if k not in control_store.ENTITY_KEYS}),))
            for kind in control_store.ENTITY_KEYS:
                for position, value in enumerate(settings.get(kind, [])):
                    cur.execute(f"INSERT INTO {control_store.table('control_entities')} VALUES (%s,%s,%s)", (kind, position, json.dumps(value)))
            for name, state_name in files:
                if state_name and name in values:
                    cur.execute(f"INSERT INTO {control_store.table('control_state')} VALUES (%s,%s)", (state_name, json.dumps(values[name])))
            conn.commit()
        finally:
            cur.close()
    loaded = control_store.load_settings()
    for key, value in settings.items():
        if loaded.get(key, [] if key in control_store.ENTITY_KEYS else None) != value:
            raise RuntimeError('Imported settings verification failed; legacy files were retained.')
    for name, state_name in files:
        if state_name and name in values and control_store.load_state(state_name) != values[name]:
            raise RuntimeError('Imported state verification failed; legacy files were retained.')
    return list(values)


def retire_existing(directory):
    """Preserve a protected rollback archive, then retire obsolete JSON stores."""
    import os
    import tarfile
    directory = Path(directory)
    files = [p for p in directory.glob('*.json') if p.name in ('settings.json', 'job-state.json', 'worker-state.json') or p.name.startswith('settings.')]
    if not files:
        return
    backup = directory / 'legacy-control-backup.tar.gz'
    if backup.exists():
        raise RuntimeError('A legacy backup already exists; JSON files were retained for review.')
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream, tarfile.open(fileobj=stream, mode='w:gz') as archive:
            for path in files:
                archive.add(path, arcname=path.name)
        for path in files:
            path.unlink()
    except Exception:
        # Keep the backup for review if retirement only partially completed.
        raise
