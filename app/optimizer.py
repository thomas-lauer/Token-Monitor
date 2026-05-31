"""Heuristic optimization engine.

Each rule queries the SQLite database with a window-relative cutoff and returns
zero or more Recommendation entries. Recommendations are sorted by estimated
monthly USD savings (descending).

Rules are intentionally conservative — better to surface a couple of strong
findings than flood the dashboard with noise.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from . import db


@dataclass
class Recommendation:
    rule_id: str
    severity: str           # "info" | "warning" | "critical"
    title: str
    body: str
    est_monthly_savings_usd: float = 0.0
    evidence: dict | None = None


def _params_for(since: Optional[str], provider: Optional[str] = None) -> tuple[str, list]:
    parts: list[str] = []
    params: list = []
    if since is not None:
        parts.append("timestamp >= ?")
        params.append(since)
    if provider:
        parts.append("provider = ?")
        params.append(provider)
    if not parts:
        return "", params
    return " AND " + " AND ".join(parts), params


def _window_days(since: Optional[str]) -> float:
    if since is None:
        return 30.0
    from datetime import datetime, timezone
    try:
        cutoff = datetime.fromisoformat(since.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - cutoff
        return max(delta.total_seconds() / 86400.0, 1.0)
    except ValueError:
        return 7.0


def _scale_to_month(window_days: float, observed_value: float) -> float:
    return observed_value * (30.0 / window_days)


def _rule_low_cache_hit_rate(since: Optional[str], window_days: float, provider: Optional[str]) -> list[Recommendation]:
    extra, params = _params_for(since, provider)
    with db.read_conn() as conn:
        row = conn.execute(
            f"""
            SELECT
              COALESCE(SUM(cache_read_tokens), 0) AS cr,
              COALESCE(SUM(input_tokens), 0) AS ip,
              COALESCE(SUM(cache_creation_tokens), 0) AS cc,
              COALESCE(SUM(cost_usd), 0) AS cost
            FROM api_requests
            WHERE 1=1 {extra}
            """,
            params,
        ).fetchone()
    pool = (row["cr"] or 0) + (row["ip"] or 0) + (row["cc"] or 0)
    if pool < 50_000:
        return []
    rate = row["cr"] / pool
    if rate >= 0.5:
        return []
    # Potential savings: bringing the cached portion of input to 80% would shift
    # ~ (0.8 - current_rate) of (input + cache_creation) from full price (avg
    # $3/MTok) to cache-read price ($0.30/MTok). Conservative 80% recovery factor.
    target = 0.8
    shiftable = (row["ip"] + row["cc"]) * max(target - rate, 0)
    saving = (shiftable / 1_000_000.0) * 2.7 * 0.8
    return [Recommendation(
        rule_id="low_cache_hit_rate",
        severity="warning" if rate < 0.3 else "info",
        title=f"Cache-Hit-Rate niedrig: {rate*100:.0f}%",
        body=(
            "Ein großer Anteil deines Inputs wird nicht aus dem Prompt-Cache "
            "gelesen. Stabilen Kontext (System-Prompts, CLAUDE.md, große "
            "Dokumente) an den Anfang der Konversation legen und längere "
            "Sessions statt vieler Kurz-Sessions führen, damit Cache-Hits "
            "entstehen können."
        ),
        est_monthly_savings_usd=_scale_to_month(window_days, saving),
        evidence={"cache_hit_rate": rate, "input_tokens": row["ip"], "cache_read": row["cr"]},
    )]


def _rule_opus_for_short_tasks(since: Optional[str], window_days: float, provider: Optional[str]) -> list[Recommendation]:
    extra, params = _params_for(since, provider)
    with db.read_conn() as conn:
        row = conn.execute(
            f"""
            SELECT
              SUM(CASE WHEN model LIKE 'claude-opus%' AND output_tokens < 500 THEN cost_usd ELSE 0 END) AS short_opus_cost,
              SUM(CASE WHEN model LIKE 'claude-opus%' AND output_tokens < 500 THEN 1 ELSE 0 END) AS short_opus_n,
              COUNT(*) AS total_n,
              SUM(cost_usd) AS total_cost
            FROM api_requests
            WHERE 1=1 {extra}
            """,
            params,
        ).fetchone()
    if not row or not row["total_n"] or not row["short_opus_n"]:
        return []
    share = (row["short_opus_n"] or 0) / row["total_n"]
    if share < 0.05:
        return []
    # Opus -> Sonnet is roughly 5x cheaper. Conservative: save 70% of those calls.
    saving = (row["short_opus_cost"] or 0) * 0.7
    return [Recommendation(
        rule_id="opus_for_short_tasks",
        severity="warning" if share >= 0.15 else "info",
        title=f"Opus für kurze Antworten ({row['short_opus_n']} Calls)",
        body=(
            f"{row['short_opus_n']} Opus-Anfragen lieferten <500 Output-Tokens. "
            "Für kurze Tasks reicht Sonnet (~5× günstiger) oder Haiku. "
            "Modellauswahl pro Skill/Agent festlegen oder Effort-Level senken."
        ),
        est_monthly_savings_usd=_scale_to_month(window_days, saving),
        evidence={"share": share, "short_opus_count": row["short_opus_n"]},
    )]


def _rule_high_effort_overuse(since: Optional[str], window_days: float, provider: Optional[str]) -> list[Recommendation]:
    extra, params = _params_for(since, provider)
    with db.read_conn() as conn:
        row = conn.execute(
            f"""
            SELECT
              SUM(CASE WHEN effort IN ('high','xhigh','max') THEN cost_usd ELSE 0 END) AS high_cost,
              SUM(CASE WHEN effort IN ('high','xhigh','max') THEN 1 ELSE 0 END) AS high_n,
              COUNT(*) AS total_n
            FROM api_requests
            WHERE 1=1 {extra}
            """,
            params,
        ).fetchone()
    if not row or not row["total_n"] or not row["high_n"]:
        return []
    share = row["high_n"] / row["total_n"]
    if share < 0.20:
        return []
    saving = (row["high_cost"] or 0) * 0.4
    return [Recommendation(
        rule_id="high_effort_overuse",
        severity="info",
        title=f"Hoher Anteil 'high/xhigh/max' Effort ({share*100:.0f}%)",
        body=(
            "Hoher Effort treibt Output-Tokens und Latenz. Setze Effort nur "
            "gezielt für komplexe Reasoning-Tasks ein und nutze sonst 'medium'."
        ),
        est_monthly_savings_usd=_scale_to_month(window_days, saving),
        evidence={"share": share},
    )]


def _rule_long_session_without_compact(since: Optional[str], window_days: float, provider: Optional[str]) -> list[Recommendation]:
    extra, params = _params_for(since, provider)
    with db.read_conn() as conn:
        sessions = conn.execute(
            f"""
            SELECT
              session_id,
              SUM(input_tokens) AS input_tokens,
              SUM(cost_usd) AS cost_usd
            FROM api_requests
            WHERE session_id IS NOT NULL {extra}
            GROUP BY session_id
            HAVING input_tokens > 200000
            ORDER BY input_tokens DESC
            LIMIT 10
            """,
            params,
        ).fetchall()
    if not sessions:
        return []
    affected = [s for s in sessions if (s["input_tokens"] or 0) > 200_000]
    if not affected:
        return []
    total_cost = sum(s["cost_usd"] or 0 for s in affected)
    saving = total_cost * 0.25
    return [Recommendation(
        rule_id="long_session_without_compact",
        severity="warning",
        title=f"{len(affected)} überlange Sessions (>200k Input-Tokens)",
        body=(
            "Sehr lange Sessions zahlen jeden Input-Token erneut für jeden "
            "Turn. Nutze /compact oder starte eine neue Session, sobald das "
            "Kontextfenster groß wird. Spart spürbar bei wiederkehrenden Tasks."
        ),
        est_monthly_savings_usd=_scale_to_month(window_days, saving),
        evidence={"affected_sessions": len(affected)},
    )]


def _rule_subagent_overload(since: Optional[str], window_days: float, provider: Optional[str]) -> list[Recommendation]:
    extra, params = _params_for(since, provider)
    with db.read_conn() as conn:
        row = conn.execute(
            f"""
            SELECT
              SUM(CASE WHEN query_source='subagent' THEN cost_usd ELSE 0 END) AS sub_cost,
              SUM(cost_usd) AS total_cost
            FROM api_requests
            WHERE 1=1 {extra}
            """,
            params,
        ).fetchone()
    if not row or not row["total_cost"] or row["total_cost"] < 0.50:
        return []
    share = (row["sub_cost"] or 0) / row["total_cost"]
    if share < 0.50:
        return []
    saving = (row["sub_cost"] or 0) * 0.20
    return [Recommendation(
        rule_id="subagent_overload",
        severity="info",
        title=f"Subagents verursachen {share*100:.0f}% der Kosten",
        body=(
            "Subagents sind nützlich, kosten aber zusätzliche Tokens (eigener "
            "System-Prompt + Context-Pass). Prüfe ob Hauptthread reicht oder "
            "Explore-Agent statt allgemeinem Sub-Claude verwendet werden kann."
        ),
        est_monthly_savings_usd=_scale_to_month(window_days, saving),
        evidence={"subagent_share": share},
    )]


def _rule_failing_tools(since: Optional[str], window_days: float, provider: Optional[str]) -> list[Recommendation]:
    extra, params = _params_for(since, provider)
    with db.read_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT tool_name, COUNT(*) AS calls,
                   SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS fails
            FROM tool_results
            WHERE 1=1 {extra}
            GROUP BY tool_name
            HAVING calls > 20 AND (CAST(fails AS REAL) / calls) > 0.15
            ORDER BY fails DESC
            LIMIT 5
            """,
            params,
        ).fetchall()
    if not rows:
        return []
    parts = ", ".join(f"{r['tool_name']} ({r['fails']}/{r['calls']})" for r in rows)
    return [Recommendation(
        rule_id="failing_tools",
        severity="warning",
        title=f"Tools mit hoher Fehlerrate: {parts}",
        body=(
            "Wiederkehrende Tool-Fehler führen zu Retry-Schleifen und doppelten "
            "Tokens. Ursache pro Tool prüfen: falsche Pfade, fehlende Pakete, "
            "Sandbox-Restriktionen. Lieber 1× richtig als 5× falsch."
        ),
        est_monthly_savings_usd=0.0,
        evidence={"tools": [dict(r) for r in rows]},
    )]


