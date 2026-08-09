from __future__ import annotations

"""
Error Classifier: маппинг error_logs → категории RUNBOOK (DP.RUNBOOK.001).

Запускается из scheduler каждые 5 мин:
1. classify_unprocessed() — классифицировать новые ошибки
2. check_escalation() — L3/L4 escalation → TG dev

Source-of-truth паттернов: DP.RUNBOOK.001-aist-bot-errors.md § 3

Grafana queries (для PostgreSQL datasource → Neon):

  Error rate by category (time series):
    SELECT date_trunc('hour', last_seen_at) AS time, category,
           SUM(occurrence_count) AS total
    FROM error_logs WHERE $__timeFilter(last_seen_at)
    GROUP BY 1, 2 ORDER BY 1

  Severity distribution (pie chart):
    SELECT severity, COUNT(*) FROM error_logs
    WHERE last_seen_at > NOW() - INTERVAL '24 hours'
    GROUP BY 1

  Unknown errors (table, triage):
    SELECT logger_name, LEFT(message, 200), occurrence_count, last_seen_at
    FROM error_logs WHERE category = 'unknown'
    AND last_seen_at > NOW() - INTERVAL '7 days'
    ORDER BY occurrence_count DESC
"""

import html
import re
import logging
from typing import Optional

from db.connection import get_health_pool

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# RUNBOOK PATTERNS (DP.RUNBOOK.001 § 3)
# ═══════════════════════════════════════════════════════════

