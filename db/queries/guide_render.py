"""
Guide render trigger — WP-406 Ф-К.

# see WP-521 Ф7 (будущий получатель точки вызова — оркестратор конвейера руководств,
# ещё в разработке), sessions/2026-08/11/2026-08-11-21-wp406-tk-trigger-portnoy/report.md
# (контракт зафиксирован пир-сессией Claude+Codex, 11.08.2026)

Insert-only клиент очереди learning.guide_render_queue (владелец схемы — activity-hub,
migration 012_guide_render_queue.sql + WP-406 добавление trigger_type='onboarding_x3').
Потребитель — render-pilot-guides.py --queue-only (DS-autonomous-agents, systemd-timer
каждые 10 мин).
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from db.connection import get_learning_pool

logger = logging.getLogger(__name__)


async def trigger_first_guide(chat_id: int, source: str, trigger_event_id: str) -> Optional[int]:
    """WP-406 Ф-К: поставить в очередь рендер первого персонального руководства.

    Единая именованная точка вызова — при готовности WP-521 Ф7 меняется реализация
    внутри этой функции, вызывающий код (handlers/onboarding.py, core/onboarder/x2.py)
    не меняется.

    Fail-open: T0-пользователь (account_id ещё не привязан) или сбой INSERT не должны
    ронять закрытие онбординга — вызывающий код уже сохранил x2/x3-переход раньше.
    Возвращает queue_id или None (пропущено/ошибка — залогировано, не поднято).
    """
    try:
        from helpers.dual_write import resolve_ory_id_from_chat

        account_id = await resolve_ory_id_from_chat(chat_id)
        if not account_id:
            logger.info("[guide_render] skip chat_id=%s: no account_id yet (T0)", chat_id)
            return None

        payload = {"trigger_event_id": trigger_event_id, "source": source}
        pool = await get_learning_pool()
        async with pool.acquire() as conn:
            queue_id = await conn.fetchval(
                """INSERT INTO learning.guide_render_queue (account_id, trigger_type, trigger_payload)
                   VALUES ($1::uuid, 'onboarding_x3', $2::jsonb)
                   RETURNING queue_id""",
                account_id,
                json.dumps(payload),
            )
        logger.info(
            "[guide_render] enqueued queue_id=%s account=%s source=%s",
            queue_id, account_id[:8], source,
        )
        return queue_id
    except Exception as e:
        logger.error("[guide_render] enqueue failed for chat_id=%s: %s", chat_id, e)
        return None
