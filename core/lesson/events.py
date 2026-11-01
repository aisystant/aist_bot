"""
WP-522 Ф18 -- писатель `lesson_closed.v1` для факта С3 чек-листа участника
("первое занятие проведено", inbox/WP-522/WP-522.md).

Канонический вызов -- из `github_workbook_webhook_handler` (oauth_server.py),
когда push содержит один или несколько `lesson/YYYY-MM-DD.md`. `is_first`
определяется ОДНИМ запросом на весь батч файлов одного webhook'а (не по
каждому файлу отдельно) -- иначе внутри одного push с несколькими lesson-
файлами (bulk-import) фоновая задача первого файла ещё не успевает вставить
строку в event-gateway к моменту проверки второго файла, и оба получат
`is_first=true` (найдено критикой Kimi, пир-сессия
2026-09-16-13-wp522-f18-lesson-closed-first, ход 3).

Межзапросная гонка (два разных webhook-вызова одного аккаунта почти
одновременно, до того как фоновая задача первого успеет вставить строку)
сознательно не устраняется: единственный читатель факта С3
(`_read_s3`/`_read_scalar_fact` в `DS-my-strategy/scripts/lib/checklist_contract.py`)
использует `SELECT 1 ... LIMIT 1` и не различает одну или несколько строк
`is_first=true` для аккаунта -- строгая кардинальность "ровно один first"
никем не потребляется, а partial unique index/advisory lock защищали бы
инвариант без наблюдаемого эффекта (консенсус с Kimi, тот же ход). Пересмотреть,
если появится потребитель, которому нужна именно кардинальность, не факт
существования.

Образец -- `core/onboarder/events.py::emit_onboarding_completed`.
"""

import asyncio
import logging
from datetime import datetime, timezone

from db.connection import get_learning_pool
from helpers.dual_write import post_event

logger = logging.getLogger(__name__)

EVENT_TYPE = "lesson_closed"
EVENT_SCHEMA_VERSION = "v1"
EVENT_SOURCE = "aist-bot"


def build_external_id(account_id: str, lesson_date: str) -> str:
    """Идемпотентность по занятию, не по коммиту -- редактирование или
    повторный push того же `lesson/<lesson_date>.md` не порождает второе
    событие. Несколько lesson-файлов в одном коммите (bulk-import) естественно
    получают разные id."""
    return f"lesson-closed-{account_id}-{lesson_date}"


async def emit_lesson_closed_batch(account_id: str, lesson_dates: list[str]) -> None:
    """Публикует `lesson_closed` в event-gateway для каждой даты занятия из
    ОДНОГО webhook-вызова. Ровно первая (по возрастанию `lesson_date`) дата
    батча получает `is_first=true`, если для аккаунта ещё не было ни одного
    `lesson_closed` -- остальные (включая все прочие даты этого же батча)
    получают `is_first=false`.
    """
    if not lesson_dates:
        return

    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        already_has_lesson = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM public.domain_event"
            " WHERE account_id = $1 AND event_type = $2)",
            account_id, EVENT_TYPE,
        )

    first_date = None if already_has_lesson else min(lesson_dates)
    occurred_at = datetime.now(timezone.utc)
    logger.info(
        "lesson_closed: emitting %d date(s) for account_id=%s (first_date=%s)",
        len(lesson_dates), account_id, first_date,
    )
    for lesson_date in lesson_dates:
        asyncio.create_task(post_event(
            source=EVENT_SOURCE,
            external_id=build_external_id(account_id, lesson_date),
            event_type=EVENT_TYPE,
            schema_version=EVENT_SCHEMA_VERSION,
            occurred_at=occurred_at,
            account_id=account_id,
            payload={"is_first": lesson_date == first_date},
        ))
