"""Small Connector/Python adapter with identifier safety."""
from __future__ import annotations

import re
from contextlib import contextmanager

import mysql.connector

from .config import ArchiveConfig

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_$]+$")


def ident(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"Unsafe MySQL identifier: {value!r}")
    return f"`{value}`"


def _connection(host: str, port: int, user: str, password: str, socket: str):
    args = {"user": user, "password": password, "autocommit": False}
    if socket:
        args["unix_socket"] = socket
    else:
        args.update({"host": host, "port": port, "ssl_disabled": False})
    return mysql.connector.connect(**args)


def test_mysql_connection(profile: dict[str, object], username: str, password: str) -> None:
    """Authenticate a profile at login without retaining a live client connection."""
    mode = str(profile.get("mode", "tcp"))
    connection = _connection(str(profile.get("host", "127.0.0.1")), int(profile.get("port", 3306)), username, password, str(profile.get("socket", "")) if mode == "socket" else "")
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        cursor.close()
    finally:
        connection.close()


@contextmanager
def source_connection(config: ArchiveConfig):
    connection = _connection(config.source_host, config.source_port, config.source_user, config.source_password, config.source_socket)
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def archive_connection(config: ArchiveConfig):
    connection = _connection(config.archive_host, config.archive_port, config.archive_user, config.archive_password, config.archive_socket)
    try:
        yield connection
    finally:
        connection.close()
