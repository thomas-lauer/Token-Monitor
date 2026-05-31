"""SQLite schema and helpers for Token-Monitor.

Single writer model: all writes go through this module so the connection can
own the lock. Readers use short-lived connections per request.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .config import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS api_requests (
    id INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,
    session_id TEXT,
    prompt_id TEXT,
    request_id TEXT,
    model TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    cost_usd REAL,
    duration_ms INTEGER,
    query_source TEXT,
    agent_name TEXT,
    skill_name TEXT,
    plugin_name TEXT,
    mcp_server TEXT,
    mcp_tool TEXT,
    effort TEXT,
    speed TEXT,
    project_path TEXT,
    source TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_api_request_id_uq
    ON api_requests(request_id);
CREATE INDEX IF NOT EXISTS idx_api_ts ON api_requests(timestamp);
CREATE INDEX IF NOT EXISTS idx_api_session ON api_requests(session_id);
CREATE INDEX IF NOT EXISTS idx_api_model ON api_requests(model);

CREATE TABLE IF NOT EXISTS tool_results (
    id INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,
    session_id TEXT,
    prompt_id TEXT,
    tool_use_id TEXT,
    tool_name TEXT NOT NULL,
    success INTEGER,
    duration_ms INTEGER,
    error_type TEXT,
    tool_input_size_bytes INTEGER,
    tool_result_size_bytes INTEGER,
    project_path TEXT,
    source TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_use_uq
    ON tool_results(tool_use_id);
CREATE INDEX IF NOT EXISTS idx_tool_ts ON tool_results(timestamp);
CREATE INDEX IF NOT EXISTS idx_tool_name ON tool_results(tool_name);

CREATE TABLE IF NOT EXISTS prompts (
    id INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,
    session_id TEXT,
    prompt_id TEXT,
    prompt_length INTEGER,
    command_name TEXT,
    command_source TEXT,
    project_path TEXT,
    source TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_prompt_id_uq
    ON prompts(prompt_id);
CREATE INDEX IF NOT EXISTS idx_prompts_ts ON prompts(timestamp);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    project_path TEXT,
    started_at TEXT,
    last_seen_at TEXT,
    entrypoint TEXT
);

CREATE TABLE IF NOT EXISTS api_errors (
    id INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,
    session_id TEXT,
    model TEXT,
    status_code INTEGER,
    error TEXT,
    attempt INTEGER,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_err_ts ON api_errors(timestamp);

CREATE TABLE IF NOT EXISTS jsonl_cursor (
    file_path TEXT PRIMARY KEY,
    mtime REAL,
    byte_offset INTEGER,
    last_imported_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


_write_lock = threading.Lock()


def _connect(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def read_conn():
    """Short-lived read connection; safe across threads (one per call)."""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_conn():
    """Serialized write connection.

    SQLite tolerates concurrent readers under WAL, but multiple writers race.
    Funnel all writes through the lock so we never see SQLITE_BUSY in normal
    operation. Background importers and the OTLP receiver both use this.
    """
    with _write_lock:
        conn = _connect()
        try:
            yield conn
        finally:
            conn.close()


def upsert_api_request(row: dict[str, Any]) -> None:
    """Insert or merge an api_request row, keyed by request_id when present.

    Fields with values 'win' over None values from a competing source so that
    OTel and JSONL imports can refine the same row.
    """
    cols = [
        "timestamp", "session_id", "prompt_id", "request_id", "model",
        "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
        "cost_usd", "duration_ms", "query_source", "agent_name", "skill_name",
        "plugin_name", "mcp_server", "mcp_tool", "effort", "speed",
        "project_path", "source",
    ]
    values = [row.get(c) for c in cols]
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)

    update_cols = [c for c in cols if c not in ("request_id", "timestamp", "source")]
    update_set = ", ".join(
        f"{c} = COALESCE(excluded.{c}, api_requests.{c})" for c in update_cols
    )

    sql = f"""
        INSERT INTO api_requests ({col_list})
        VALUES ({placeholders})
        ON CONFLICT(request_id) DO UPDATE SET {update_set}
    """
    with write_conn() as conn:
        if row.get("request_id"):
            conn.execute(sql, values)
        else:
            conn.execute(
                f"INSERT INTO api_requests ({col_list}) VALUES ({placeholders})",
                values,
            )
        if row.get("session_id"):
            conn.execute(
                """
                INSERT INTO sessions (session_id, project_path, started_at, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    project_path = COALESCE(excluded.project_path, sessions.project_path),
                    last_seen_at = MAX(sessions.last_seen_at, excluded.last_seen_at)
                """,
                (
                    row["session_id"],
                    row.get("project_path"),
                    row["timestamp"],
                    row["timestamp"],
                ),
            )


def insert_tool_result(row: dict[str, Any]) -> None:
    cols = [
        "timestamp", "session_id", "prompt_id", "tool_use_id", "tool_name",
        "success", "duration_ms", "error_type", "tool_input_size_bytes",
        "tool_result_size_bytes", "project_path", "source",
    ]
    values = [row.get(c) for c in cols]
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)

    update_cols = [c for c in cols if c not in ("tool_use_id", "timestamp", "source")]
    update_set = ", ".join(
        f"{c} = COALESCE(excluded.{c}, tool_results.{c})" for c in update_cols
    )

    sql = f"""
        INSERT INTO tool_results ({col_list})
        VALUES ({placeholders})
        ON CONFLICT(tool_use_id) DO UPDATE SET {update_set}
    """
    with write_conn() as conn:
        if row.get("tool_use_id"):
            conn.execute(sql, values)
        else:
            conn.execute(
                f"INSERT INTO tool_results ({col_list}) VALUES ({placeholders})",
                values,
            )


def insert_prompt(row: dict[str, Any]) -> None:
    cols = [
        "timestamp", "session_id", "prompt_id", "prompt_length",
        "command_name", "command_source", "project_path", "source",
    ]
    values = [row.get(c) for c in cols]
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)
    sql = f"""
        INSERT INTO prompts ({col_list}) VALUES ({placeholders})
        ON CONFLICT(prompt_id) DO NOTHING
    """
    with write_conn() as conn:
        if row.get("prompt_id"):
            conn.execute(sql, values)
        else:
            conn.execute(
                f"INSERT INTO prompts ({col_list}) VALUES ({placeholders})",
                values,
            )


def insert_api_error(row: dict[str, Any]) -> None:
    cols = [
        "timestamp", "session_id", "model", "status_code",
        "error", "attempt", "source",
    ]
    values = [row.get(c) for c in cols]
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)
    with write_conn() as conn:
        conn.execute(
            f"INSERT INTO api_errors ({col_list}) VALUES ({placeholders})",
            values,
        )


def get_cursor(file_path: str) -> tuple[float, int] | None:
    with read_conn() as conn:
        row = conn.execute(
            "SELECT mtime, byte_offset FROM jsonl_cursor WHERE file_path = ?",
            (file_path,),
        ).fetchone()
        return (row["mtime"], row["byte_offset"]) if row else None


def set_cursor(file_path: str, mtime: float, byte_offset: int, ts: str) -> None:
    with write_conn() as conn:
        conn.execute(
            """
            INSERT INTO jsonl_cursor (file_path, mtime, byte_offset, last_imported_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(file_path) DO UPDATE SET
                mtime = excluded.mtime,
                byte_offset = excluded.byte_offset,
                last_imported_at = excluded.last_imported_at
            """,
            (file_path, mtime, byte_offset, ts),
        )


def set_meta(key: str, value: str) -> None:
    with write_conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_meta(key: str) -> str | None:
    with read_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
