"""
WP-522 Ф11 волна 2 — писатель `onboarding_completed.v1` для чек-листа участника.

Каноническая точка эмиссии факта «Х2 и Х3 оба закрыты» (WP-406 Ф18) — вызывается
из обоих симметричных мест, где это может произойти: `core/onboarder/x2.py`
(_finish_x2, когда Х3 закрылся раньше Х2) и `handlers/onboarding.py`
(on_x3_confirm, когда Х2 закрылся раньше Х3). До этой правки оба места звали
`log_event` независимо и не публиковали событие в event-gateway — читатель
факта С1 чек-листа (`_read_s1` в DS-my-strategy/scripts/lib/checklist_contract.py`)
читает `public.domain_event`, куда `log_event` не пишет.

Спецификация — пир-сессия с Kimi 2026-09-05-18-wp406-onboarding-completed-writer
(консенсус: единая функция вместо двух отдельных вызовов в двух call site —
меньше риска, что третий call site забудет про `post_event`). Образец —
`emit_concept_named` (engines/shared/mentor_concept_naming.py).
"""

import asyncio
import logging
from datetime import datetime, timezone

from helpers.dual_write import post_event, resolve_ory_id_from_chat

logger = logging.getLogger(__name__)

EVENT_TYPE = "onboarding_completed"
EVENT_SCHEMA_VERSION = "v1"
EVENT_SOURCE = "aist-bot"


def build_external_id(account_id: str, occurred_at: datetime) -> str:
    """Идемпотентность по дню — избыточный запас: `newly_marked`-guard на
    call site (`mark_x2_done`/`mark_x3_done`) гарантирует не более одного
    вызова на account_id за всё время жизни аккаунта, различать повторы в
    один день не от чего."""
    return f"onboarding-completed-{account_id}-{occurred_at.strftime('%Y%m%d')}"


async def emit_onboarding_completed(
    chat_id: int, entry_type: str, source: str, lang: str, closed_by: str,
) -> None:
    """Логирует локально (ЦД/engagement) и публикует в event-gateway (чек-лист).

    `closed_by` ("x2" | "x3") — какой из двух шагов закрылся вторым. Идёт
    только в `log_event` (локальная аналитика по пути прохождения) — в
    `post_event` не идёт: `_read_s1` не различает путь закрытия, платить за
    ещё одно поле в каноне события нечем (payload-конвенция этого модуля —
    поле есть, только если его кто-то реально фильтрует, см. `concept_id` у
    `concept_named`).

    Без Ory-аккаунта (T0, не привязан) факт не к кому привязать в
    event-gateway — пропуск с логом, не ошибка (тот же принцип, что
    `emit_concept_named`); `log_event` при этом всё равно пишется.
    """
    from db.queries.events import log_event

    await log_event(chat_id, EVENT_TYPE, {
        "entry_type": entry_type, "source": source, "lang": lang, "closed_by": closed_by,
    })

    account_id = await resolve_ory_id_from_chat(chat_id)
    if not account_id:
        logger.info(
            "onboarding_completed: chat_id=%s closed_by=%s but no account — gateway event skipped",
            chat_id, closed_by,
        )
        return

    occurred_at = datetime.now(timezone.utc)
    logger.info("onboarding_completed: emitting for account_id=%s (closed_by=%s)", account_id, closed_by)
    asyncio.create_task(post_event(
        source=EVENT_SOURCE,
        external_id=build_external_id(account_id, occurred_at),
        event_type=EVENT_TYPE,
        schema_version=EVENT_SCHEMA_VERSION,
        occurred_at=occurred_at,
        account_id=account_id,
        payload={},
    ))