PATTERNS: list[dict] = [
    # ORDER MATTERS: specific categories first, generic (DB) last.
    # First match wins — patterns with explicit keywords (MCP, Claude, aiogram)
    # must precede generic patterns (connection timeout, etc.).

    # --- FSM (§ 3.1) ---
    {"category": "fsm", "severity": "L1",
     "pattern": r"(?i)no handler for state|dead.?end|unhandled.*state",
     "action": "Reset → mode_select"},
    {"category": "fsm", "severity": "L1",
     "pattern": r"(?i)Unstick.*Recover|stuck.*state|state.*stuck",
     "action": "Auto-recovery (unstick.py)"},
    {"category": "fsm", "severity": "L2",
     "pattern": r"(?i)state.*corrupt|FSM.*mismatch|state.*sync",
     "action": "PR: sync FSM state с DB"},
    {"category": "fsm", "severity": "L2",
     "pattern": r"(?i)стейт не найден|state.*not found|state.*not registered",
     "action": "PR: зарегистрировать стейт или убрать команду"},

    # --- Discourse — before generic Claude 429 pattern ---
    {"category": "scheduler", "severity": "L1",
     "pattern": r"(?i)\[Discourse\] Comment polling paused after rate[-\s]?limit",
     "action": "Отложить пакет; не менять счётчик отсутствующих топиков"},

    # --- Claude API (§ 3.3) — before DB (claude timeout ≠ db timeout) ---
    {"category": "claude_api", "severity": "L1",
     "pattern": r"(?i)rate_limit|RateLimitError|status.?code.*429",
     "action": "Retry с backoff (auto)"},
    {"category": "claude_api", "severity": "L1",
     "pattern": r"(?i)overloaded|OverloadedError|status.?code.*529",
     "action": "Degrade: cached content"},
    {"category": "claude_api", "severity": "L1",
     "pattern": r"(?i)transient error.*50[023]|status.?code.*50[023]|API error 50[023]",
     "action": "Retry с backoff (auto)"},
    {"category": "claude_api", "severity": "L1",
     "pattern": r"(?i)APITimeoutError|anthropic.*timeout|claude.*timeout",
     "action": "Retry 1x, затем fallback"},
    {"category": "claude_api", "severity": "L1",
     "pattern": r"(?i)Content generation returned None|generate_content.*returned None",
     "action": "Haiku on-the-fly вернул None (max_tokens/timeout) → Sonnet fallback"},
    {"category": "claude_api", "severity": "L2",
     "pattern": r"(?i)invalid.*response.*claude|json.*decode.*anthropic",
     "action": "PR: fix response parsing"},

    # --- Telegram API (§ 3.4) ---
    {"category": "telegram_api", "severity": "L1",
     "pattern": r"(?i)ConflictError|conflict.*polling|Failed to fetch updates",
     "action": "Transient: Railway redeploy (auto-resolve)"},
    {"category": "telegram_api", "severity": "L1",
     "pattern": r"(?i)RetryAfter|flood.?control",
     "action": "Задержка N секунд (auto: aiogram)"},
    {"category": "telegram_api", "severity": "L1",
     "pattern": r"(?i)bot was blocked|Forbidden.*blocked|user.*deactivated",
     "action": "Skip + пометить (auto)"},
    {"category": "telegram_api", "severity": "L1",
     "pattern": r"(?i)chat not found|Bad Request.*chat",
     "action": "Skip + лог (auto)"},
    {"category": "telegram_api", "severity": "L2",
     "pattern": r"(?i)message.*too long|MESSAGE_TOO_LONG",
     "action": "PR: add text truncation"},
    {"category": "telegram_api", "severity": "L1",
     "pattern": r"(?i)can't parse entities|Unsupported start tag|parse.*entities.*error",
     "action": "Экранировать HTML в тексте сообщения (html.escape)"},

    # --- Digital Twin (§ 3.5a) — DT MCP OAuth/token errors ---
    {"category": "dt", "severity": "L1",
     "pattern": r"(?i)DT.*token.*refresh.*fail|DT.*proactive refresh failed",
     "action": "Auto: disconnect + notify user to /twin"},
    {"category": "dt", "severity": "L1",
     "pattern": r"(?i)OryOAuth.*invalid_grant|OryOAuth.*Token refresh.*invalid_grant",
     "action": "Auto: disconnect + notify user to re-auth"},
    {"category": "dt", "severity": "L2",
     "pattern": r"(?i)DT.*token.*exchange.*fail|DT OAuth.*fail",
     "action": "Check DT MCP server status"},
    {"category": "dt", "severity": "L1",
     "pattern": r"(?i)DT.*persist.*fail|DT.*failed to persist|DT.*failed to delete",
     "action": "Check Neon connection"},
    {"category": "dt", "severity": "L1",
     "pattern": r"(?i)DigitalTwin.*circuit breaker|DT.*circuit breaker",
     "action": "Per-user CB: auto-recovery after 120s"},

    # --- MCP (§ 3.5) — before DB (MCP connection fail ≠ db connection fail) ---
    {"category": "mcp", "severity": "L3",
     "pattern": r"(?i)MCP.*connection.*fail|MCP.*connect.*error|MCP.*refused",
     "action": "Retry 3x, fallback без MCP"},
    {"category": "mcp", "severity": "L1",
     "pattern": r"(?i)MCP.*timeout|knowledge.*timeout",
     "action": "Fallback без MCP knowledge"},
    {"category": "mcp", "severity": "L2",
     "pattern": r"(?i)MCP.*invalid.*response|MCP.*parse",
     "action": "PR: fix MCP response parsing"},

    # --- Scheduler (§ 3.6) — before DB (scheduler errors contain generic words) ---
    {"category": "scheduler", "severity": "L1",
     "pattern": r"(?i)\[Scheduler\].*error|\[PreGen\].*(?:timeout|failed)",
     "action": "Retry в след. цикл (auto)"},
    {"category": "scheduler", "severity": "L2",
     "pattern": r"(?i)offset-naive and offset-aware|can't subtract.*datetime",
     "action": "PR: привести datetime к naive (CLAUDE.md § 10.5)"},
    {"category": "scheduler", "severity": "L1",
     "pattern": r"(?i)MarathonQueue.*[Ff]ailed to send|[Ff]ailed to send checkin",
     "action": "Проверить: пользователь заблокировал бота / TelegramRetryAfter"},
    {"category": "scheduler", "severity": "L4",
     "pattern": r"(?i)scheduler.*stuck|scheduler.*not.*start|asyncio.*deadlock",
     "action": "Escalate: проверить Railway logs"},

    # --- Deployment (§ 3.7) — webhook/infrastructure issues ---
    {"category": "deployment", "severity": "L1",
     "pattern": r"(?i)secret token.*unallowed|Failed to set webhook|webhook.*error",
     "action": "Проверить WEBHOOK_SECRET env var (деплой)"},
    {"category": "deployment", "severity": "L1",
     "pattern": r"(?i)terminated by.*setWebhook|terminated by other getUpdates",
     "action": "Transient: переключение polling↔webhook (Railway redeploy)"},

    # --- DB (§ 3.2) — last: generic patterns (connection, timeout) ---
    {"category": "db", "severity": "L3",
     "pattern": r"(?i)too many connections|pool.*exhaust|connection pool",
     "action": "Restart бот (освободить pool)"},
    {"category": "db", "severity": "L3",
     "pattern": r"(?i)connection.*timed?\s*out|connect.*refused|ConnectionRefusedError",
     "action": "Restart + проверить Neon status"},
    {"category": "db", "severity": "L2",
     "pattern": r"(?i)statement.*timeout|canceling statement due to",
     "action": "PR: optimize query / add index"},
    {"category": "db", "severity": "L4",
     "pattern": r"(?i)relation.*does not exist|UndefinedTableError",
     "action": "Escalate: ручной запуск CREATE TABLE"},
]

