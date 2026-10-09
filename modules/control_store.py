"""Shared MySQL control schema; only connection bootstrap remains on Compute."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .profile_store import load_profiles
from .secret_provider import vault_credential

_TARGET = ContextVar('archive_control_target', default=None)
_EXECUTION = ContextVar('archive_execution_id', default=None)
ENTITY_KEYS = ('source_connections', 'archive_connections', 'source_tables', 'archive_tables', 'source_mappings', 'custom_sources')


def profile_path():
    return Path(os.environ.get('ERROR_ARCHIVER_PROFILE_STORE', 'profiles.json'))


def active_profile():
    path = profile_path()
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    name = payload.get('active_control_profile')
    return payload.get('profiles', {}).get(name)


def bind(profile, username, password):
    return _TARGET.set((dict(profile), username, password))


def unbind(token):
    _TARGET.reset(token)


def ready():
    target = _TARGET.get()
    profile = target[0] if target else active_profile()
    return bool(profile and profile.get('control_schema'))


def target():
    current = _TARGET.get()
    if current:
        return current
    profile = active_profile()
    if not profile or not profile.get('control_schema'):
        raise RuntimeError('Configure the archive control database before running jobs.')
    if not profile.get('secret_ocid'):
        raise RuntimeError('The control connection requires a credential Secret OCID for unattended workers.')
    user, password = vault_credential(profile['secret_ocid'], profile.get('user', ''))
    return profile, user, password


@contextmanager
def connection():
    # Deferred imports avoid the config / MySQL adapter import cycle.
    from .mysql_util import _cached_connection
    profile, username, password = target()
    with _cached_connection(str(profile.get('host', '127.0.0.1')), int(profile.get('port', 3306)), username, password, str(profile.get('socket', '')) if profile.get('mode') == 'socket' else '') as conn:
        yield conn


def table(name):
    from .mysql_util import ident
    profile, _, _ = _TARGET.get() or (active_profile(), '', '')
    if not profile or not profile.get('control_schema'):
        raise RuntimeError('Archive control schema has not been configured.')
    return f"{ident(profile['control_schema'])}.{ident(name)}"


def initialize():
    from .mysql_util import ident
    profile, _, _ = target()
    with connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS {ident(profile['control_schema'])} CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
            cur.execute(f"CREATE TABLE IF NOT EXISTS {table('control_settings')} (id TINYINT PRIMARY KEY, payload JSON NOT NULL) ENGINE=InnoDB")
            cur.execute(f"CREATE TABLE IF NOT EXISTS {table('control_entities')} (kind VARCHAR(32) NOT NULL, position INT NOT NULL, payload JSON NOT NULL, PRIMARY KEY(kind, position)) ENGINE=InnoDB")
            cur.execute(f"CREATE TABLE IF NOT EXISTS {table('control_state')} (name VARCHAR(32) PRIMARY KEY, payload JSON NOT NULL) ENGINE=InnoDB")
            cur.execute(f"INSERT IGNORE INTO {table('control_settings')} VALUES (1, %s)", ('{}',))
            conn.commit()
        finally:
            cur.close()


def decode(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def load_settings():
    if not ready():
        return {}
    with connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT payload FROM {table('control_settings')} WHERE id=1")
            row = cur.fetchone()
            if not row:
                raise RuntimeError('Initialize the archive control schema first.')
            settings = decode(row[0])
            cur.execute(f"SELECT kind, payload FROM {table('control_entities')} ORDER BY kind, position")
            for kind, payload in cur.fetchall():
                settings.setdefault(kind, []).append(decode(payload))
            return settings
        finally:
            cur.close()


def save_settings(settings, *, require_empty=False):
    # Validate and strip any accidental plaintext password fields recursively.
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if 'password' not in k.lower()}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value
    settings = clean(settings)
    with connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT payload FROM {table('control_settings')} WHERE id=1 FOR UPDATE")
            row = cur.fetchone()
            if not row:
                raise ValueError('Initialize the control schema before saving settings.')
            if require_empty:
                if decode(row[0]):
                    raise ValueError('Import requires an empty control schema; existing settings were not overwritten.')
                cur.execute(f"SELECT COUNT(*) FROM {table('control_entities')}")
                if cur.fetchone()[0]:
                    raise ValueError('Import requires an empty control schema; existing connection records were not overwritten.')
            cur.execute(f"UPDATE {table('control_settings')} SET payload=%s WHERE id=1", (json.dumps({k: v for k, v in settings.items() if k not in ENTITY_KEYS}),))
            cur.execute(f"DELETE FROM {table('control_entities')}")
            for kind in ENTITY_KEYS:
                for position, value in enumerate(settings.get(kind, [])):
                    cur.execute(f"INSERT INTO {table('control_entities')} VALUES (%s,%s,%s)", (kind, position, json.dumps(value)))
            conn.commit()
        finally:
            cur.close()


def load_state(name='job'):
    if not ready():
        return {}
    with connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT payload FROM {table('control_state')} WHERE name=%s", (name,))
            row = cur.fetchone()
            return decode(row[0]) if row else {}
        finally:
            cur.close()


def update_state(name, update):
    with connection() as conn:
        cur = conn.cursor()
        try:
            # A no-op upsert takes an exclusive row lock. INSERT IGNORE can
            # leave competing sessions holding shared locks before FOR UPDATE,
            # causing a lock-upgrade deadlock on simultaneous state updates.
            cur.execute(f"INSERT INTO {table('control_state')} VALUES (%s,%s) ON DUPLICATE KEY UPDATE name=name", (name, '{}'))
            cur.execute(f"SELECT payload FROM {table('control_state')} WHERE name=%s FOR UPDATE", (name,))
            state = update(decode(cur.fetchone()[0]))
            cur.execute(f"UPDATE {table('control_state')} SET payload=%s WHERE name=%s", (json.dumps(state, default=str), name))
            conn.commit()
            return state
        finally:
            cur.close()


@contextmanager
def execution_lock():
    # Dedicated connection: a pooled connection might be borrowed recursively by
    # extraction and cannot safely own a cross-Compute lock throughout that work.
    from .mysql_util import _connection
    profile, user, password = target()
    lock_name = 'error-archiver:' + hashlib.sha256(profile['control_schema'].encode()).hexdigest()[:48]
    conn = _connection(str(profile.get('host', '127.0.0.1')), int(profile.get('port', 3306)), user, password, str(profile.get('socket', '')) if profile.get('mode') == 'socket' else '')
    cur = conn.cursor()
    acquired = False
    token = None
    execution_id = uuid.uuid4().hex
    try:
        cur.execute('SELECT GET_LOCK(%s, 0)', (lock_name,))
        acquired = cur.fetchone()[0] == 1
        if acquired:
            token = _EXECUTION.set(execution_id)
            cur.execute('SELECT CONNECTION_ID()')
            owner = cur.fetchone()[0]
            update_state('execution', lambda state: {'id': execution_id, 'owner_connection': owner, 'cancel_requested': False})
        yield acquired
    finally:
        try:
            if acquired:
                update_state('execution', lambda state: {} if state.get('id') == execution_id else state)
        finally:
            if token is not None:
                _EXECUTION.reset(token)
            conn.close()  # Closing also releases GET_LOCK, including on exceptions.


def check_cancelled():
    execution_id = _EXECUTION.get()
    if execution_id:
        state = load_state('execution')
        if state.get('id') != execution_id:
            raise RuntimeError('Archive execution ownership changed; stopping before advancing checkpoints.')
        if state.get('cancel_requested'):
            raise RuntimeError('Archive execution cancelled by a control administrator; checkpoints were not advanced.')


def request_cancel(execution_id):
    if not execution_id:
        raise ValueError('Select an active execution to override.')
    def update(state):
        if state.get('id') != execution_id:
            raise ValueError('The execution has changed; refresh before requesting an override.')
        return {**state, 'cancel_requested': True}
    return update_state('execution', update)


def validate_credentials(profile, create=False):
    """Validate Vault access, authentication, and control-schema runtime writes."""
    from .mysql_util import ident, test_mysql_connection
    ident(profile['control_schema'])
    user, password = vault_credential(profile['secret_ocid'], profile.get('user', ''))
    test_mysql_connection(profile, user, password)
    token = bind(profile, user, password)
    try:
        with connection() as conn:
            cur = conn.cursor()
            cur.execute('SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME IN (%s,%s,%s)', (profile['control_schema'], 'control_settings', 'control_entities', 'control_state'))
            initialized = cur.fetchone()[0] == 3
            cur.close()
        if not initialized and create:
            initialize()
            initialized = True
        if initialized:
            with connection() as conn:
                cur = conn.cursor()
                try:
                    probe = 'probe_' + uuid.uuid4().hex[:20]
                    cur.execute(f"SELECT payload FROM {table('control_settings')} WHERE id=1")
                    if cur.fetchone() is None:
                        raise RuntimeError('Control settings row is missing; initialize the schema.')
                    cur.execute(f"SELECT payload FROM {table('control_entities')} LIMIT 1")
                    cur.fetchall()
                    cur.execute(f"UPDATE {table('control_settings')} SET payload=payload WHERE id=1")
                    cur.execute(f"UPDATE {table('control_entities')} SET payload=payload WHERE 1=0")
                    cur.execute(f"INSERT INTO {table('control_entities')} VALUES (%s,%s,%s)", (probe, 0, '{}'))
                    cur.execute(f"DELETE FROM {table('control_entities')} WHERE kind=%s", (probe,))
                    cur.execute(f"INSERT INTO {table('control_state')} VALUES (%s,%s)", (probe, '{}'))
                    cur.execute(f"UPDATE {table('control_state')} SET payload=%s WHERE name=%s", ('{}', probe))
                    cur.execute(f"DELETE FROM {table('control_state')} WHERE name=%s", (probe,))
                    conn.rollback()
                finally:
                    cur.close()
        return initialized
    finally:
        unbind(token)


def export_bootstrap(name, profile):
    """Portable unattended-worker connection settings, with no plaintext secrets."""
    keys = ('name', 'host', 'port', 'mode', 'socket', 'profile_management', 'control_schema', 'user', 'secret_ocid')
    public = {key: value for key, value in profile.items() if key in keys}
    public['name'] = name
    return {'active_control_profile': name, 'profiles': {name: public}}


def public_settings(value):
    if isinstance(value, dict):
        return {key: public_settings(item) for key, item in value.items() if 'password' not in key.lower() and key.lower() not in {'token', 'private_key', 'ssh_private_key'}}
    if isinstance(value, list):
        return [public_settings(item) for item in value]
    return value
