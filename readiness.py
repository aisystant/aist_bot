"""Bounded dependency readiness, separate from process liveness."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable


READINESS_TIMEOUT_SECONDS = 2.0
DatabaseProbe = Callable[[], Awaitable[bool]]
SchedulerProbe = Callable[[], str]
logger = logging.getLogger(__name__)


async def probe_primary_database() -> bool:
    """Return whether the primary database path can execute a trivial query."""
    from db.connection import get_pool

    pool = await get_pool()
    return await pool.fetchval("SELECT 1") == 1


def scheduler_readiness() -> str:
    """Report the configured scheduler state without exposing internals."""
    if os.getenv("DISABLE_SCHEDULER", "false").lower() == "true":
        return "disabled"

    from core.scheduler import _scheduler

    return "ready" if _scheduler is not None and _scheduler.running else "unavailable"


async def readiness_snapshot(
    *,
    database_probe: DatabaseProbe | None = None,
    scheduler_probe: SchedulerProbe | None = None,
    timeout_seconds: float = READINESS_TIMEOUT_SECONDS,
) -> tuple[dict[str, object], int]:
    """Return a safe readiness payload and HTTP status code."""
    run_database_probe = database_probe or probe_primary_database
    run_scheduler_probe = scheduler_probe or scheduler_readiness
    try:
        scheduler_state = run_scheduler_probe()
    except Exception as exc:
        logger.warning("Readiness scheduler probe failed (%s)", type(exc).__name__)
        scheduler_state = "unavailable"

    components = {"database": "unavailable", "scheduler": scheduler_state}
    try:
        database_ready = await asyncio.wait_for(
            run_database_probe(),
            timeout=timeout_seconds,
        )
        components["database"] = "ready" if database_ready else "unavailable"
    except asyncio.TimeoutError:
        components["database"] = "timeout"
    except Exception as exc:
        logger.warning("Readiness database probe failed (%s)", type(exc).__name__)

    scheduler_ready = scheduler_state in {"ready", "disabled"}
    ready = components["database"] == "ready" and scheduler_ready
    payload: dict[str, object] = {
        "status": "ready" if ready else "degraded",
        "components": components,
    }
    return payload, 200 if ready else 503
