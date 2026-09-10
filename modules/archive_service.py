"""Archive error-log rows and maintain monthly archive partitions."""
from __future__ import annotations

import json
import csv
import io
import zipfile
from datetime import date, datetime, timezone

from .config import ArchiveConfig
from .job_state import load_state
from .mysql_util import archive_connection, ident, source_connection


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _add_months(value: date, months: int) -> date:
    month = value.month - 1 + months
    return date(value.year + month // 12, month % 12 + 1, 1)


def _partition_name(value: date) -> str:
    return f"p{value.year:04d}{value.month:02d}"


def _table(config: ArchiveConfig) -> str:
    return f"{ident(config.archive_db)}.{ident(config.archive_table)}"


def ensure_schema(config: ArchiveConfig) -> None:
    """Create the archive table and required monthly partitions idempotently."""
    db, table = ident(config.archive_db), ident(config.archive_table)
    with archive_connection(config) as conn:
        cur = conn.cursor()
        cur.execute(f"CREATE DATABASE IF NOT EXISTS {db} CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
        cur.execute(
            f"""CREATE TABLE IF NOT EXISTS {db}.{table} (
                event_time DATETIME(6) NOT NULL,
                log_type VARCHAR(255) NOT NULL,
                payload JSON NOT NULL,
                archived_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                source_fingerprint BINARY(32) NOT NULL,
                PRIMARY KEY (event_time, source_fingerprint),
                KEY ix_archived_at (archived_at)
            ) ENGINE=InnoDB PARTITION BY RANGE COLUMNS(event_time) (
                PARTITION p_bootstrap VALUES LESS THAN ('2000-01-01')
            )"""
        )
        cur.execute(f"ALTER TABLE {db}.{table} MODIFY log_type VARCHAR(255) NOT NULL")
        conn.commit()
    ensure_future_partitions(config)


def ensure_future_partitions(config: ArchiveConfig, months_ahead: int = 2) -> list[str]:
    """Add individual monthly partitions ahead of ingestion, safely skipping existing ones."""
    added: list[str] = []
    start = _month_start(datetime.now(timezone.utc).date())
    with archive_connection(config) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT PARTITION_NAME FROM information_schema.PARTITIONS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND PARTITION_NAME IS NOT NULL",
            (config.archive_db, config.archive_table),
        )
        existing = {row[0] for row in cur.fetchall()}
        # Keep a partition for every month that can be retained, plus a small future runway.
        for offset in range(-config.retention_months, months_ahead + 1):
            month = _add_months(start, offset)
            name = _partition_name(month)
            if name in existing:
                continue
            boundary = _add_months(month, 1).isoformat()
            # Boundary is generated from a date, never supplied by a browser/user.
            cur.execute(f"ALTER TABLE {_table(config)} ADD PARTITION (PARTITION {ident(name)} VALUES LESS THAN ('{boundary}'))")
            added.append(name)
        conn.commit()
    return added


def archive_error_log(config: ArchiveConfig) -> int:
    """Copy the configured MySQL error, slow, or general log table."""
    ensure_schema(config)
    copied = 0
    state = load_state()
    cursors = dict(state.get("source_cursors", {}))
    final_cursors = dict(cursors)
    with source_connection(config) as source, archive_connection(config) as archive:
        read = source.cursor(dictionary=True)
        write = archive.cursor()
        retention_floor = _month_start(_add_months(datetime.now(timezone.utc).date(), -config.retention_months))
        sources = {"error_log": ("performance_schema.error_log", "LOGGED"), "slow_log": ("mysql.slow_log", "start_time"), "general_log": ("mysql.general_log", "event_time")}
        selected = [(kind, *sources[kind]) for kind in config.log_types]
        for source in config.custom_sources:
            schema, table = source["source"].split(".")
            selected.append((f"custom:{source['name']}", f"{ident(schema)}.{ident(table)}", source["timestamp_column"]))
        sql = f"""INSERT IGNORE INTO {_table(config)}
            (event_time, log_type, payload, source_fingerprint)
            VALUES (%s, %s, %s, UNHEX(SHA2(%s, 256)))"""
        for kind, table_name, timestamp_column in selected:
            checkpoint = cursors.get(kind)
            final_cursor = checkpoint
            while True:
                floor = checkpoint or retention_floor
                comparator = ">" if checkpoint else ">="
                read.execute(f"SELECT * FROM {table_name} WHERE {timestamp_column} {comparator} %s ORDER BY {timestamp_column} ASC LIMIT %s", (floor, config.batch_size))
                rows = read.fetchall()
                if not rows:
                    break
                for row in rows:
                    event_time = row.pop(timestamp_column.upper(), row.pop(timestamp_column, None))
                    payload = json.dumps(row, default=str, sort_keys=True)
                    write.execute(sql, (event_time, kind, payload, f"{kind}|{event_time}|{payload}"))
                    copied += write.rowcount
                    final_cursor = event_time
                checkpoint = final_cursor
                if len(rows) < config.batch_size:
                    break
            if final_cursor:
                final_cursors[kind] = str(final_cursor)
        archive.commit()
    return copied, final_cursors


def prune_expired_partitions(config: ArchiveConfig) -> list[str]:
    """Drop whole monthly partitions older than the configured retention threshold."""
    cutoff = _month_start(_add_months(datetime.now(timezone.utc).date(), -config.retention_months))
    with archive_connection(config) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT PARTITION_NAME FROM information_schema.PARTITIONS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND PARTITION_NAME REGEXP '^p[0-9]{6}$' "
            "ORDER BY PARTITION_ORDINAL_POSITION",
            (config.archive_db, config.archive_table),
        )
        names = [row[0] for row in cur.fetchall()]
        expired = [name for name in names if date(int(name[1:5]), int(name[5:7]), 1) < cutoff]
        if expired:
            cur.execute(f"ALTER TABLE {_table(config)} DROP PARTITION " + ", ".join(ident(name) for name in expired))
            conn.commit()
        return expired


