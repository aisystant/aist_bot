from __future__ import annotations

"""
Запросы для таблицы request_traces (трейсинг запросов).

Пороговые значения (мс):
  Навигация (/mode, /help, /profile, /settings, /language, cb:*):
    green <1000, yellow <3000, red >=3000
  Тяжёлые (/feed, /learn, /test, /assessment):
    green <3000, yellow <8000, red >=8000
  Консультант (msg:?*):
    green <8000, yellow <20000, red >=20000
"""

import hashlib
import html
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from db.connection import get_health_pool, get_learning_pool
from config import get_logger

logger = get_logger(__name__)

# --- Thresholds (ms) ---
# (green_max, yellow_max) — выше yellow_max = red
THRESHOLDS = {
    'nav':          (1000, 3000),    # /mode, /help, /profile, cb:*
    'heavy':        (3000, 8000),    # /feed, /learn, /test
    'consultation': (8000, 20000),   # msg:?*
}

_NAV_COMMANDS = {'/mode', '/help', '/profile', '/settings', '/language', '/mydata', '/feedback'}
_HEAVY_COMMANDS = {'/feed', '/learn', '/test', '/assessment'}
# Pattern-based: cb:marathon_next_* and cb:marathon_get_* are content-generating (heavy).
# Other cb:marathon_* (back_menu, settings_back, reset_confirm, etc.) remain nav.
_HEAVY_MARATHON_PATTERNS = ('cb:marathon_next_', 'cb:marathon_get_')
_HEAVY_CALLBACKS = {
    'cb:feed_confirm', 'cb:feed_get_digest', 'cb:go_profile',
}


def classify_command(command: str) -> str:
    """Classify command into threshold category.

    Categories (SLA):
    - nav: instant commands, <1s (e.g. /mode, /help, cb:inline_*)
    - heavy: content generation, <3s (e.g. /feed, cb:marathon_next_*, cb:marathon_get_*)
    - consultation: AI dialogue, <8s (e.g. msg:*)
    """
    if not command:
        return 'nav'
    cmd = command.split()[0] if command else ''
    if any(cmd.startswith(p) for p in _HEAVY_MARATHON_PATTERNS):
        return 'heavy'
    if cmd in _HEAVY_CALLBACKS:
        return 'heavy'
    if cmd.startswith('cb:'):
        return 'nav'
    if cmd.startswith('msg:'):
        return 'consultation'
    if cmd in _HEAVY_COMMANDS:
        return 'heavy'
    return 'nav'


def get_color(total_ms: float, category: str) -> str:
    """Return traffic light emoji for given latency."""
    green_max, yellow_max = THRESHOLDS.get(category, THRESHOLDS['nav'])
    if total_ms <= green_max:
        return '\U0001f7e2'  # green
    elif total_ms <= yellow_max:
        return '\U0001f7e1'  # yellow
    else:
        return '\U0001f534'  # red


async def cleanup_old_traces(days: int = 7) -> int:
    """Удалить traces старше N дней. Возвращает количество удалённых.

    WP-253 G4: cleanup на health БД.
    """
    pool = await get_health_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM public.request_traces WHERE created_at < NOW() - INTERVAL '1 day' * $1",
            days,
        )
        count = int(result.split()[-1]) if result else 0
        if count > 0:
            logger.info(f"[Traces] Cleaned up {count} traces older than {days} days")
        return count


