"""OTLP/HTTP receiver for Claude Code telemetry.

Implements the subset of the OTLP HTTP spec that Claude Code actually emits:
  POST /v1/metrics  (Content-Type: application/x-protobuf)
  POST /v1/logs     (Content-Type: application/x-protobuf)
  POST /v1/traces   (accepted but currently discarded — out of scope)

Responses are minimal protobuf successes (empty body, 200 OK) which all OTel
SDKs accept.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response

from . import db
from .pricing import cost_for
from .proto_decode import decode_logs, decode_metrics


log = logging.getLogger("token_monitor.otlp")
router = APIRouter()


def _ns_to_iso(ns: int | None) -> str:
    if not ns:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat()


def _project_path_from_attrs(attrs: dict) -> str | None:
    return (
        attrs.get("project.path")
        or attrs.get("workspace.path")
        or attrs.get("service.workspace")
    )


def _handle_api_request_event(attrs: dict, ts: str) -> None:
    model = attrs.get("model") or "unknown"
    input_tokens = int(attrs.get("input_tokens") or 0)
    output_tokens = int(attrs.get("output_tokens") or 0)
    cache_read = int(attrs.get("cache_read_tokens") or 0)
    cache_create = int(attrs.get("cache_creation_tokens") or 0)
    cost_attr = attrs.get("cost_usd")
    if cost_attr is None:
        cost = cost_for(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_create,
        )
    else:
        cost = float(cost_attr)

    row = {
        "timestamp": ts,
        "session_id": attrs.get("session.id"),
        "prompt_id": attrs.get("prompt.id"),
        "request_id": attrs.get("request_id"),
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_create,
        "cost_usd": cost,
        "duration_ms": int(attrs.get("duration_ms") or 0) or None,
        "query_source": attrs.get("query_source"),
        "agent_name": attrs.get("agent.name"),
        "skill_name": attrs.get("skill.name"),
        "plugin_name": attrs.get("plugin.name"),
        "mcp_server": attrs.get("mcp_server.name"),
        "mcp_tool": attrs.get("mcp_tool.name"),
        "effort": attrs.get("effort"),
        "speed": attrs.get("speed"),
        "project_path": _project_path_from_attrs(attrs),
        "source": "otel",
    }
    db.upsert_api_request(row)


def _handle_tool_result_event(attrs: dict, ts: str) -> None:
    success_str = str(attrs.get("success") or "").lower()
    success = 1 if success_str in ("true", "1", "yes") else 0 if success_str else None
    db.insert_tool_result({
        "timestamp": ts,
        "session_id": attrs.get("session.id"),
        "prompt_id": attrs.get("prompt.id"),
        "tool_use_id": attrs.get("tool_use_id"),
        "tool_name": attrs.get("tool_name") or "unknown",
        "success": success,
        "duration_ms": int(attrs.get("duration_ms") or 0) or None,
        "error_type": attrs.get("error_type"),
        "tool_input_size_bytes": int(attrs.get("tool_input_size_bytes") or 0) or None,
        "tool_result_size_bytes": int(attrs.get("tool_result_size_bytes") or 0) or None,
        "project_path": _project_path_from_attrs(attrs),
        "source": "otel",
    })


def _handle_user_prompt_event(attrs: dict, ts: str) -> None:
    db.insert_prompt({
        "timestamp": ts,
        "session_id": attrs.get("session.id"),
        "prompt_id": attrs.get("prompt.id"),
        "prompt_length": int(attrs.get("prompt_length") or 0) or None,
        "command_name": attrs.get("command_name"),
        "command_source": attrs.get("command_source"),
        "project_path": _project_path_from_attrs(attrs),
        "source": "otel",
    })


def _handle_api_error_event(attrs: dict, ts: str) -> None:
    db.insert_api_error({
        "timestamp": ts,
        "session_id": attrs.get("session.id"),
        "model": attrs.get("model"),
        "status_code": int(attrs.get("status_code") or 0) or None,
        "error": attrs.get("error"),
        "attempt": int(attrs.get("attempt") or 0) or None,
        "source": "otel",
    })


_LOG_HANDLERS = {
    "claude_code.api_request": _handle_api_request_event,
    "api_request": _handle_api_request_event,
    "claude_code.tool_result": _handle_tool_result_event,
    "tool_result": _handle_tool_result_event,
    "claude_code.user_prompt": _handle_user_prompt_event,
    "user_prompt": _handle_user_prompt_event,
    "claude_code.api_error": _handle_api_error_event,
    "api_error": _handle_api_error_event,
}


def _handle_token_metric(attrs: dict, value: int | float, ts: str) -> None:
    """Token metric is per-type (input/output/cacheRead/cacheCreation).

    We aggregate into api_requests when we can identify the request, otherwise
    we store a synthetic row keyed only by timestamp+session+model so totals
    remain accurate even without correlating events.
    """
    model = attrs.get("model") or "unknown"
    type_ = attrs.get("type") or ""
    tokens = int(value or 0)

    field_map = {
        "input": "input_tokens",
        "output": "output_tokens",
        "cacheRead": "cache_read_tokens",
        "cacheCreation": "cache_creation_tokens",
    }
    field = field_map.get(type_)
    if not field:
        return

    row = {
        "timestamp": ts,
        "session_id": attrs.get("session.id"),
        "prompt_id": None,
        "request_id": None,
        "model": model,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cost_usd": None,
        "duration_ms": None,
        "query_source": attrs.get("query_source"),
        "agent_name": attrs.get("agent.name"),
        "skill_name": attrs.get("skill.name"),
        "plugin_name": attrs.get("plugin.name"),
        "mcp_server": attrs.get("mcp_server.name"),
        "mcp_tool": attrs.get("mcp_tool.name"),
        "effort": attrs.get("effort"),
        "speed": attrs.get("speed"),
        "project_path": _project_path_from_attrs(attrs),
        "source": "otel-metric",
    }
    row[field] = tokens
    row["cost_usd"] = cost_for(
        model,
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cache_read_tokens=row["cache_read_tokens"],
        cache_creation_tokens=row["cache_creation_tokens"],
    )
    db.upsert_api_request(row)


def _handle_cost_metric(attrs: dict, value: float, ts: str) -> None:
    """`claude_code.cost.usage` arrives without a request_id. We just record it
    as a standalone cost row so total spend is preserved (the api_request log
    event remains the primary source when present)."""
    model = attrs.get("model") or "unknown"
    db.upsert_api_request({
        "timestamp": ts,
        "session_id": attrs.get("session.id"),
        "prompt_id": None,
        "request_id": None,
        "model": model,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cost_usd": float(value or 0.0),
        "duration_ms": None,
        "query_source": attrs.get("query_source"),
        "agent_name": attrs.get("agent.name"),
        "skill_name": attrs.get("skill.name"),
        "plugin_name": attrs.get("plugin.name"),
        "mcp_server": attrs.get("mcp_server.name"),
        "mcp_tool": attrs.get("mcp_tool.name"),
        "effort": attrs.get("effort"),
        "speed": attrs.get("speed"),
        "project_path": _project_path_from_attrs(attrs),
        "source": "otel-cost",
    })


_METRIC_HANDLERS = {
    "claude_code.token.usage": _handle_token_metric,
    "claude_code.cost.usage": _handle_cost_metric,
}


_OK_RESPONSE = Response(content=b"", media_type="application/x-protobuf", status_code=200)


@router.post("/v1/metrics")
async def receive_metrics(request: Request) -> Response:
    body = await request.body()
    try:
        points = decode_metrics(body)
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to decode metrics payload: %s", exc)
        return _OK_RESPONSE

    for point in points:
        handler = _METRIC_HANDLERS.get(point["name"])
        if handler is None:
            continue
        ts = _ns_to_iso(point.get("timestamp_ns"))
        try:
            handler(point.get("attrs") or {}, point.get("value") or 0, ts)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to process metric %s: %s", point["name"], exc)

    return _OK_RESPONSE


@router.post("/v1/logs")
async def receive_logs(request: Request) -> Response:
    body = await request.body()
    try:
        records = decode_logs(body)
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to decode logs payload: %s", exc)
        return _OK_RESPONSE

    for record in records:
        event_name = record.get("event_name") or ""
        handler = _LOG_HANDLERS.get(event_name)
        if handler is None:
            continue
        ts = _ns_to_iso(record.get("timestamp_ns"))
        try:
            handler(record.get("attrs") or {}, ts)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to process event %s: %s", event_name, exc)

    return _OK_RESPONSE


@router.post("/v1/traces")
async def receive_traces(request: Request) -> Response:
    await request.body()
    return _OK_RESPONSE