# Pre-compiled patterns for performance
_COMPILED = [
    {**p, "_re": re.compile(p["pattern"])} for p in PATTERNS
]

# Logger name prefix → category hint (fallback when no pattern matches)
LOGGER_HINTS: dict[str, str] = {
    "core.unstick": "fsm",
    "db.": "db",
    "asyncpg": "db",
    "clients.claude": "claude_api",
    "anthropic": "claude_api",
    "aiogram": "telegram_api",
    "clients.mcp": "mcp",
    "core.scheduler": "scheduler",
    "engines.feed": "scheduler",
    "states.": "scheduler",
    "core.tracing": "fsm",
}

SEVERITY_EMOJI = {"L4": "\U0001f534", "L3": "\U0001f7e0", "L2": "\U0001f7e1", "L1": "\U0001f7e2"}

# ═══════════════════════════════════════════════════════════
# SUPPRESSION ALLOWLIST (WP-45 Ф2)
# ═══════════════════════════════════════════════════════════
# Known benign errors that should be classified but NOT escalated/alerted.
# These are expected in normal operation and generate noise in monitoring.
# Pattern: compiled regex matched against f"{logger_name}: {message}"

_SUPPRESSED_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?i)bot was blocked by the user"),
    re.compile(r"(?i)user.*deactivated"),
    re.compile(r"(?i)chat not found"),
    re.compile(r"(?i)Forbidden.*blocked"),
    re.compile(r"(?i)ConflictError.*polling"),  # transient Railway redeploy
    re.compile(r"(?i)terminated by other.*getUpdates"),  # webhook/polling switch
    re.compile(r"(?i)RetryAfter|flood.?control"),  # TG rate limit, auto-handled
    # peer-session 2026-06-05-02: два класса, что монитор ложно клеил как claude_api/L2.
    re.compile(r"(?i)WakaTime API error 422|missing a timezone"),  # внешнее per-user: нет таймзоны в чужом WakaTime-аккаунте, наш код не чинит
    re.compile(r"(?i)Unclosed (client session|connector)"),  # resource leak — лечится закрытием сессий в shutdown, не код-баг класса Claude
]


def is_suppressed(logger_name: str, message: str) -> bool:
    """Check if error matches suppression allowlist (benign, expected errors).

    Suppressed errors are still classified and stored in error_logs,
    but excluded from escalation alerts to reduce noise.
    """
    search_text = f"{logger_name}: {message}"
    return any(p.search(search_text) for p in _SUPPRESSED_PATTERNS)


# ═══════════════════════════════════════════════════════════
# CLASSIFICATION
# ═══════════════════════════════════════════════════════════