async def get_latency_report(hours: int = 24) -> dict:
    """Get latency report for the last N hours.

    WP-253 B-port (28 апр): summary, by_command, all_traces (для red detection)
    мигрированы на learning.public.domain_event. slowest_spans остаётся на legacy
    request_traces — event payload содержит spans_count, не spans jsonb array.
    Полная миграция slowest_spans требует writer payload expansion (child-WP под G6).

    Returns dict with:
      - summary: {total_requests, avg_ms, p95_ms, red_count}
      - by_command: [{command, avg_ms, p95_ms, max_ms, count, color}]
      - red_traces: [{command, total_ms, state, created_at}] (last 5 red)
      - slowest_spans: [{name, avg_ms, max_ms, count}] (legacy, до payload expansion)
    """
    # WP-253: 3 из 4 запросов — на learning pool
    learning_pool = await get_learning_pool()
    async with learning_pool.acquire() as lc:
        summary = await lc.fetchrow("""
            SELECT COUNT(*) AS total,
                   AVG((payload->>'total_ms')::numeric)::int AS avg_ms,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY (payload->>'total_ms')::numeric)::int AS p95_ms
            FROM public.domain_event
            WHERE source = 'aist-bot' AND event_type = 'request_traced'
              AND ingested_at > NOW() - INTERVAL '1 hour' * $1
        """, hours)

        by_command = await lc.fetch("""
            SELECT payload->>'command' AS command,
                   AVG((payload->>'total_ms')::numeric)::int AS avg_ms,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY (payload->>'total_ms')::numeric)::int AS p95_ms,
                   MAX((payload->>'total_ms')::numeric)::int AS max_ms,
                   COUNT(*) AS count
            FROM public.domain_event
            WHERE source = 'aist-bot' AND event_type = 'request_traced'
              AND ingested_at > NOW() - INTERVAL '1 hour' * $1
              AND payload->>'command' IS NOT NULL
            GROUP BY 1
            ORDER BY avg_ms DESC
        """, hours)

        all_traces = await lc.fetch("""
            SELECT payload->>'command' AS command,
                   (payload->>'total_ms')::numeric AS total_ms,
                   payload->>'state' AS state,
                   ingested_at AS created_at
            FROM public.domain_event
            WHERE source = 'aist-bot' AND event_type = 'request_traced'
              AND ingested_at > NOW() - INTERVAL '1 hour' * $1
            ORDER BY ingested_at DESC
        """, hours)

    # slowest_spans читается из health БД (WP-253 G4 migration 8 мая):
    # writer (core/tracing.py:_save_trace_to_db) пишет в health.request_traces.
    # Event payload содержит только spans_count, поэтому slowest_spans
    # требует физическую таблицу со span jsonb массивом.
    health_pool = await get_health_pool()
    async with health_pool.acquire() as conn:
        slowest_spans = await conn.fetch("""
            SELECT s->>'name' AS name,
                   AVG((s->>'duration_ms')::numeric)::int AS avg_ms,
                   MAX((s->>'duration_ms')::numeric)::int AS max_ms,
                   COUNT(*) AS count
            FROM public.request_traces, jsonb_array_elements(spans) AS s
            WHERE created_at > NOW() - INTERVAL '1 hour' * $1
            GROUP BY name
            ORDER BY avg_ms DESC
            LIMIT 10
        """, hours)

    # Classify and find red
    red_traces = []
    red_count = 0
    for t in all_traces:
        cat = classify_command(t['command'])
        _, yellow_max = THRESHOLDS.get(cat, THRESHOLDS['nav'])
        if t['total_ms'] > yellow_max:
            red_count += 1
            if len(red_traces) < 5:
                red_traces.append(dict(t))

    summary_dict = dict(summary) if summary else {'total': 0, 'avg_ms': None, 'p95_ms': None}
    if summary_dict['total'] == 0:
        summary_dict['avg_ms'] = None
        summary_dict['p95_ms'] = None
    return {
        'summary': summary_dict,
        'red_count': red_count,
        'by_command': [dict(r) for r in by_command],
        'red_traces': red_traces,
        'slowest_spans': [dict(r) for r in slowest_spans],
    }


async def check_nav_latency_alerts(minutes: int = 15) -> Optional[str]:
    """Сообщить о серии медленных nav-команд без предположения о причине."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                payload->>'command' AS command,
                (payload->>'total_ms')::numeric AS total_ms,
                payload->>'state' AS state,
                ingested_at AS created_at
            FROM public.domain_event
            WHERE source = 'aist-bot'
              AND event_type = 'request_traced'
              AND ingested_at > NOW() - INTERVAL '1 minute' * $1
            ORDER BY (payload->>'total_ms')::numeric DESC
        """, minutes)

    if not rows:
        return None

    nav_red_items = []
    for r in rows:
        cat = classify_command(r['command'])
        if cat == 'nav':
            _, yellow_max = THRESHOLDS['nav']
            if r['total_ms'] > yellow_max:
                nav_red_items.append(r)

    if len(nav_red_items) < 3:
        return None

    lines = [f"\U0001f6a8 <b>Алерт: медленная навигация</b> ({len(nav_red_items)} nav-команд >{THRESHOLDS['nav'][1]}мс за {minutes} мин)\n"]
    lines.append(
        "\U0001f7e1 Причина не установлена: проверь spans, ожидание соединения "
        "и холодный старт."
    )
    for r in nav_red_items[:5]:
        ms = int(r['total_ms'])
        lines.append(f"  \U0001f534 {html.escape(r['command'])}: <b>{ms}мс</b>")

    lines.append(f"\n\U0001f449 /latency — полный отчёт")
    return "\n".join(lines)


