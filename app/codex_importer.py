"""Importer for OpenAI Codex CLI's session rollouts.

Codex stores one JSONL per session under:

  ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl

Each line is `{ "timestamp", "type", "payload" }` where `type` is one of:

  - "session_meta"  : session id, cwd, originator, cli_version, model_provider
  - "turn_context"  : per-turn model + effort + reasoning settings
  - "event_msg"     : payload.type ∈ { token_count, exec_command_end, ... }
  - "response_item" : payload.type ∈ { function_call, message, reasoning, ... }

Token bookkeeping lives in `event_msg` records whose `payload.type ==
"token_count"`. The first such record only carries rate-limit info; later
records carry both `info.total_token_usage` (cumulative) and
`info.last_token_usage` (delta for the just-finished turn). We treat each
event with non-null `info` as a single API request and key it on
`session_id + timestamp` for idempotent re-imports.

Tool calls map to `exec_command_end` (for shell commands) and to
`response_item` with `payload.type` in {`function_call`,
`custom_tool_call`} for non-shell tool invocations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import db
from .config import CODEX_SESSIONS_DIR, JSONL_POLL_INTERVAL_SECONDS
from .pricing import cost_for


log = logging.getLogger("token_monitor.codex")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request_id(session_id: str | None, timestamp: str) -> str:
    """Stable request_id so reruns/upserts don't create duplicates."""
    h = hashlib.sha1(f"codex|{session_id}|{timestamp}".encode("utf-8")).hexdigest()[:16]
    return f"codex_{h}"


class _SessionState:
    """Per-file state carried across the JSONL lines."""
    __slots__ = ("session_id", "cwd", "model", "effort", "cli_version")

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.cwd: str | None = None
        self.model: str | None = None
        self.effort: str | None = None
        self.cli_version: str | None = None


def _handle_session_meta(record: dict, st: _SessionState) -> None:
    payload = record.get("payload") or {}
    st.session_id = payload.get("id") or st.session_id
    st.cwd = payload.get("cwd") or st.cwd
    st.cli_version = payload.get("cli_version") or st.cli_version


def _handle_turn_context(record: dict, st: _SessionState) -> None:
    payload = record.get("payload") or {}
    st.model = payload.get("model") or st.model
    st.effort = payload.get("effort") or st.effort


def _handle_token_count(record: dict, st: _SessionState) -> None:
    payload = record.get("payload") or {}
    info = payload.get("info")
    if not info:
        return  # rate-limit-only heartbeat

    last = info.get("last_token_usage") or {}
    if not last or last.get("total_tokens") in (None, 0):
        return

    # Codex reports input_tokens as the *full* input (cached + new). Split it
    # so our schema's cache_read column reflects the cached portion.
    raw_input = int(last.get("input_tokens") or 0)
    cached_input = int(last.get("cached_input_tokens") or 0)
    new_input = max(raw_input - cached_input, 0)
    visible_output = int(last.get("output_tokens") or 0)
    reasoning_output = int(last.get("reasoning_output_tokens") or 0)

    ts = record.get("timestamp") or _now_iso()
    model = st.model or "gpt-5"

    cost = cost_for(
        model,
        input_tokens=new_input,
        output_tokens=visible_output + reasoning_output,
        cache_read_tokens=cached_input,
        cache_creation_tokens=0,
        provider="openai",
    )

    db.upsert_api_request({
        "timestamp": ts,
        "session_id": st.session_id,
        "prompt_id": None,
        "request_id": _request_id(st.session_id, ts),
        "model": model,
        "input_tokens": new_input,
        "output_tokens": visible_output + reasoning_output,
        "cache_read_tokens": cached_input,
        "cache_creation_tokens": 0,
        "cost_usd": cost,
        "duration_ms": None,
        "query_source": "main",
        "agent_name": None,
        "skill_name": None,
        "plugin_name": None,
        "mcp_server": None,
        "mcp_tool": None,
        "effort": st.effort,
        "speed": None,
        "project_path": st.cwd,
        "source": "codex-jsonl",
        "provider": "openai",
    })