def run_archive_cycle(config: ArchiveConfig) -> dict[str, object]:
    ensure_schema(config)
    copied, source_cursors = archive_error_log(config)
    dropped = prune_expired_partitions(config)
    added = ensure_future_partitions(config)
    return {"copied": copied, "partitions_added": added, "partitions_dropped": dropped, "source_cursors": source_cursors, "source_log_types": list(config.log_types)}


def list_partitions(config: ArchiveConfig) -> list[dict[str, object]]:
    with archive_connection(config) as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT PARTITION_NAME AS partition_name, PARTITION_DESCRIPTION AS boundary, "
            "TABLE_ROWS AS table_rows, DATA_LENGTH AS data_length, CREATE_TIME AS create_time "
            "FROM information_schema.PARTITIONS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
            "AND PARTITION_NAME IS NOT NULL ORDER BY PARTITION_ORDINAL_POSITION",
            (config.archive_db, config.archive_table),
        )
        return cur.fetchall()


def recent_rows(config: ArchiveConfig, limit: int = 100) -> list[dict[str, object]]:
    limit = max(1, min(int(limit), 500))
    with archive_connection(config) as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(f"SELECT event_time, log_type, payload, archived_at FROM {_table(config)} ORDER BY event_time DESC LIMIT %s", (limit,))
        return cur.fetchall()


def fetch_archive_page(config: ArchiveConfig, page: int, page_size: int, partition_name: str = "", source_type: str = "") -> tuple[list[dict[str, object]], int]:
    page = max(1, page)
    page_size = max(10, min(page_size, 500))
    partition_clause = ""
    if partition_name:
        if partition_name != "p_bootstrap" and (not partition_name.startswith("p") or not partition_name[1:].isdigit()):
            raise ValueError("Invalid partition selection.")
        partition_clause = f" PARTITION ({ident(partition_name)})"
    where = " WHERE log_type = %s" if source_type else ""
    parameters: tuple[object, ...] = (source_type,) if source_type else ()
    with archive_connection(config) as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(f"SELECT COUNT(*) AS total FROM {_table(config)}{partition_clause}{where}", parameters)
        total = int(cur.fetchone()["total"])
        cur.execute(f"SELECT event_time, log_type, payload, archived_at FROM {_table(config)}{partition_clause}{where} ORDER BY event_time DESC LIMIT %s OFFSET %s", parameters + (page_size, (page - 1) * page_size))
        return cur.fetchall(), total


def truncate_partition(config: ArchiveConfig, partition_name: str) -> None:
    if partition_name == "p_bootstrap" or not partition_name.startswith("p") or not partition_name[1:].isdigit():
        raise ValueError("Only monthly archive partitions may be emptied.")
    with archive_connection(config) as conn:
        cur = conn.cursor()
        cur.execute(f"ALTER TABLE {_table(config)} TRUNCATE PARTITION {ident(partition_name)}")
        conn.commit()


def drop_partition(config: ArchiveConfig, partition_name: str) -> None:
    if partition_name == "p_bootstrap" or not partition_name.startswith("p") or not partition_name[1:].isdigit():
        raise ValueError("Only monthly archive partitions may be dropped.")
    with archive_connection(config) as conn:
        cur = conn.cursor()
        cur.execute(f"ALTER TABLE {_table(config)} DROP PARTITION {ident(partition_name)}")
        conn.commit()


def selected_partitions_zip(config: ArchiveConfig, names: list[str]) -> bytes:
    """Build one ZIP containing one CSV per selected monthly partition."""
    safe_names = []
    for name in names:
        if name == "p_bootstrap" or not name.startswith("p") or not name[1:].isdigit():
            raise ValueError("Only monthly archive partitions can be downloaded.")
        safe_names.append(name)
    buffer = io.BytesIO()
    with archive_connection(config) as conn, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in safe_names:
            cur = conn.cursor(dictionary=True)
            cur.execute(f"SELECT event_time, log_type, payload, archived_at FROM {_table(config)} PARTITION ({ident(name)}) ORDER BY event_time")
            text = io.StringIO()
            writer = csv.DictWriter(text, fieldnames=("event_time", "log_type", "payload", "archived_at"))
            writer.writeheader()
            for row in cur:
                writer.writerow(row)
            archive.writestr(f"{name}.csv", text.getvalue())
            cur.close()
    return buffer.getvalue()