async def check_latency_alerts(minutes: int = 15) -> Optional[str]:
    """Check recent traces for red-zone violations.

    WP-253 B-port (28 апр): чтение из learning.public.domain_event
    (event_type='request_traced', source='aist-bot'). Writer (core/tracing.py:_dual_write_trace)
    кладёт command/state/total_ms в payload jsonb.

    INVARIANT: writer ОБЯЗАН формировать payload с полями command, state, total_ms.

    Returns alert message (HTML) if there are red-zone requests, None otherwise.
    """
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                payload->>'command' AS command,
                (payload->>'total_ms')::numeric AS total_ms,
                payload->>'state' AS state,
                ingested_at AS created_at
            FROM public.domain_event
            WHERE source = 'aist-bot'
              AND event_type = 'request_traced'
              AND ingested_at > NOW() - INTERVAL '1 minute' * $1
            ORDER BY (payload->>'total_ms')::numeric DESC
        """, minutes)

    if not rows:
        return None

    red_items = []
    for r in rows:
        cat = classify_command(r['command'])
        _, yellow_max = THRESHOLDS.get(cat, THRESHOLDS['nav'])
        if r['total_ms'] > yellow_max:
            red_items.append(r)

    if not red_items:
        return None

    lines = [f"\U0001f6a8 <b>Алерт: латентность</b> ({len(red_items)} красных за {minutes} мин)\n"]
    for r in red_items[:5]:
        cat = classify_command(r['command'])
        ms = int(r['total_ms'])
        lines.append(f"  \U0001f534 {html.escape(r['command'])}: <b>{ms}мс</b> ({cat})")

    lines.append(f"\n\U0001f449 /latency — полный отчёт")
    return "\n".join(lines)


def _tool_name_fingerprint(tool_name: str) -> str:
    """Return a stable fingerprint without retaining the raw tool name."""
    return hashlib.sha256(str(tool_name).encode("utf-8")).hexdigest()


async def log_tool_call_audit(
    telegram_user_id: int,
    query: str,
    available_tools: List[str],
    chosen_tool: str,
    tool_input: Dict[str, Any],
    result_summary: str,
) -> None:
    """Audit which tool the LLM picked and what was available at call time (Л2.2,
    ArchGate 2026-07-07, DP.SC.129). Discovery decouples new-tool rollout from bot
    deploys, so there's no deploy diff to correlate a selection-accuracy regression
    against — this snapshot is the replacement signal.

    Privacy invariant (D6.5): domain_event receives metadata only. User text, tool
    input values, tool results, and Telegram identifiers must never enter either the
    payload or external_id. account_id is stored in the dedicated ownership column so
    RLS and erasure paths can identify the subject.
    """
    try:
        external_id = f"tool-audit-{uuid.uuid4().hex}"
        normalized_available_tools = available_tools or []
        normalized_tool_input = tool_input or {}
        payload = {
            "available_tool_count": len(normalized_available_tools),
            "available_tool_fingerprints": [
                _tool_name_fingerprint(tool_name)
                for tool_name in normalized_available_tools
            ],
            "chosen_tool_fingerprint": _tool_name_fingerprint(chosen_tool),
            "query_length": len(query or ""),
            "tool_input_key_count": len(normalized_tool_input),
            "tool_input_length": len(
                json.dumps(
                    normalized_tool_input,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
            ),
            "result_length": len(result_summary or ""),
        }

        from helpers.dual_write import resolve_ory_id_from_chat

        account_id = await resolve_ory_id_from_chat(telegram_user_id)
        if not account_id:
            logger.warning(
                "tool_call_audit skipped: canonical account_id unavailable"
            )
            return

        lp = await get_learning_pool()
        async with lp.acquire() as conn:
            await conn.execute(
                """INSERT INTO public.domain_event
                   (source, external_id, event_type, schema_version, account_id, occurred_at, payload)
                   VALUES ($1, $2, $3, $4, $5::uuid, $6, $7::jsonb)
                   ON CONFLICT (source, external_id) DO NOTHING""",
                "aist-bot",
                external_id,
                "tool_call_audit",
                "v2",
                account_id,
                datetime.now(timezone.utc),
                json.dumps(payload),
            )
    except Exception as exc:
        logger.warning(
            "tool_call_audit insert failed: %s",
            type(exc).__name__,
        )