def _rule_oversize_reads(since: Optional[str], window_days: float, provider: Optional[str]) -> list[Recommendation]:
    extra, params = _params_for(since, provider)
    with db.read_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT COUNT(*) AS n, SUM(tool_result_size_bytes) AS total_bytes
            FROM tool_results
            WHERE tool_name='Read' AND tool_result_size_bytes > 200000 {extra}
            """,
            params,
        ).fetchone()
    if not rows or not rows["n"]:
        return []
    extra_input_tokens = (rows["total_bytes"] or 0) / 4
    saving = (extra_input_tokens / 1_000_000.0) * 3.00 * 0.5
    return [Recommendation(
        rule_id="oversize_reads",
        severity="info",
        title=f"{rows['n']} große Read-Calls (>200 KB)",
        body=(
            "Read von Riesendateien bläst den Context auf. Nutze offset/limit, "
            "Grep mit Range oder Glob, um die relevante Stelle zu finden."
        ),
        est_monthly_savings_usd=_scale_to_month(window_days, saving),
        evidence={"big_reads": rows["n"]},
    )]


def _rule_fast_mode_overuse(since: Optional[str], window_days: float, provider: Optional[str]) -> list[Recommendation]:
    extra, params = _params_for(since, provider)
    with db.read_conn() as conn:
        row = conn.execute(
            f"""
            SELECT
              SUM(CASE WHEN speed='fast' THEN cost_usd ELSE 0 END) AS fast_cost,
              SUM(cost_usd) AS total_cost
            FROM api_requests
            WHERE 1=1 {extra}
            """,
            params,
        ).fetchone()
    if not row or not row["total_cost"] or row["total_cost"] < 0.50:
        return []
    share = (row["fast_cost"] or 0) / row["total_cost"]
    if share < 0.50:
        return []
    saving = (row["fast_cost"] or 0) * 0.15
    return [Recommendation(
        rule_id="fast_mode_overuse",
        severity="info",
        title=f"Fast-Mode dominiert ({share*100:.0f}% Kostenanteil)",
        body=(
            "Fast-Mode ist teurer pro Token. Wenn Latenz nicht kritisch ist, "
            "lieber Standard-Mode nutzen. /fast nur gezielt einsetzen."
        ),
        est_monthly_savings_usd=_scale_to_month(window_days, saving),
        evidence={"fast_share": share},
    )]


def _rule_output_heavy(since: Optional[str], window_days: float, provider: Optional[str]) -> list[Recommendation]:
    extra, params = _params_for(since, provider)
    with db.read_conn() as conn:
        row = conn.execute(
            f"""
            SELECT
              COALESCE(SUM(input_tokens),0) AS ip,
              COALESCE(SUM(output_tokens),0) AS op,
              COALESCE(SUM(cost_usd),0) AS cost
            FROM api_requests
            WHERE 1=1 {extra}
            """,
            params,
        ).fetchone()
    if (row["ip"] or 0) < 10_000 or (row["op"] or 0) <= 3 * (row["ip"] or 1):
        return []
    saving = (row["cost"] or 0) * 0.05
    return [Recommendation(
        rule_id="output_heavy",
        severity="info",
        title="Output-lastige Sessions",
        body=(
            "Output-Tokens sind 5× teurer als Input. Wenn der Output >3× größer "
            "ist als der Input, lieber konkretere Anweisungen ('antworte kurz', "
            "'kein Summary am Ende') oder Antwort-Format vorgeben."
        ),
        est_monthly_savings_usd=_scale_to_month(window_days, saving),
        evidence={"in": row["ip"], "out": row["op"]},
    )]


_RULES = [
    _rule_low_cache_hit_rate,
    _rule_opus_for_short_tasks,
    _rule_high_effort_overuse,
    _rule_long_session_without_compact,
    _rule_subagent_overload,
    _rule_failing_tools,
    _rule_oversize_reads,
    _rule_fast_mode_overuse,
    _rule_output_heavy,
]


def recommendations(since: Optional[str], provider: Optional[str] = None) -> list[dict]:
    window_days = _window_days(since)
    out: list[Recommendation] = []
    for rule in _RULES:
        try:
            out.extend(rule(since, window_days, provider))
        except Exception:
            continue
    out.sort(key=lambda r: r.est_monthly_savings_usd, reverse=True)
    return [asdict(r) for r in out]
