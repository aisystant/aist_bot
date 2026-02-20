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

import re
import logging
from typing import Optional

from db.connection import acquire

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

    # --- Claude API (§ 3.3) — before DB (claude timeout ≠ db timeout) ---
    {"category": "claude_api", "severity": "L1",
     "pattern": r"(?i)rate_limit|RateLimitError|status.?code.*429",
     "action": "Retry с backoff (auto)"},
    {"category": "claude_api", "severity": "L1",
     "pattern": r"(?i)overloaded|OverloadedError|status.?code.*529",
     "action": "Degrade: cached content"},
    {"category": "claude_api", "severity": "L1",
     "pattern": r"(?i)APITimeoutError|anthropic.*timeout|claude.*timeout",
     "action": "Retry 1x, затем fallback"},
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
    {"category": "scheduler", "severity": "L4",
     "pattern": r"(?i)scheduler.*stuck|scheduler.*not.*start|asyncio.*deadlock",
     "action": "Escalate: проверить Railway logs"},

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
    "core.tracing": "fsm",
}

SEVERITY_EMOJI = {"L4": "\U0001f534", "L3": "\U0001f7e0", "L2": "\U0001f7e1", "L1": "\U0001f7e2"}


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


async def classify_unprocessed(limit: int = 100) -> int:
    """Classify errors that haven't been classified yet.

    Called from scheduler every 5 min.
    Returns number of classified errors.
    """
    async with await acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, logger_name, message, traceback
            FROM error_logs
            WHERE category IS NULL
            ORDER BY last_seen_at DESC
            LIMIT $1
        """, limit)

    if not rows:
        return 0

    updates = []
    for row in rows:
        result = classify_error(
            row['logger_name'], row['message'], row.get('traceback')
        )
        updates.append((row['id'], result['category'], result['severity'], result['action']))

    async with await acquire() as conn:
        await conn.executemany("""
            UPDATE error_logs
            SET category = $2, severity = $3, suggested_action = $4
            WHERE id = $1
        """, updates)

    logger.info(f"[Classifier] Classified {len(updates)} errors")
    return len(updates)


# ═══════════════════════════════════════════════════════════
# L4 ESCALATION
# ═══════════════════════════════════════════════════════════

async def check_escalation() -> Optional[str]:
    """L4: find L3/L4 or high-occurrence unknown errors needing escalation.

    Returns HTML alert text for TG, None if nothing to escalate.
    Called from scheduler every 15 min.
    """
    async with await acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, category, severity, logger_name, message,
                   occurrence_count, context, last_seen_at
            FROM error_logs
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

    lines = [
        "\U0001f6a8 <b>ESCALATION</b> "
        f"({len(rows)} \u043e\u0448\u0438\u0431\u043e\u043a \u0442\u0440\u0435\u0431\u0443\u044e\u0442 \u0432\u043d\u0438\u043c\u0430\u043d\u0438\u044f)\n"
    ]

    for r in rows:
        sev = r['severity'] or '??'
        cat = r['category'] or 'unknown'
        emoji = SEVERITY_EMOJI.get(r['severity'], "\u26aa")
        msg = (r['message'] or '')[:80]
        count = f" x{r['occurrence_count']}" if r['occurrence_count'] > 1 else ""
        lines.append(f"  {emoji} [{cat}/{sev}] {msg}{count}")

    lines.append(f"\n\U0001f449 /errors \u2014 \u043f\u043e\u043b\u043d\u044b\u0439 \u043e\u0442\u0447\u0451\u0442")

    # Mark as escalated
    ids = [r['id'] for r in rows]
    async with await acquire() as conn:
        await conn.execute(
            "UPDATE error_logs SET escalated = TRUE WHERE id = ANY($1::int[])", ids
        )

    logger.warning(f"[Classifier] Escalated {len(ids)} errors (L3/L4/unknown)")
    return "\n".join(lines)
