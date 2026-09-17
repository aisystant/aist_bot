from __future__ import annotations

"""
WP-253 Ф12.6 фаза A — dual-write профиля пользователя в persona.bot_profile.

Зеркалит 18 профильных полей public.users (главная база бота) в
persona.bot_profile (Neon) за флагом. Читает — фаза C (cutover), здесь не
происходит: главный источник истины остаётся public.users до отдельного
явного решения пилота.

bot_profile — ОДНА база на все окружения бота (pilot + prod используют один
PERSONA_URL, WP-253 Ф12.5 находка: коллизия 97 chat_id 17.09.2026). Поэтому:
  - на pilot флаг остаётся выключенным, пока нет отдельной Neon-ветки persona
    для пилотного окружения (открытый вопрос пилоту, см. карточку РП);
  - на prod включение — поэтапное: сперва BOT_PROFILE_DUAL_WRITE_CHAT_IDS
    (непустой allowlist), затем пустой allowlist (все).

Зеркалятся только T1+ пользователи (ory_id IS NOT NULL) — у T0 нет владельца
для RLS-политики self_only, зеркалирование их строки создало бы запись,
невидимую самому пользователю (peer-session 2026-09-17-07, консенсус с Kimi).
Тот же фильтр использует backfill-скрипт фазы B — сверка расхождений 0
остаётся согласованной без дополнительной синхронизации фильтров.

Гонка двух писателей одного chat_id внутри одного процесса бота (например,
update_intern и update_tg_username почти одновременно) закрыта per-chat_id
локом: порядок зеркал = порядку коммитов в public.users. Guard по updated_at
в SQL — второй, более грубый рубеж против записи из ДРУГОГО процесса
(refresh-скрипт фазы B, второй бот при нарушении изоляции pilot/prod).
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

# Тот же паттерн, что db/sql_helpers.py использует для валидации идентификаторов
# (не импортируем оттуда напрямую — там это приватная функция, не публичный API).
_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

# Порядок фиксирован — используется и в SQL, и в тестах.
PROFILE_MIRROR_FIELDS: tuple[str, ...] = (
    "name", "occupation", "role", "domain", "interests", "motivation", "goals",
    "language", "timezone", "experience_level", "difficulty_preference",
    "learning_style", "delivery_format", "detail_level", "study_duration",
    "current_problems", "desires", "tg_username",
)

# Поля public.users, которые НЕ зеркалятся: у каждого другой владелец записи
# (identity.py:link_ory/update_user_tier, aisystant.py, oauth_server.py/
# handlers/twin.py). Зеркалирование создало бы раздвоение источника истины
# (peer-session 2026-09-17-03, независимое ревью Fable-субагента).
_EXCLUDED_OWNER_ELSEWHERE = frozenset({
    "tier", "email", "aisystant_id", "aisystant_linked_at", "dt_connected_at", "dt_user_id",
})
assert not (set(PROFILE_MIRROR_FIELDS) & _EXCLUDED_OWNER_ELSEWHERE), (
    "PROFILE_MIRROR_FIELDS не должен пересекаться с полями другого владельца"
)

for _field in PROFILE_MIRROR_FIELDS:
    assert _IDENTIFIER_RE.match(_field), f"invalid SQL identifier: {_field!r}"

_INSERT_COLUMNS = f"chat_id, account_id, {', '.join(PROFILE_MIRROR_FIELDS)}, updated_at"
_INSERT_PARAMS = ", ".join(f"${i}" for i in range(1, 4 + len(PROFILE_MIRROR_FIELDS)))
_UPDATE_SET = ", ".join(f"{col} = EXCLUDED.{col}" for col in PROFILE_MIRROR_FIELDS)

# $1=chat_id $2=account_id $3..$3+len=поля $(N)=updated_at
MIRROR_SQL = f"""
INSERT INTO bot_profile ({_INSERT_COLUMNS})
VALUES ({_INSERT_PARAMS})
ON CONFLICT (chat_id) DO UPDATE SET
    {_UPDATE_SET},
    account_id = COALESCE(EXCLUDED.account_id, bot_profile.account_id),
    updated_at = EXCLUDED.updated_at
