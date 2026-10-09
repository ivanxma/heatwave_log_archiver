"""Capture actual cluster-instance identity without changing logical connections."""
import uuid
from .mysql_util import _connection
from .secret_provider import vault_credential, clear_credential_cache


def read_source_identity(connection):
    cursor = connection.cursor()
    try:
        cursor.execute('SELECT @@server_uuid, @@hostname')
        row = cursor.fetchone()
        if not row:
            raise ValueError('The source server did not return its identity.')
        return str(uuid.UUID(str(row[0]))), str(row[1])
    finally:
        cursor.close()


def read_server_uuid(connection):
    return read_source_identity(connection)[0]


def with_identity(profile, server_uuid, hostname):
    result = dict(profile)
    observed = list(profile.get('server_uuids', []))
    if profile.get('server_uuid'):
        observed.append(profile['server_uuid'])
    result['server_uuids'] = list(dict.fromkeys([*observed, server_uuid]))
    result['server_uuid'] = server_uuid
    result['server_hostname'] = hostname
    result['server_hostnames'] = {**profile.get('server_hostnames', {}), server_uuid: hostname}
    return result


def validate_source_connection(profile):
    clear_credential_cache()
    user, password = vault_credential(profile.get('secret_ocid', ''), profile.get('user', ''))
    connection = _connection(profile.get('host', ''), int(profile.get('port', 3306)), user, password, profile.get('socket', ''))
    try:
        return read_source_identity(connection)
    finally:
        connection.close()
