"""Validated runtime configuration loaded exclusively from environment variables."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .secret_provider import vault_credential
from dataclasses import dataclass


def config_file() -> Path:
    return Path(os.environ.get("ERROR_ARCHIVER_CONFIG_FILE", "instance/settings.json"))


def _settings() -> dict[str, object]:
    path = config_file()
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _value(name: str, default: str = "", settings: dict[str, object] | None = None) -> str:
    # Environment values are deployment defaults; saved web settings take precedence.
    setting_key = name.removeprefix("ERROR_ARCHIVER_").lower()
    if settings and setting_key in settings:
        return str(settings[setting_key]).strip()
    return os.environ.get(name, default).strip()


def _positive_int(name: str, default: int, settings: dict[str, object] | None = None) -> int:
    try:
        value = int(_value(name, str(default), settings))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


@dataclass(frozen=True)
class ArchiveConfig:
    enabled: bool
    log_type: str
    log_types: tuple[str, ...]
    custom_source: str
    custom_timestamp_column: str
    custom_sources: tuple[dict[str, str], ...]
    source_host: str
    source_port: int
    source_user: str
    source_password: str
    source_secret_ocid: str
    source_socket: str
    archive_host: str
    archive_port: int
    archive_user: str
    archive_password: str
    archive_secret_ocid: str
    archive_socket: str
    archive_db: str
    archive_table: str
    retention_months: int
    batch_size: int
    schedule: str

    @classmethod
    def from_env(cls) -> "ArchiveConfig":
        settings = _settings()
        source_host = _value("ERROR_ARCHIVER_SOURCE_HOST", "127.0.0.1", settings)
        source_user = _value("ERROR_ARCHIVER_SOURCE_USER", settings=settings)
        archive_user = _value("ERROR_ARCHIVER_ARCHIVE_USER", source_user, settings)
        log_type = _value("ERROR_ARCHIVER_LOG_TYPE", "error_log", settings)
        raw_types = _value("ERROR_ARCHIVER_LOG_TYPES", log_type, settings)
        log_types = tuple(item.strip() for item in raw_types.split(",") if item.strip())
        custom_sources = settings.get("custom_sources", []) if isinstance(settings.get("custom_sources", []), list) else []
        if _value("ERROR_ARCHIVER_CUSTOM_SOURCE", settings=settings):
            custom_sources = [*custom_sources, {"name": "custom", "source": _value("ERROR_ARCHIVER_CUSTOM_SOURCE", settings=settings), "timestamp_column": _value("ERROR_ARCHIVER_CUSTOM_TIMESTAMP_COLUMN", "event_time", settings)}]
        source_secret_ocid = _value("ERROR_ARCHIVER_SOURCE_SECRET_OCID", settings=settings)
        archive_secret_ocid = _value("ERROR_ARCHIVER_ARCHIVE_SECRET_OCID", settings=settings)
        source_user, source_password = vault_credential(source_secret_ocid, source_user)
        archive_user, archive_password = vault_credential(archive_secret_ocid, archive_user)
        config = cls(
            enabled=_value("ERROR_ARCHIVER_ENABLED", "true", settings).lower() in {"1", "true", "yes", "on"},
            log_type=log_type,
            log_types=log_types,
            custom_source=_value("ERROR_ARCHIVER_CUSTOM_SOURCE", settings=settings),
            custom_timestamp_column=_value("ERROR_ARCHIVER_CUSTOM_TIMESTAMP_COLUMN", "event_time", settings),
            custom_sources=tuple({"name": str(item.get("name", "custom")), "source": str(item.get("source", "")), "timestamp_column": str(item.get("timestamp_column", "event_time"))} for item in custom_sources if isinstance(item, dict)),
            source_host=source_host,
            source_port=_positive_int("ERROR_ARCHIVER_SOURCE_PORT", 3306, settings),
            source_user=source_user, source_password=source_password, source_secret_ocid=source_secret_ocid,
            source_socket=_value("ERROR_ARCHIVER_SOURCE_SOCKET", settings=settings),
            archive_host=_value("ERROR_ARCHIVER_ARCHIVE_HOST", source_host, settings),
            archive_port=_positive_int("ERROR_ARCHIVER_ARCHIVE_PORT", 3306, settings),
            archive_user=archive_user, archive_password=archive_password, archive_secret_ocid=archive_secret_ocid,
            archive_socket=_value("ERROR_ARCHIVER_ARCHIVE_SOCKET", settings=settings),
            archive_db=_value("ERROR_ARCHIVER_ARCHIVE_DB", "archivedb", settings),
            archive_table=_value("ERROR_ARCHIVER_ARCHIVE_TABLE", "performance_schema_error_log_archive", settings),
            retention_months=_positive_int("ERROR_ARCHIVER_RETENTION_MONTHS", 12, settings),
            batch_size=_positive_int("ERROR_ARCHIVER_BATCH_SIZE", 5000, settings),
            schedule=_value("ERROR_ARCHIVER_SCHEDULE", "5min", settings),
        )
        if not config.source_user or not config.archive_user:
            raise ValueError("Source and archive users are required")
        if not re.fullmatch(r"[1-9][0-9]*\s*(min|mins|minute|minutes|h|hour|hours)", config.schedule.lower()):
            raise ValueError("ERROR_ARCHIVER_SCHEDULE must look like '5min' or '1hour'")
        if not config.log_types or any(value not in {"error_log", "slow_log", "general_log"} for value in config.log_types):
            raise ValueError("Select one or more of error_log, slow_log, or general_log")
        if config.custom_source and not re.fullmatch(r"[A-Za-z0-9_$]+\.[A-Za-z0-9_$]+", config.custom_source):
            raise ValueError("Custom source must be a schema.table or schema.view identifier")
        if not re.fullmatch(r"[A-Za-z0-9_$]+", config.custom_timestamp_column):
            raise ValueError("Custom timestamp column is invalid")
        for source in config.custom_sources:
            if not re.fullmatch(r"[A-Za-z0-9_$]+\.[A-Za-z0-9_$]+", source["source"]) or not re.fullmatch(r"[A-Za-z0-9_$]+", source["timestamp_column"]):
                raise ValueError("Each custom source requires schema.table and a timestamp column")
        return config


def save_settings(settings: dict[str, object]) -> None:
    """Atomically persist server-side settings (including DB passwords) with 0600 mode."""
    path = config_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(path)
