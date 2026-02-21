"""
L3 Health Check: каскадные ошибки → Railway restart.

Pipeline (WP-45 Phase 4):
1. detect: 10+ unique error_key за 5 мин ИЛИ pool exhaustion
2. notify: TG-сообщение разработчику о рестарте
3. restart: Railway GraphQL API → deploymentRestart
4. cooldown: max 1 restart за 30 мин

Safety: feature flag, cooldown, TG уведомление, graceful без токена.
"""

import time
from typing import Optional

import aiohttp
from aiogram import Bot

from config import get_logger
from db.connection import acquire

logger = get_logger(__name__)

# Railway GraphQL endpoint
_RAILWAY_API = "https://backboard.railway.com/graphql/v2"

# Cooldown tracking (in-memory)
_last_restart_ts: float = 0.0

# Thresholds
L3_CASCADE_THRESHOLD = 10  # unique error_keys in 5 min
L3_COOLDOWN_MINUTES = 30


async def _count_cascade_errors(minutes: int = 5) -> int:
    """Count unique error_keys in last N minutes."""
    async with await acquire() as conn:
        row = await conn.fetchrow("""
            SELECT COUNT(DISTINCT error_key) AS unique_count
            FROM error_logs
            WHERE last_seen_at > NOW() - INTERVAL '1 minute' * $1
              AND error_key IS NOT NULL
        """, minutes)
    return row['unique_count'] if row else 0


async def _has_pool_exhaustion(minutes: int = 5) -> bool:
    """Check for DB pool/connection errors in last N minutes."""
    async with await acquire() as conn:
        row = await conn.fetchrow("""
            SELECT COUNT(*) AS cnt
            FROM error_logs
            WHERE last_seen_at > NOW() - INTERVAL '1 minute' * $1
              AND severity = 'L3'
              AND (error_key ILIKE '%pool%' OR error_key ILIKE '%connection%refused%'
                   OR error_key ILIKE '%too many connections%')
        """, minutes)
    return (row['cnt'] if row else 0) > 0


async def _get_latest_deployment_id(
    token: str, service_id: str, environment_id: str
) -> Optional[str]:
    """Query Railway API for latest deployment ID."""
    query = """
        query deployments($first: Int, $input: DeploymentListInput!) {
            deployments(first: $first, input: $input) {
                edges { node { id status } }
            }
        }
    """
    variables = {
        "first": 1,
        "input": {
            "serviceId": service_id,
            "environmentId": environment_id,
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            _RAILWAY_API,
            json={"query": query, "variables": variables},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                logger.error(f"[L3] Railway deployments query failed: {resp.status}")
                return None
            data = await resp.json()

    edges = (data.get("data") or {}).get("deployments", {}).get("edges", [])
    if not edges:
        logger.error("[L3] No deployments found")
        return None
    return edges[0]["node"]["id"]


async def _restart_deployment(token: str, deployment_id: str) -> bool:
    """Restart Railway deployment (reuses existing image, no rebuild)."""
    query = """
        mutation deploymentRestart($id: String!) {
            deploymentRestart(id: $id)
        }
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            _RAILWAY_API,
            json={"query": query, "variables": {"id": deployment_id}},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                logger.error(f"[L3] Railway restart failed: {resp.status}")
                return False
            data = await resp.json()

    if data.get("errors"):
        logger.error(f"[L3] Railway restart errors: {data['errors']}")
        return False
    logger.info(f"[L3] Deployment {deployment_id} restart triggered")
    return True


async def run_l3_health_check(bot: Bot, dev_chat_id: str) -> bool:
    """Main L3 health check cycle. Called every 15 min by scheduler.

    Returns True if restart was triggered, False otherwise.
    """
    global _last_restart_ts

    import os
    railway_token = os.getenv("RAILWAY_API_TOKEN")
    service_id = os.getenv("RAILWAY_SERVICE_ID")
    environment_id = os.getenv("RAILWAY_ENVIRONMENT_ID")
    enabled = os.getenv("L3_RESTART_ENABLED", "false").lower() == "true"

    if not enabled:
        return False
    if not railway_token or not service_id or not environment_id:
        return False

    # Cooldown check
    now = time.monotonic()
    if _last_restart_ts and (now - _last_restart_ts) < L3_COOLDOWN_MINUTES * 60:
        return False

    # Detect cascade
    unique_errors = await _count_cascade_errors(minutes=5)
    pool_issue = await _has_pool_exhaustion(minutes=5)

    if unique_errors < L3_CASCADE_THRESHOLD and not pool_issue:
        return False

    reason = []
    if unique_errors >= L3_CASCADE_THRESHOLD:
        reason.append(f"{unique_errors} unique errors in 5 min")
    if pool_issue:
        reason.append("pool/connection exhaustion")
    reason_str = " + ".join(reason)

    # Notify before restart
    try:
        await bot.send_message(
            int(dev_chat_id),
            f"\U0001f6a8 <b>L3 Auto-Restart</b>\n"
            f"Причина: {reason_str}\n"
            f"Перезапуск через Railway API...",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"[L3] TG notification failed: {e}")

    # Get deployment and restart
    deployment_id = await _get_latest_deployment_id(
        railway_token, service_id, environment_id
    )
    if not deployment_id:
        try:
            await bot.send_message(
                int(dev_chat_id),
                "\u274c <b>L3</b>: не удалось получить deployment ID",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return False

    success = await _restart_deployment(railway_token, deployment_id)
    _last_restart_ts = now

    # Notify result
    try:
        if success:
            await bot.send_message(
                int(dev_chat_id),
                "\u2705 <b>L3</b>: Railway restart triggered",
                parse_mode="HTML",
            )
        else:
            await bot.send_message(
                int(dev_chat_id),
                "\u274c <b>L3</b>: Railway restart failed (check logs)",
                parse_mode="HTML",
            )
    except Exception:
        pass

    return success
