"""Small Connector/Python adapter with identifier safety."""
from __future__ import annotations

import re
import hashlib
import threading
from contextlib import contextmanager

import mysql.connector

from .config import ArchiveConfig

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_$]+$")
_CONNECTION_CACHE: dict[tuple[str, int, str, str, str], object] = {}
_CONNECTION_LOCKS: dict[tuple[str, int, str, str, str], threading.RLock] = {}
_CACHE_LOCK = threading.RLock()


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


def _connection_key(host: str, port: int, user: str, password: str, socket: str) -> tuple[str, int, str, str, str]:
    """Identify a live connection without retaining the password as a cache key."""
    return host, port, user, hashlib.sha256(password.encode("utf-8")).hexdigest(), socket


def clear_connection_cache() -> None:
    """Close all server-memory connections after connection settings change."""
    with _CACHE_LOCK:
        connections = list(_CONNECTION_CACHE.values())
        _CONNECTION_CACHE.clear()
        _CONNECTION_LOCKS.clear()
    for connection in connections:
        try:
            connection.close()
        except Exception:
            pass


def _drop_connection(key: tuple[str, int, str, str, str], connection: object) -> None:
    with _CACHE_LOCK:
        if _CONNECTION_CACHE.get(key) is connection:
            _CONNECTION_CACHE.pop(key, None)
    try:
        connection.close()
    except Exception:
        pass


@contextmanager
def _cached_connection(host: str, port: int, user: str, password: str, socket: str):
    """Borrow a verified MySQL connection held only in this process's memory."""
    key = _connection_key(host, port, user, password, socket)
    with _CACHE_LOCK:
        connection_lock = _CONNECTION_LOCKS.setdefault(key, threading.RLock())
    # One operation at a time per connection; different connection identities
    # may run concurrently in mapping worker threads.
    with connection_lock:
        with _CACHE_LOCK:
            connection = _CONNECTION_CACHE.get(key)
        try:
            if connection is None or not connection.is_connected():
                if connection is not None:
                    _drop_connection(key, connection)
                connection = _connection(host, port, user, password, socket)
                with _CACHE_LOCK:
                    _CONNECTION_CACHE[key] = connection
            # Validate each reused connection before it is handed to a caller.
            cursor = connection.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            cursor.close()
            yield connection
            connection.rollback()  # release any read transaction left by a report
        except Exception:
            if connection is not None:
                _drop_connection(key, connection)
            raise


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
    with _cached_connection(config.source_host, config.source_port, config.source_user, config.source_password, config.source_socket) as connection:
        yield connection


@contextmanager
def archive_connection(config: ArchiveConfig):
    with _cached_connection(config.archive_host, config.archive_port, config.archive_user, config.archive_password, config.archive_socket) as connection:
        yield connection