def classify_error(logger_name: str, message: str, traceback_text: str | None) -> dict:
    """Classify a single error by matching against RUNBOOK patterns.

    Returns {"category": str, "severity": str|None, "action": str|None}
    """
    search_text = f"{message}\n{traceback_text or ''}"

    # 1. Regex patterns (precise match)
    for p in _COMPILED:
        if p["_re"].search(search_text):
            return {
                "category": p["category"],
                "severity": p["severity"],
                "action": p["action"],
            }

    # 2. Logger name hints (fallback)
    for prefix, category in LOGGER_HINTS.items():
        if logger_name.startswith(prefix):
            return {
                "category": category,
                "severity": "L1",
                "action": "Проверить error_logs",
            }

    # 3. Unknown
    return {"category": "unknown", "severity": None, "action": None}


_HAIKU_CLASSIFY_PROMPT = """Classify this bot error into a RUNBOOK category.

Logger: {logger_name}
Message: {message}
Traceback (last 3 lines): {traceback_tail}

Respond in JSON only:
{{"category": "fsm|claude_api|telegram_api|mcp|scheduler|db|deployment", "severity": "L1|L2|L3|L4", "action": "<one sentence fix suggestion>"}}

Rules:
- L1: transient, auto-recoverable (retry, skip)
- L2: code bug, needs PR fix
- L3: infrastructure issue (restart, check service)
- L4: critical, manual intervention needed"""

_VALID_CATEGORIES = {"fsm", "claude_api", "telegram_api", "mcp", "scheduler", "db", "deployment", "dt"}
_VALID_SEVERITIES = {"L1", "L2", "L3", "L4"}