def _handle_exec_command_end(record: dict, st: _SessionState) -> None:
    payload = record.get("payload") or {}
    duration = payload.get("duration") or {}
    secs = int(duration.get("secs") or 0)
    nanos = int(duration.get("nanos") or 0)
    duration_ms = secs * 1000 + (nanos // 1_000_000) if (secs or nanos) else None
    exit_code = payload.get("exit_code")

    out = payload.get("aggregated_output")
    out_size = len(out.encode("utf-8", errors="ignore")) if isinstance(out, str) else None

    db.insert_tool_result({
        "timestamp": record.get("timestamp") or _now_iso(),
        "session_id": st.session_id,
        "prompt_id": None,
        "tool_use_id": payload.get("call_id"),
        "tool_name": "Bash",
        "success": 1 if exit_code == 0 else 0 if exit_code is not None else None,
        "duration_ms": duration_ms,
        "error_type": None if exit_code == 0 else (f"exit_{exit_code}" if exit_code is not None else None),
        "tool_input_size_bytes": None,
        "tool_result_size_bytes": out_size,
        "project_path": st.cwd,
        "source": "codex-jsonl",
        "provider": "openai",
    })


def _handle_function_call(record: dict, st: _SessionState) -> None:
    """response_item / function_call entries. The matching function_call_output
    arrives later but we don't need to merge — exec_command_end already covers
    shell timings, and other tools won't have duration anyway."""
    payload = record.get("payload") or {}
    name = payload.get("name") or "unknown"
    if name == "shell_command":
        # Already counted via exec_command_end with a richer payload.
        return
    args = payload.get("arguments")
    input_size = len(args.encode("utf-8", errors="ignore")) if isinstance(args, str) else None

    db.insert_tool_result({
        "timestamp": record.get("timestamp") or _now_iso(),
        "session_id": st.session_id,
        "prompt_id": None,
        "tool_use_id": payload.get("call_id"),
        "tool_name": name,
        "success": None,
        "duration_ms": None,
        "error_type": None,
        "tool_input_size_bytes": input_size,
        "tool_result_size_bytes": None,
        "project_path": st.cwd,
        "source": "codex-jsonl",
        "provider": "openai",
    })


def _iter_lines_with_offsets(path: Path, start_offset: int) -> Iterator[tuple[int, str]]:
    with path.open("rb") as fh:
        fh.seek(start_offset)
        while True:
            line = fh.readline()
            if not line:
                break
            offset = fh.tell()
            try:
                yield offset, line.decode("utf-8", errors="replace")
            except UnicodeDecodeError:
                continue


def import_file(path: Path) -> int:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return 0

    cursor_key = f"codex::{path}"
    cursor = db.get_cursor(cursor_key)
    start_offset = 0
    if cursor is not None:
        old_mtime, old_offset = cursor
        if old_mtime == stat.st_mtime and old_offset == stat.st_size:
            return 0
        if old_mtime == stat.st_mtime and old_offset < stat.st_size:
            start_offset = old_offset
        # else full re-scan

    state = _SessionState()
    # If we're resuming partway, we still need session metadata. Cheap: a
    # second pass over the first ~50 lines to seed session_meta + first
    # turn_context. This is bounded so it doesn't blow up on huge files.
    if start_offset > 0:
        with path.open("rb") as fh:
            for _ in range(200):
                line = fh.readline()
                if not line:
                    break
                try:
                    rec = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if rec.get("type") == "session_meta":
                    _handle_session_meta(rec, state)
                elif rec.get("type") == "turn_context":
                    _handle_turn_context(rec, state)
                if state.session_id and state.model:
                    break

    imported = 0
    last_offset = start_offset
    for offset, raw in _iter_lines_with_offsets(path, start_offset):
        last_offset = offset
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        rtype = record.get("type")
        try:
            if rtype == "session_meta":
                _handle_session_meta(record, state)
            elif rtype == "turn_context":
                _handle_turn_context(record, state)
            elif rtype == "event_msg":
                ptype = (record.get("payload") or {}).get("type")
                if ptype == "token_count":
                    _handle_token_count(record, state)
                elif ptype == "exec_command_end":
                    _handle_exec_command_end(record, state)
            elif rtype == "response_item":
                ptype = (record.get("payload") or {}).get("type")
                if ptype in ("function_call", "custom_tool_call"):
                    _handle_function_call(record, state)
        except Exception as exc:  # noqa: BLE001
            log.warning("Codex line skipped (%s in %s): %s", rtype, path.name, exc)
            continue
        imported += 1

    db.set_cursor(cursor_key, stat.st_mtime, last_offset, _now_iso())
    return imported


def scan_once() -> dict[str, int]:
    if not CODEX_SESSIONS_DIR.exists():
        return {"files": 0, "lines": 0}

    files_seen = 0
    lines_imported = 0
    for jsonl_path in CODEX_SESSIONS_DIR.rglob("rollout-*.jsonl"):
        files_seen += 1
        try:
            lines_imported += import_file(jsonl_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Codex import error for %s: %s", jsonl_path, exc)

    db.set_meta("last_codex_scan_at", _now_iso())
    db.set_meta("last_codex_files_seen", str(files_seen))
    return {"files": files_seen, "lines": lines_imported}


async def importer_loop() -> None:
    log.info("Codex importer starting; watching %s", CODEX_SESSIONS_DIR)
    while True:
        started = time.monotonic()
        try:
            stats = await asyncio.to_thread(scan_once)
            elapsed = time.monotonic() - started
            log.info(
                "Codex scan: %s files, %s lines imported in %.2fs",
                stats["files"], stats["lines"], elapsed,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected codex importer error: %s", exc)
        await asyncio.sleep(JSONL_POLL_INTERVAL_SECONDS)
