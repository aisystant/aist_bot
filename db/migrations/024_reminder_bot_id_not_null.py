"""
Миграция 024: learning.reminder.bot_id → NOT NULL (WP-212 Layer 1).

Контекст (peer-сессия 2026-06-05-33-reminder-botid-backfill, Claude + Kimi):
- WP-320 добавил колонку bot_id рантайм-костылём (ADD COLUMN IF NOT EXISTS) в трёх
  местах кода. Layer 2 (commit 4a17bd2) заставил код писать/фильтровать по bot_id.
  Эта миграция закрывает Layer 1: гарантирует колонку и включает NOT NULL на уровне БД.

Почему это миграция бота, а не отдельный скрипт: миграция едет ВМЕСТЕ с кодом, который
пишет bot_id, и запускается при старте ДО планировщика → ограничение включается строго
после того, как инстанс уже пишет bot_id. Порядок гарантирован самим механизмом.

Стратегия для существующих NULL-строк (Вариант A):
  - backfill bot_id = own_bot_id для истории (sent=TRUE) и будущих (scheduled_for > utcnow)
    → сохраняет живые будущие напоминания этого инстанса;
  - DELETE оставшихся NULL (просроченные неотправленные — стале заблокировавших бота);
  - ALTER ... SET NOT NULL.
Идемпотентно: на повторном старте backfill/delete = no-op, SET NOT NULL — no-op.

На проде (Neon) backfill истории/будущих уже выполнен вручную в peer-сессии — здесь
останется удалить просроченные NULL и поставить ограничение.

Допущение «одна БД — один бот»: `DELETE ... WHERE bot_id IS NULL` удаляет NULL-строки
без оглядки на инстанс. Это безопасно, т.к. @aist_me_bot (Neon) и @aist_pilot_bot
(Railway Postgres) пишут в РАЗНЫЕ базы (см. деплой-таблицу в MEMORY.md). Если когда-нибудь
два бота с разными токенами разделят одну БД — DELETE инстанса B может снести NULL-строки,
которые инстанс A ещё не успел backfill'ить; тогда стратегию пересмотреть.

Запуск вручную:
    python -m db.migrations.024_reminder_bot_id_not_null
"""

import asyncio
import os
import sys

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import LEARNING_URL


def _own_bot_id() -> int:
    """Числовая часть токена = публичный id бота. 0 если токен недоступен."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    head = token.split(":", 1)[0]
    return int(head) if head.isdigit() else 0


async def _ensure(conn) -> dict:
    """Идемпотентно: колонка → backfill own_bot_id → delete NULL → NOT NULL."""
    # 1. Колонка существует (заменяет рантайм-костыли ADD COLUMN IF NOT EXISTS bot_id).
    await conn.execute("ALTER TABLE public.reminder ADD COLUMN IF NOT EXISTS bot_id BIGINT")

    pbid = _own_bot_id()
    if pbid <= 0:
        # Без id не можем безопасно backfill'ить — НЕ ставим NOT NULL, чтобы не уронить INSERT.
        return {"skipped": True, "reason": "no_bot_id", "backfilled": 0, "deleted": 0}

    # 2. Backfill: история + будущие → own_bot_id (просроченные неотправленные не трогаем).
    #    scheduled_for хранится как naive UTC → сравниваем с naive UTC now.
    backfilled = await conn.execute(
        """UPDATE public.reminder
           SET bot_id = $1
           WHERE bot_id IS NULL
             AND (sent = TRUE OR scheduled_for > (now() AT TIME ZONE 'UTC'))""",
        pbid,
    )
    # 3. Удаляем оставшиеся NULL (просроченные неотправленные — стале).
    deleted = await conn.execute("DELETE FROM public.reminder WHERE bot_id IS NULL")
    # 4. Включаем ограничение (идемпотентно: при отсутствии NULL — no-op).
    await conn.execute("ALTER TABLE public.reminder ALTER COLUMN bot_id SET NOT NULL")

    def _n(tag: str) -> int:
        # asyncpg возвращает 'UPDATE N' / 'DELETE N'
        parts = tag.split()
        return int(parts[-1]) if parts and parts[-1].isdigit() else 0

    return {
        "skipped": False,
        "bot_id": pbid,
        "backfilled": _n(backfilled),
        "deleted": _n(deleted),
    }


async def migrate():
    conn = await asyncpg.connect(LEARNING_URL, statement_cache_size=0)
    try:
        async with conn.transaction():
            diag = await _ensure(conn)
        if diag["skipped"]:
            print(f"  reminder.bot_id NOT NULL ПРОПУЩЕНА: {diag['reason']} "
                  f"(колонка гарантирована, ограничение не включено)")
        else:
            print(f"  reminder.bot_id ensure OK; bot_id={diag['bot_id']} "
                  f"backfilled={diag['backfilled']} deleted={diag['deleted']}")
        print("Миграция 024 завершена")
        return diag
    finally:
        await conn.close()


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    """Всегда выполняет идемпотентную ensure-схему. Возвращает False (нет «создания
    таблицы» — это constraint-миграция, не DDL новой таблицы)."""
    await migrate()
    return False


if __name__ == "__main__":
    asyncio.run(migrate())