async def _classify_with_haiku(logger_name: str, message: str, traceback_text: str | None) -> dict | None:
    """Classify unknown error using Haiku. Returns dict or None on failure.

    Budget: ≤5 calls per cycle, ≤50 tokens/call. Timeout: 8s.
    """
    import os
    import json
    import aiohttp
    from clients.claude import resolve_proxy_endpoint

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    base_url, headers = resolve_proxy_endpoint(api_key)

    traceback_tail = ""
    if traceback_text:
        lines = traceback_text.strip().splitlines()
        traceback_tail = "\n".join(lines[-3:])

    from config import CLAUDE_MODEL_HAIKU
    prompt = _HAIKU_CLASSIFY_PROMPT.format(
        logger_name=logger_name,
        message=(message or "")[:200],
        traceback_tail=traceback_tail[:300],
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                base_url,
                headers=headers,
                json={
                    "model": CLAUDE_MODEL_HAIKU,
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                text = data["content"][0]["text"]
                text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                result = json.loads(text)
                # Validate
                cat = result.get("category", "")
                sev = result.get("severity", "")
                if cat not in _VALID_CATEGORIES or sev not in _VALID_SEVERITIES:
                    return None
                return {
                    "category": cat,
                    "severity": sev,
                    "action": (result.get("action") or "")[:200],
                }
    except Exception:
        return None


async def classify_unprocessed(limit: int = 100) -> int:
    """Classify errors that haven't been classified yet.

    Called from scheduler every 5 min.
    Phase 1: regex patterns (instant).
    Phase 2: Haiku fallback for unknowns (≤5 calls, async).
    Returns number of classified errors.
    """
    async with (await get_health_pool()).acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, logger_name, message, traceback
            FROM public.error_logs
            WHERE category IS NULL
            ORDER BY last_seen_at DESC
            LIMIT $1
        """, limit)

    if not rows:
        return 0

    updates = []
    unknown_rows = []

    # Phase 1: regex classification
    for row in rows:
        result = classify_error(
            row['logger_name'], row['message'], row.get('traceback')
        )
        if result['category'] == 'unknown':
            unknown_rows.append(row)
        updates.append((row['id'], result['category'], result['severity'], result['action']))

    # Phase 2: Haiku fallback for unknowns (max 5 per cycle)
    haiku_upgraded = 0
    for row in unknown_rows[:5]:
        haiku_result = await _classify_with_haiku(
            row['logger_name'], row['message'], row.get('traceback')
        )
        if haiku_result:
            # Find and update the corresponding entry in updates
            for i, (uid, _, _, _) in enumerate(updates):
                if uid == row['id']:
                    updates[i] = (
                        row['id'],
                        haiku_result['category'],
                        haiku_result['severity'],
                        haiku_result['action'],
                    )
                    haiku_upgraded += 1
                    break

    async with (await get_health_pool()).acquire() as conn:
        await conn.executemany("""
            UPDATE public.error_logs
            SET category = $2, severity = $3, suggested_action = $4
            WHERE id = $1
        """, updates)

    classified = len(updates)
    if haiku_upgraded:
        logger.info(f"[Classifier] Classified {classified} errors ({haiku_upgraded} via Haiku)")
    else:
        logger.info(f"[Classifier] Classified {classified} errors")
    return classified


# ═══════════════════════════════════════════════════════════
# L4 ESCALATION
# ═══════════════════════════════════════════════════════════

async def _escalate_persistent_l1() -> int:
    """Upgrade persistent L1 errors to L2 for autofix pipeline.

    L1 с action "retry" + occurrence_count >= 3 + age > 1h = код-баг, не transient.
    Повышаем severity до L2 → попадёт в autofix pipeline.
    Returns number of escalated errors.
    """
    async with (await get_health_pool()).acquire() as conn:
        result = await conn.execute("""
            UPDATE public.error_logs
            SET severity = 'L2',
                suggested_action = 'PR: persistent L1 → auto-escalated to L2'
            WHERE severity = 'L1'
              AND occurrence_count >= 3
              AND first_seen_at < NOW() - INTERVAL '1 hour'
              AND last_seen_at > NOW() - INTERVAL '24 hours'
              AND escalated = FALSE
              AND suggested_action ILIKE '%retry%'
        """)
        count = int(result.split()[-1]) if result else 0
        if count > 0:
            logger.info(f"[Classifier] Escalated {count} persistent L1 → L2")
        return count


async def check_escalation() -> Optional[str]:
    """L4: find L3/L4 or high-occurrence unknown errors needing escalation.

    Returns HTML alert text for TG, None if nothing to escalate.
    Called from scheduler every 15 min.
    """
    # Phase 1: upgrade persistent L1 → L2 (код-баги, не transient)
    await _escalate_persistent_l1()

    async with (await get_health_pool()).acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, category, severity, logger_name, message,
                   occurrence_count, context, last_seen_at
            FROM public.error_logs
            WHERE escalated = FALSE
              AND last_seen_at > NOW() - INTERVAL '1 hour'
              AND (
                  severity IN ('L3', 'L4')
                  OR (category = 'unknown' AND occurrence_count >= 5)
              )
            ORDER BY
                CASE severity
                    WHEN 'L4' THEN 1 WHEN 'L3' THEN 2 ELSE 3
                END,
                occurrence_count DESC
            LIMIT 5
        """)

    if not rows:
        return None

    # Filter out suppressed (benign) errors before alerting
    rows = [r for r in rows if not is_suppressed(r['logger_name'], r['message'] or '')]
    if not rows:
        return None

    lines = [
        "\U0001f6a8 <b>ESCALATION</b> "
        f"({len(rows)} \u043e\u0448\u0438\u0431\u043e\u043a \u0442\u0440\u0435\u0431\u0443\u044e\u0442 \u0432\u043d\u0438\u043c\u0430\u043d\u0438\u044f)\n"
    ]

    for r in rows:
        sev = r['severity'] or '??'
        cat = html.escape(r['category'] or 'unknown')
        emoji = SEVERITY_EMOJI.get(r['severity'], "\u26aa")
        msg = html.escape((r['message'] or '')[:80])
        count = f" x{r['occurrence_count']}" if r['occurrence_count'] > 1 else ""
        lines.append(f"  {emoji} [{cat}/{sev}] {msg}{count}")

    lines.append(f"\n\U0001f449 /errors \u2014 \u043f\u043e\u043b\u043d\u044b\u0439 \u043e\u0442\u0447\u0451\u0442")

    # Mark as escalated
    ids = [r['id'] for r in rows]
    async with (await get_health_pool()).acquire() as conn:
        await conn.execute(
            "UPDATE public.error_logs SET escalated = TRUE WHERE id = ANY($1::int[])", ids
        )

    logger.warning(f"[Classifier] Escalated {len(ids)} errors (L3/L4/unknown)")
    return "\n".join(lines)
