"""Importer for Claude Code's local JSONL session transcripts.

Transcripts live under ~/.claude/projects/<project-slug>/<session-uuid>.jsonl
and contain one JSON object per line. Relevant types:

  - "user"      : user prompt or a tool_result wrapped in message.content
  - "assistant" : model response with message.usage (the token-bearing entries)
  - "system"    : system notes (ignored)
  - others      : permission-mode, file-history-snapshot, ai-title, attachment (ignored)

This module scans the directory, remembers per-file byte offsets in jsonl_cursor,
and incrementally imports new lines on each pass.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import db
from .config import CLAUDE_PROJECTS_DIR, JSONL_POLL_INTERVAL_SECONDS
from .pricing import cost_for


log = logging.getLogger("token_monitor.jsonl")


def _decode_project_path(project_slug: str) -> str:
    """Claude Code mangles project paths into directory names.

    Examples:
      C--Users-ThomasLauer        -> C:\\Users\\ThomasLauer
      D--OneDrive---TLDS--KI...   -> D:\\OneDrive - TLDS\\_KI... (heuristic)

    The mapping is lossy (path separators and spaces both become dashes), so we
    only do a best-effort reconstruction useful for display, not for fs access.
    """
    if len(project_slug) >= 2 and project_slug[1] == "-":
        return project_slug[0] + ":\\" + project_slug[2:].replace("-", "\\")
    return project_slug


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_ts(ts: str | None) -> str:
    if not ts:
        return _now_iso()
    return ts


def _content_size(content: Any) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content.encode("utf-8", errors="ignore"))
    try:
        return len(json.dumps(content, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _process_assistant(record: dict, project_path: str) -> None:
    msg = record.get("message") or {}
    usage = msg.get("usage") or {}
    if not usage:
        return

    model = msg.get("model") or "unknown"
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_create = int(usage.get("cache_creation_input_tokens") or 0)
    speed_raw = usage.get("speed") or msg.get("speed")
    speed = "fast" if speed_raw == "fast" else ("normal" if speed_raw else None)

    cost = cost_for(
        model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_create,
    )

    row = {
        "timestamp": _normalize_ts(record.get("timestamp")),
        "session_id": record.get("sessionId"),
        "prompt_id": record.get("promptId"),
        "request_id": msg.get("id"),
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_create,
        "cost_usd": cost,
        "duration_ms": None,
        "query_source": "subagent" if record.get("isSidechain") else "main",
        "agent_name": None,
        "skill_name": None,
        "plugin_name": None,
        "mcp_server": None,
        "mcp_tool": None,
        "effort": None,
        "speed": speed,
        "project_path": project_path,
        "source": "jsonl",
    }
    db.upsert_api_request(row)

    for block in msg.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            db.insert_tool_result({
                "timestamp": row["timestamp"],
                "session_id": row["session_id"],
                "prompt_id": row["prompt_id"],
                "tool_use_id": block.get("id"),
                "tool_name": block.get("name") or "unknown",
                "success": None,
                "duration_ms": None,
                "error_type": None,
                "tool_input_size_bytes": _content_size(block.get("input")),
                "tool_result_size_bytes": None,
                "project_path": project_path,
                "source": "jsonl",
            })


def _process_user(record: dict, project_path: str) -> None:
    msg = record.get("message") or {}
    ts = _normalize_ts(record.get("timestamp"))
    session_id = record.get("sessionId")
    prompt_id = record.get("promptId")

    content = msg.get("content")
    is_tool_result_entry = (
        isinstance(content, list)
        and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content)
    )

    if is_tool_result_entry:
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            is_error = bool(block.get("is_error"))
            db.insert_tool_result({
                "timestamp": ts,
                "session_id": session_id,
                "prompt_id": prompt_id,
                "tool_use_id": block.get("tool_use_id"),
                "tool_name": None,
                "success": 0 if is_error else 1,
                "duration_ms": None,
                "error_type": "tool_error" if is_error else None,
                "tool_input_size_bytes": None,
                "tool_result_size_bytes": _content_size(block.get("content")),
                "project_path": project_path,
                "source": "jsonl",
            })
        return

    if record.get("isMeta"):
        return

    prompt_text = content if isinstance(content, str) else _content_size(content)
    prompt_length = len(content) if isinstance(content, str) else _content_size(content)

    db.insert_prompt({
        "timestamp": ts,
        "session_id": session_id,
        "prompt_id": prompt_id,
        "prompt_length": prompt_length,
        "command_name": None,
        "command_source": None,
        "project_path": project_path,
        "source": "jsonl",
    })


def _iter_lines_with_offsets(path: Path, start_offset: int) -> Iterator[tuple[int, str]]:
    """Yield (byte_offset_after_line, raw_line) starting at start_offset."""
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
    """Import new lines from a single JSONL file. Returns line count imported."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return 0

    cursor = db.get_cursor(str(path))
    start_offset = 0
    if cursor is not None:
        old_mtime, old_offset = cursor
        if old_mtime == stat.st_mtime and old_offset == stat.st_size:
            return 0
        if old_mtime == stat.st_mtime and old_offset < stat.st_size:
            start_offset = old_offset
        # else: file was rewritten — fall through to full re-scan from 0

    project_slug = path.parent.name
    project_path = _decode_project_path(project_slug)

    imported = 0
    last_offset = start_offset
    for offset, line in _iter_lines_with_offsets(path, start_offset):
        last_offset = offset
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        rtype = record.get("type")
        try:
            if rtype == "assistant":
                _process_assistant(record, project_path)
            elif rtype == "user":
                _process_user(record, project_path)
        except Exception as exc:  # noqa: BLE001 — never let one bad line abort import
            log.warning("Failed to process %s record in %s: %s", rtype, path.name, exc)
            continue
        imported += 1

    db.set_cursor(str(path), stat.st_mtime, last_offset, _now_iso())
    return imported


def scan_once() -> dict[str, int]:
    """Single pass over all JSONL files. Returns summary stats."""
    if not CLAUDE_PROJECTS_DIR.exists():
        log.info("Claude projects dir not found: %s", CLAUDE_PROJECTS_DIR)
        return {"files": 0, "lines": 0}

    files_seen = 0
    lines_imported = 0
    for jsonl_path in CLAUDE_PROJECTS_DIR.rglob("*.jsonl"):
        files_seen += 1
        try:
            lines_imported += import_file(jsonl_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Import error for %s: %s", jsonl_path, exc)

    db.set_meta("last_jsonl_scan_at", _now_iso())
    db.set_meta("last_jsonl_files_seen", str(files_seen))
    return {"files": files_seen, "lines": lines_imported}


async def importer_loop() -> None:
    """Background task: initial full scan, then poll every N seconds."""
    log.info("JSONL importer starting; watching %s", CLAUDE_PROJECTS_DIR)
    while True:
        started = time.monotonic()
        try:
            stats = await asyncio.to_thread(scan_once)
            elapsed = time.monotonic() - started
            log.info(
                "JSONL scan: %s files, %s lines imported in %.2fs",
                stats["files"], stats["lines"], elapsed,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected importer error: %s", exc)

        await asyncio.sleep(JSONL_POLL_INTERVAL_SECONDS)