WHERE bot_profile.updated_at IS NULL OR EXCLUDED.updated_at >= bot_profile.updated_at
"""

_MIRROR_TIMEOUT_S = 5.0

# Счётчики наблюдаемости (читает dev_stats.py). Не персистентны — сбрасываются
# при рестарте процесса, это ожидаемо (индикатор текущего запуска, не журнал).
MIRROR_COUNTS: dict[str, int] = {
    "mirrored": 0,
    "stale_guard": 0,
    "skipped_flag": 0,
    "skipped_t0": 0,
    "fk_skipped": 0,
    "failed": 0,
}

# asyncio.Lock per chat_id — сериализует «UPDATE public.users + зеркало» внутри
# одного процесса, для ВСЕХ точек записи (update_intern, update_tg_username,
# identity.link_ory, T0→T1 ветка в oauth_server.py). Заводится лениво и
# НИКОГДА не удаляется из словаря — тот же паттерн, что уже используется в
# этом репо (handlers/external_session.py:_get_finalize_lock,
# clients/gateway_mcp.py:_refresh_single_token): удаление записи по
# `not lock.locked()` не является безопасным индикатором отсутствия
# ожидающих (снятие лока будит waiter асинхронно, не синхронно с release()) —
# конкретный 3-writer сценарий воспроизведён и ломает взаимоисключение
# (cold-review этой сессии). Telegram chat_id — конечное множество активных
# пользователей, рост словаря без чистки принят как компромисс.
_chat_locks: dict[int, asyncio.Lock] = {}
_chat_locks_guard = asyncio.Lock()


async def chat_lock(chat_id: int) -> asyncio.Lock:
    """Вернуть (создав при необходимости) лок для конкретного chat_id."""
    async with _chat_locks_guard:
        lock = _chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            _chat_locks[chat_id] = lock
        return lock


def mirror_enabled_for(chat_id: int) -> bool:
    """Флаг включён и (allowlist пуст ИЛИ chat_id в allowlist)."""
    from config import BOT_PROFILE_DUAL_WRITE_CHAT_IDS, BOT_PROFILE_DUAL_WRITE_ENABLED

    if not BOT_PROFILE_DUAL_WRITE_ENABLED:
        return False
    if not BOT_PROFILE_DUAL_WRITE_CHAT_IDS:
        return True
    return chat_id in BOT_PROFILE_DUAL_WRITE_CHAT_IDS


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """public.users.updated_at — naive TIMESTAMP по конвенции бота (UTC, см.
    CLAUDE.md §10.6). bot_profile.updated_at — TIMESTAMPTZ: naive datetime
    интерпретируется Postgres по session timezone, не по UTC — без явной
    пометки запись молча сдвинется на сессионный офсет (peer-session
    2026-09-17-07, находка Kimi). Помечаем явно перед передачей параметром.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def mirror_profile_row(row: dict) -> str:
    """Зеркалит одну строку public.users (после её UPDATE/INSERT) в
    persona.bot_profile. Ожидает в `row`: chat_id, ory_id, updated_at и все
    PROFILE_MIRROR_FIELDS (типично — результат RETURNING вызывающего запроса).

    Возвращает исход: mirrored | skipped_flag | skipped_t0 | fk_skipped | failed.
    Никогда не поднимает исключение — вызывающий код (главный путь записи в
    public.users) не должен падать из-за недоступности persona.
    """
    chat_id = row["chat_id"]

    if not mirror_enabled_for(chat_id):
        MIRROR_COUNTS["skipped_flag"] += 1
        return "skipped_flag"

    ory_id = row.get("ory_id")
    if ory_id is None:
        # T0: нет владельца для RLS self_only — не зеркалим (см. docstring модуля).
        MIRROR_COUNTS["skipped_t0"] += 1
        return "skipped_t0"

    params = [chat_id, ory_id] + [row.get(f) for f in PROFILE_MIRROR_FIELDS] + [_as_utc(row.get("updated_at"))]

    try:
        from db.connection import get_persona_pool

        pool = await asyncio.wait_for(get_persona_pool(), timeout=_MIRROR_TIMEOUT_S)
        async with pool.acquire() as conn:
            tag = await asyncio.wait_for(conn.execute(MIRROR_SQL, *params), timeout=_MIRROR_TIMEOUT_S)
        # Command tag "INSERT <oid> <rows>". rows=0 значит guard (WHERE в
        # ON CONFLICT DO UPDATE) отклонил запись как устаревшую — это не
        # ошибка, но и не «зеркалировано»: отдельный исход для наблюдаемости
        # (cold-review этой сессии — раньше любой success-return без
        # исключения безусловно считался "mirrored").
        affected = int(tag.rsplit(" ", 1)[-1])
        if affected == 0:
            MIRROR_COUNTS["stale_guard"] += 1
            return "stale_guard"
        MIRROR_COUNTS["mirrored"] += 1
        return "mirrored"
    except asyncpg.ForeignKeyViolationError:
        # ory_id из public.users ещё не спровижинен в persona.ory_identity
        # (тот же случай, что backfill фазы B помечает fk_violation).
        logger.warning("[bot_profile.mirror] FK violation for chat_id=%s — skipped, no retry", chat_id)
        MIRROR_COUNTS["fk_skipped"] += 1
        return "fk_skipped"
    except Exception as exc:
        # Намеренно широкий except (не только asyncpg.PostgresError/OSError/
        # asyncio.TimeoutError) — cold-review нашёл живой контрпример:
        # asyncpg.exceptions.ClientConfigurationError (пустой/некорректный
        # PERSONA_URL) не наследует PostgresError и пробивал этот вызов
        # наружу в update_intern/update_tg_username/link_ory, роняя основной
        # путь записи профиля — ровно то, что докстринг модуля обещает не
        # допускать. Без значений полей в логе — PII-инвариант.
        logger.warning(
            "[bot_profile.mirror] %s for chat_id=%s — main write not affected",
            type(exc).__name__, chat_id,
        )
        MIRROR_COUNTS["failed"] += 1
        return "failed"


def get_mirror_counts() -> dict:
    """Снимок счётчиков для /dev-статистики (db/queries/dev_stats.py)."""
    return dict(MIRROR_COUNTS)
