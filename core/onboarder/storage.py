"""
Хранилище состояния Онбордера (WP-406 Ф5, «Фундамент»).

# see DP.SC.170, DP.ROLE.067

Две оси хранения (консенсус peer-сессии 2026-06-11-07):
  - Отметки завершения характеристик: `development.user_state.x2_completed_at`
    и `x3_completed_at` (миграция 030). Индексируемы — нужны для проактивных
    напоминаний застрявшим и аналитики флота (Ф6). Ставятся один раз (idempotent
    через COALESCE), повторный вызов не сдвигает дату.
  - Черновик/last_gap диалога: `current_context['onboarding']` (TEXT-as-JSON,
    переживает redeploy, см. правило 10.36 CLAUDE.md). Транзиентные данные хода.

Слой реальный и проверяемый, но в «Фундаменте» ещё не вызывается из живого
роутинга — поведение бота не меняется.
"""

import logging

from db.connection import get_pool
from db.queries.users import get_intern, update_intern

logger = logging.getLogger(__name__)

_CONTEXT_KEY = "onboarding"


async def get_status(chat_id: int) -> dict:
    """Прочитать отметки завершения Х2/Х3 пользователя.

    Returns:
        {"x2_done": bool, "x3_done": bool,
         "x2_completed_at": datetime | None, "x3_completed_at": datetime | None}
        Для несуществующего пользователя — обе характеристики не закрыты.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT x2_completed_at, x3_completed_at
            FROM development.user_state
            WHERE chat_id = $1
            """,
            chat_id,
        )
    x2_at = row["x2_completed_at"] if row else None
    x3_at = row["x3_completed_at"] if row else None
    return {
        "x2_done": x2_at is not None,
        "x3_done": x3_at is not None,
        "x2_completed_at": x2_at,
        "x3_completed_at": x3_at,
    }


async def mark_x2_done(chat_id: int) -> None:
    """Поставить отметку «понял сообщество» (Х2). Idempotent: не сдвигает дату."""
    await _mark_done(chat_id, "x2_completed_at")


async def mark_x3_done(chat_id: int) -> None:
    """Поставить отметку «выбрал траекторию» (Х3). Idempotent: не сдвигает дату."""
    await _mark_done(chat_id, "x3_completed_at")


async def _mark_done(chat_id: int, column: str) -> None:
    # column — из фиксированного набора (mark_x2_done/mark_x3_done), не из ввода.
    pool = await get_pool()
    async with pool.acquire() as conn:
        status = await conn.execute(
            f"""
            UPDATE development.user_state
            SET {column} = COALESCE({column}, (NOW() AT TIME ZONE 'utc'))
            WHERE chat_id = $1
            """,
            chat_id,
        )
    # "UPDATE 0" = строки user_state ещё нет: отметка не поставлена. В живом flow
    # get_intern лениво создаёт строку, поэтому это сигнал ошибки вызова, не норма.
    if status == "UPDATE 0":
        logger.warning("onboarder mark skipped: no user_state row for chat_id=%s (%s)", chat_id, column)


async def get_onboarding_context(chat_id: int) -> dict:
    """Черновик/last_gap диалога Онбордера из current_context['onboarding']."""
    intern = await get_intern(chat_id)
    context = intern.get("current_context") or {}
    return context.get(_CONTEXT_KEY) or {}


async def save_onboarding_context(chat_id: int, patch: dict) -> dict:
    """Слить patch в current_context['onboarding'] и сохранить.

    Merge поверх существующего поднамеспейса (не затирает соседние ключи
    current_context). Возвращает обновлённый поднамеспейс.
    """
    intern = await get_intern(chat_id)
    context = intern.get("current_context") or {}
    onboarding = context.get(_CONTEXT_KEY) or {}
    onboarding.update(patch)
    context[_CONTEXT_KEY] = onboarding
    await update_intern(chat_id, current_context=context)
    return onboarding
