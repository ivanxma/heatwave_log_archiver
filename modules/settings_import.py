"""Validate exported job settings before atomically restoring a fresh schema."""
import json
import re
from .config import ArchiveConfig
from .control_store import ENTITY_KEYS, public_settings

MAX_IMPORT_BYTES = 2 * 1024 * 1024


def parse_settings(stream):
    raw = stream.read(MAX_IMPORT_BYTES + 1)
    if len(raw) > MAX_IMPORT_BYTES:
        raise ValueError('Job settings file exceeds the 2 MiB limit.')
    try:
        settings = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ValueError('The file must contain valid UTF-8 JSON.') from None
    if not isinstance(settings, dict) or not settings:
        raise ValueError('Exported settings must be a nonempty JSON object.')
    if 'profiles' in settings or 'active_control_profile' in settings:
        raise ValueError('Choose job-settings.json, rather than the worker profiles.json export.')
    if public_settings(settings) != settings:
        raise ValueError('Plaintext passwords, tokens and private keys are not accepted; use credential Secret OCIDs.')
    if not isinstance(settings.get('enabled'), bool):
        raise ValueError('The exported enabled field must be a JSON boolean.')
    if 'configured' in settings and not isinstance(settings['configured'], bool):
        raise ValueError('The configured field must be a JSON boolean.')
    for kind in ENTITY_KEYS:
        if kind in settings and (not isinstance(settings[kind], list) or not all(isinstance(item, dict) for item in settings[kind])):
            raise ValueError(f'{kind} must be a list of objects.')
    for kind in ('source_connections', 'archive_connections'):
        for item in settings.get(kind, []):
            if kind == 'source_connections' and item.get('server_uuid'):
                import uuid
                try:
                    if str(uuid.UUID(item['server_uuid'])) != item['server_uuid']:
                        raise ValueError
                except (ValueError, AttributeError, TypeError):
                    raise ValueError('Source connection server_uuid must be a canonical MySQL UUID.') from None
            if kind == 'source_connections':
                import uuid
                observed = item.get('server_uuids', [])
                hostnames = item.get('server_hostnames', {})
                if not isinstance(observed, list) or not isinstance(hostnames, dict):
                    raise ValueError('Observed source UUIDs and hostnames must be a list and object.')
                try:
                    if any(not isinstance(value, str) or str(uuid.UUID(value)) != value for value in [*observed, *hostnames]):
                        raise ValueError
                except (ValueError, AttributeError, TypeError):
                    raise ValueError('Source connection observed UUIDs must be canonical MySQL UUIDs.') from None
            secret = item.get('secret_ocid', '')
            if not isinstance(secret, str) or not secret.startswith('ocid1.vaultsecret.'):
                raise ValueError(f'{kind} requires credential Secret OCIDs.')
            try:
                port = int(item.get('port', 3306))
            except (ValueError, TypeError):
                raise ValueError(f'{kind} contains an invalid port.') from None
            if not 1 <= port <= 65535 or not (item.get('host') or item.get('socket')):
                raise ValueError(f'{kind} requires a host/socket and a port between 1 and 65535.')
    for key in ('source_secret_ocid', 'archive_secret_ocid'):
        if settings.get(key) and not str(settings[key]).startswith('ocid1.vaultsecret.'):
            raise ValueError(f'{key} must identify a credential secret.')
    for kind, connections in [('source_tables', 'source_connections'), ('archive_tables', 'archive_connections')]:
        names = {item.get('name') for item in settings.get(connections, [])}
        for item in settings.get(kind, []):
            if item.get('connection') not in names:
                raise ValueError(f'{kind} references a missing connection.')
            if kind == 'source_tables':
                identifiers = [item.get('source', '').split('.'), [item.get('timestamp_column', '')]]
                if len(identifiers[0]) != 2:
                    raise ValueError('Source tables require schema.table and a timestamp column.')
            else:
                identifiers = [[item.get('archive_db', ''), item.get('archive_table', '')]]
            if not all(re.fullmatch(r'[A-Za-z0-9_$]+', str(value)) for group in identifiers for value in group):
                raise ValueError(f'{kind} contains invalid SQL identifiers.')
    source_names = {item.get('name') for item in settings.get('source_tables', [])}
    archive_names = {item.get('name') for item in settings.get('archive_tables', [])}
    for mapping in settings.get('source_mappings', []):
        if mapping.get('source_table') not in source_names or mapping.get('archive_table_ref') not in archive_names:
            raise ValueError('Each mapping must reference existing source and archive tables.')
    ArchiveConfig.from_env(False, False, settings=settings)
    return settings
