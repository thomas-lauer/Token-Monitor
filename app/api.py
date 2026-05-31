"""Dashboard JSON API.

All endpoints accept:
  - `range`: lookback window, one of '24h' (default for most), '7d' (default
    for charts), '30d', '90d', 'all'.
  - `provider`: optional filter — 'anthropic', 'openai', or omitted (all).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from . import db
from .config import DB_PATH
from .optimizer import recommendations


router = APIRouter(prefix="/api")


_RANGE_MAP = {
    "24h": timedelta(hours=24),
    "7d":  timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}
_VALID_PROVIDERS = {"anthropic", "openai"}


def _since(range_: str) -> Optional[str]:
    if range_ == "all":
        return None
    delta = _RANGE_MAP.get(range_, _RANGE_MAP["24h"])
    return (datetime.now(timezone.utc) - delta).isoformat()


def _where_clause(
    range_: str,
    provider: Optional[str] = None,
    ts_col: str = "timestamp",
) -> tuple[str, list]:
    """Build a `WHERE` clause covering both range and provider filters."""
    parts: list[str] = []
    params: list = []
    since = _since(range_)
    if since is not None:
        parts.append(f"{ts_col} >= ?")
        params.append(since)
    if provider:
        if provider not in _VALID_PROVIDERS:
            raise HTTPException(400, f"Unknown provider: {provider}")
        parts.append("provider = ?")
        params.append(provider)
    if not parts:
        return "", params
    return " WHERE " + " AND ".join(parts) + " ", params


@router.get("/health")
def health() -> dict:
    with db.read_conn() as conn:
        per_provider = conn.execute(
            "SELECT provider, COUNT(*) AS n, MAX(timestamp) AS latest "
            "FROM api_requests GROUP BY provider"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) AS n FROM api_requests").fetchone()["n"]
        latest = conn.execute(
            "SELECT MAX(timestamp) AS t FROM api_requests"
        ).fetchone()["t"]
    return {
        "status": "ok",
        "db_path": str(DB_PATH),
        "db_size_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
        "api_request_count": total,
        "latest_event_ts": latest,
        "providers": [
            {"provider": r["provider"], "requests": r["n"], "latest": r["latest"]}
            for r in per_provider
        ],
        "last_claude_scan_at": db.get_meta("last_jsonl_scan_at"),
        "last_claude_files_seen": db.get_meta("last_jsonl_files_seen"),
        "last_codex_scan_at": db.get_meta("last_codex_scan_at"),
        "last_codex_files_seen": db.get_meta("last_codex_files_seen"),
    }


@router.get("/summary")
def summary(
    range: str = Query("24h"),
    provider: Optional[str] = Query(None),
) -> dict:
    where, params = _where_clause(range, provider)
    with db.read_conn() as conn:
        row = conn.execute(
            f"""
            SELECT
              COALESCE(SUM(input_tokens),0)          AS input_tokens,
              COALESCE(SUM(output_tokens),0)         AS output_tokens,
              COALESCE(SUM(cache_read_tokens),0)     AS cache_read_tokens,
              COALESCE(SUM(cache_creation_tokens),0) AS cache_creation_tokens,
              COALESCE(SUM(cost_usd),0)              AS total_cost_usd,
              COUNT(DISTINCT session_id)             AS sessions,
              COUNT(*)                               AS requests
            FROM api_requests
            {where}
            """,
            params,
        ).fetchone()

        top_model = conn.execute(
            f"""
            SELECT model, SUM(cost_usd) AS c
            FROM api_requests
            {where}
            GROUP BY model ORDER BY c DESC LIMIT 1
            """,
            params,
        ).fetchone()

        per_provider = conn.execute(
            f"""
            SELECT provider,
                   COALESCE(SUM(cost_usd),0) AS cost_usd,
                   COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens),0) AS tokens,
                   COUNT(*) AS requests
            FROM api_requests
            {where}
            GROUP BY provider
            """,
            params,
        ).fetchall()

    total_tokens = (
        row["input_tokens"] + row["output_tokens"]
        + row["cache_read_tokens"] + row["cache_creation_tokens"]
    )
    cache_pool = row["cache_read_tokens"] + row["input_tokens"] + row["cache_creation_tokens"]
    cache_hit_rate = (
        row["cache_read_tokens"] / cache_pool if cache_pool > 0 else 0.0
    )

    return {
        "range": range,
        "provider": provider,
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "cache_read_tokens": row["cache_read_tokens"],
        "cache_creation_tokens": row["cache_creation_tokens"],
        "total_tokens": total_tokens,
        "total_cost_usd": row["total_cost_usd"],
        "sessions": row["sessions"],
        "requests": row["requests"],
        "cache_hit_rate": cache_hit_rate,
        "avg_cost_per_session": (
            row["total_cost_usd"] / row["sessions"] if row["sessions"] else 0.0
        ),
        "top_model": top_model["model"] if top_model else None,
        "providers": [
            {"provider": r["provider"], "cost_usd": r["cost_usd"],
             "tokens": r["tokens"], "requests": r["requests"]}
            for r in per_provider
        ],
    }


@router.get("/timeseries")
def timeseries(
    range: str = Query("7d"),
    metric: str = Query("tokens", regex="^(tokens|cost)$"),
    bucket: str = Query("day", regex="^(hour|day)$"),
    provider: Optional[str] = Query(None),
    groupBy: Optional[str] = Query(None, regex="^(provider)$"),
) -> dict:
    where, params = _where_clause(range, provider)
    bucket_expr = "substr(timestamp, 1, 13)" if bucket == "hour" else "substr(timestamp, 1, 10)"

    if groupBy == "provider":
        with db.read_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT
                  {bucket_expr} AS bucket,
                  provider,
                  SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens) AS tokens,
                  SUM(cost_usd) AS cost_usd
                FROM api_requests
                {where}
                GROUP BY bucket, provider
                ORDER BY bucket ASC
                """,
                params,
            ).fetchall()

        buckets: list[str] = []
        providers: list[str] = []
        seen_buckets: dict[str, dict[str, float]] = {}
        for r in rows:
            b = r["bucket"]
            if b not in seen_buckets:
                seen_buckets[b] = {}
                buckets.append(b)
            if r["provider"] not in providers:
                providers.append(r["provider"])
            seen_buckets[b][r["provider"]] = (
                r["tokens"] if metric == "tokens" else round(r["cost_usd"] or 0.0, 4)
            )
        return {
            "labels": buckets,
            "series": [
                {"name": p, "data": [seen_buckets[b].get(p, 0) for b in buckets]}
                for p in providers
            ],
        }

    with db.read_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT
              {bucket_expr} AS bucket,
              SUM(input_tokens)          AS input_tokens,
              SUM(output_tokens)         AS output_tokens,
              SUM(cache_read_tokens)     AS cache_read_tokens,
              SUM(cache_creation_tokens) AS cache_creation_tokens,
              SUM(cost_usd)              AS cost_usd
            FROM api_requests
            {where}
            GROUP BY bucket
            ORDER BY bucket ASC
            """,
            params,
        ).fetchall()

    if metric == "tokens":
        return {
            "labels": [r["bucket"] for r in rows],
            "series": [
                {"name": "Input",          "data": [r["input_tokens"] or 0 for r in rows]},
                {"name": "Output",         "data": [r["output_tokens"] or 0 for r in rows]},
                {"name": "Cache Read",     "data": [r["cache_read_tokens"] or 0 for r in rows]},
                {"name": "Cache Creation", "data": [r["cache_creation_tokens"] or 0 for r in rows]},
            ],
        }
    return {
        "labels": [r["bucket"] for r in rows],
        "series": [{"name": "Cost USD", "data": [round(r["cost_usd"] or 0.0, 4) for r in rows]}],
    }


@router.get("/breakdown")
def breakdown(
    range: str = Query("7d"),
    dim: str = Query("model"),
    limit: int = Query(20, ge=1, le=200),
    provider: Optional[str] = Query(None),
) -> dict:
    col_map = {
        "model":    "model",
        "session":  "session_id",
        "project":  "project_path",
        "skill":    "skill_name",
        "agent":    "agent_name",
        "mcp":      "mcp_server",
        "source":   "source",
        "speed":    "speed",
        "effort":   "effort",
        "query_source": "query_source",
        "provider": "provider",
    }
    col = col_map.get(dim)
    if not col:
        raise HTTPException(400, f"Unknown dim: {dim}")

    where, params = _where_clause(range, provider)
    with db.read_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT
              COALESCE({col}, '(none)') AS label,
              SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens) AS tokens,
              SUM(cost_usd) AS cost_usd,
              COUNT(*) AS requests
            FROM api_requests
            {where}
            GROUP BY label
            ORDER BY cost_usd DESC
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()
    return {
        "dim": dim,
        "rows": [
            {"label": r["label"], "tokens": r["tokens"] or 0,
             "cost_usd": r["cost_usd"] or 0.0, "requests": r["requests"]}
            for r in rows
        ],
    }


@router.get("/tools")
def tools(
    range: str = Query("7d"),
    limit: int = Query(30, ge=1, le=200),
    provider: Optional[str] = Query(None),
) -> dict:
    where, params = _where_clause(range, provider)
    with db.read_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT
              tool_name,
              COUNT(*) AS calls,
              SUM(COALESCE(success,1)) AS successes,
              AVG(duration_ms) AS avg_duration_ms,
              SUM(tool_result_size_bytes) AS result_bytes
            FROM tool_results
            {where}
            GROUP BY tool_name
            ORDER BY calls DESC
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()
    return {
        "rows": [
            {
                "tool_name": r["tool_name"],
                "calls": r["calls"],
                "successes": r["successes"] or 0,
                "failure_rate": 1.0 - ((r["successes"] or r["calls"]) / r["calls"])
                    if r["calls"] else 0.0,
                "avg_duration_ms": round(r["avg_duration_ms"], 1) if r["avg_duration_ms"] else None,
                "result_bytes": r["result_bytes"] or 0,
            }
            for r in rows
        ],
    }


@router.get("/sessions")
def sessions(
    range: str = Query("7d"),
    limit: int = Query(100, ge=1, le=500),
    provider: Optional[str] = Query(None),
) -> dict:
    where, params = _where_clause(range, provider)
    with db.read_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT
              session_id,
              project_path,
              provider,
              MIN(timestamp) AS started_at,
              MAX(timestamp) AS last_seen_at,
              SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens) AS tokens,
              SUM(cost_usd) AS cost_usd,
              COUNT(*) AS requests,
              GROUP_CONCAT(DISTINCT model) AS models
            FROM api_requests
            {where}
            GROUP BY session_id
            ORDER BY last_seen_at DESC
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()
    return {
        "rows": [
            {
                "session_id": r["session_id"],
                "project_path": r["project_path"],
                "provider": r["provider"],
                "started_at": r["started_at"],
                "last_seen_at": r["last_seen_at"],
                "tokens": r["tokens"] or 0,
                "cost_usd": r["cost_usd"] or 0.0,
                "requests": r["requests"],
                "models": r["models"],
            }
            for r in rows
        ],
    }


@router.get("/session/{session_id}")
def session_detail(session_id: str) -> dict:
    with db.read_conn() as conn:
        meta = conn.execute(
            """
            SELECT
              session_id,
              project_path,
              provider,
              MIN(timestamp) AS started_at,
              MAX(timestamp) AS last_seen_at,
              SUM(input_tokens) AS input_tokens,
              SUM(output_tokens) AS output_tokens,
              SUM(cache_read_tokens) AS cache_read_tokens,
              SUM(cache_creation_tokens) AS cache_creation_tokens,
              SUM(cost_usd) AS cost_usd,
              COUNT(*) AS requests
            FROM api_requests
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()

        requests_rows = conn.execute(
            """
            SELECT timestamp, model, input_tokens, output_tokens,
                   cache_read_tokens, cache_creation_tokens, cost_usd,
                   query_source, agent_name, skill_name, source, provider
            FROM api_requests
            WHERE session_id = ?
            ORDER BY timestamp ASC
            LIMIT 1000
            """,
            (session_id,),
        ).fetchall()

        tool_rows = conn.execute(
            """
            SELECT tool_name, COUNT(*) AS calls, SUM(COALESCE(success,1)) AS successes
            FROM tool_results WHERE session_id = ?
            GROUP BY tool_name ORDER BY calls DESC
            """,
            (session_id,),
        ).fetchall()

    return {
        "meta": dict(meta) if meta else None,
        "requests": [dict(r) for r in requests_rows],
        "tools": [dict(r) for r in tool_rows],
    }


@router.get("/heatmap")
def heatmap(
    range: str = Query("30d"),
    provider: Optional[str] = Query(None),
) -> dict:
    """Tokens by day-of-week × hour-of-day."""
    where, params = _where_clause(range, provider)
    with db.read_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT
              CAST(strftime('%w', timestamp) AS INTEGER) AS dow,
              CAST(strftime('%H', timestamp) AS INTEGER) AS hour,
              SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens) AS tokens
            FROM api_requests
            {where}
            GROUP BY dow, hour
            """,
            params,
        ).fetchall()
    return {"rows": [{"dow": r["dow"], "hour": r["hour"], "tokens": r["tokens"] or 0} for r in rows]}


@router.get("/recommendations")
def recommendations_endpoint(
    range: str = Query("7d"),
    provider: Optional[str] = Query(None),
) -> dict:
    since = _since(range)
    return {"range": range, "provider": provider, "items": recommendations(since, provider)}
