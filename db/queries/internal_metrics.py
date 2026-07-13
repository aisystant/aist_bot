from __future__ import annotations

"""
Persistence for health.internal_metrics (WP-7 GTW6).

Narrow internal-observability metrics table shared with event-gateway and the
projection workers — schema source of truth:
DS-IT-systems/neon-migrations/mvp/005-health-internal-metrics.sql

Lives in the `learning` Neon database (same one as learning.domain_event),
NOT the bot's own dedicated `health` BC database (HEALTH_URL / get_health_pool,
used for error_logs/user_sessions/pending_fixes) — those are two different
physical Neon databases despite the similar name.

Convention (matches the existing Grafana alert "Event Gateway Volume Anomaly"):
one row per occurrence — readers COUNT(*) rows in a time window rather than
reading a running counter value.
"""

from db.connection import get_learning_pool


async def record_internal_metric(metric_name: str, worker: str, value_numeric: float) -> None:
    """Insert one row into health.internal_metrics.

    The table's CHECK constraint requires value_numeric or value_jsonb to be
    non-null; this helper only supports the numeric form, which is sufficient
    for tally-style counters (pass 1.0 per occurrence). Add a value_jsonb
    parameter if a future caller needs structured context.

    Lets exceptions propagate — callers on a hot path are expected to wrap this
    in their own fire-and-forget/best-effort handling, e.g.
    clients.gateway_mcp.GatewayMCPClient._record_token_cache_miss_metric.
    """
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            '''INSERT INTO health.internal_metrics (metric_name, worker, value_numeric)
               VALUES ($1, $2, $3)''',
            metric_name, worker, value_numeric,
        )
