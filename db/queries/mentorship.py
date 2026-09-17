"""
WP-578 — Рабочее место наставника: архив переписки, привязка чата к потоку.

БД: отдельный Neon-проект (не `aisystant`), схема + RLS — Ф1
(neon-migrations/sandbox/2026-09-16-wp578-f1-mentorship-storage.sql), правка
Ф2 (stream_chat, addressed_to_mentor, app.lookup_stream_chat) — тот же файл.
Пул — MENTORSHIP_URL (config/settings.py), роль подключения — mentorship_app
(LOGIN NOBYPASSRLS). Каждый запрос к RLS-таблицам обязан идти внутри
_with_account_context — без SET LOCAL app.current_account_id база отвечает
исключением (fail-closed, app.require_account_id()).

Разбор решения: DS-my-strategy/inbox/WP-578/DRR-f2-telegram-bridge.md
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import asyncpg

from config import get_logger
from db.connection import get_mentorship_pool

logger = get_logger(__name__)


@dataclass(frozen=True)
class StreamChatContext:
    """Результат app.lookup_stream_chat — к какому потоку относится чат и чьим
    контекстом читателя пользоваться для последующих RLS-запросов."""

    stream_id: str
    reader_account_id: str


async def lookup_stream_chat(telegram_chat_id: int) -> Optional[StreamChatContext]:
    """Чат зарегистрирован за потоком? SECURITY DEFINER — работает без GUC
    (см. комментарий к app.lookup_stream_chat в файле миграции)."""
    pool = await get_mentorship_pool()
    if pool is None:
        return None
    row = await pool.fetchrow(
        "SELECT stream_id, reader_account_id FROM app.lookup_stream_chat($1)",
        telegram_chat_id,
    )
    if row is None or row["stream_id"] is None:
        return None
    return StreamChatContext(stream_id=row["stream_id"], reader_account_id=str(row["reader_account_id"]))


async def lookup_participant_stream(account_id: str) -> Optional[StreamChatContext]:
    """Поток участника по его account_id — резолв для личных сообщений (DM),
    которые НЕ проходят через stream_chat (у каждого участника свой
    уникальный чат с ботом, никто его не регистрирует командой). Возвращает
    None, если участник ещё ни разу не был классифицирован в своей группе
    потока — честное, задокументированное ограничение MVP (DRR-f2 §2)."""
    pool = await get_mentorship_pool()
    if pool is None:
        return None
    row = await pool.fetchrow(
        "SELECT stream_id, reader_account_id FROM app.lookup_participant_stream($1)",
        account_id,
    )
    if row is None or row["stream_id"] is None:
        return None
    return StreamChatContext(stream_id=row["stream_id"], reader_account_id=str(row["reader_account_id"]))


async def _with_account_context(pool: asyncpg.Pool, account_id: str, fn):
    """SET LOCAL app.current_account_id через set_config — SET не принимает
    bind-параметры напрямую, set_config($1, ..., true) им транзакционно
    эквивалентен и безопасен для asyncpg."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_account_id', $1, true)", account_id)
            return await fn(conn)


async def get_stream_reader_role(account_id: str, stream_id: str) -> Optional[str]:
    """Роль вызывающего в потоке ('mentor'|'pilot') или None, если не читатель.

    Не заменяет RLS (та защита реальна независимо), а даёт коду боту дружелюбное
    сообщение об ошибке ДО попытки записи вместо голого исключения БД.
    """
    pool = await get_mentorship_pool()
    if pool is None:
        return None

    async def _query(conn: asyncpg.Connection) -> Optional[str]:
        row = await conn.fetchrow(
            "SELECT role FROM public.stream_reader WHERE account_id = $1 AND stream_id = $2",
            account_id,
            stream_id,
        )
        return row["role"] if row else None

    return await _with_account_context(pool, account_id, _query)


async def register_stream_chat(telegram_chat_id: int, stream_id: str, registered_by_account_id: str) -> str:
    """Зарегистрировать телеграм-чат за потоком (команда /mentor_stream).

    Идемпотентно: тот же чат + тот же поток → "already_registered", без
    новой строки. Другой поток → старая активная строка помечается
    superseded_at, новая вставляется (аудируемо, не молчаливый UPDATE —
    DRR-f2 §3). Вызывающий должен быть читателем (mentor/pilot) целевого
    потока — иначе RLS WITH CHECK отклонит INSERT (fail-closed на уровне
    БД, не только на уровне этой функции).

    Returns: "registered" | "already_registered" | "not_stream_reader"
    """
    pool = await get_mentorship_pool()
    if pool is None:
        raise RuntimeError("MENTORSHIP_URL is not configured — mentorship module disabled")

    role = await get_stream_reader_role(registered_by_account_id, stream_id)
    if role is None:
        logger.warning(
            "[Mentorship] register_stream_chat отклонён: account=%s не читатель потока %s",
            registered_by_account_id,
            stream_id,
        )
        return "not_stream_reader"

    async def _do(conn: asyncpg.Connection) -> str:
        current = await conn.fetchrow(
            "SELECT stream_id FROM public.stream_chat WHERE telegram_chat_id = $1 AND superseded_at IS NULL",
            telegram_chat_id,
        )
        if current is not None and current["stream_id"] == stream_id:
            return "already_registered"
        if current is not None:
            await conn.execute(
                "UPDATE public.stream_chat SET superseded_at = now() WHERE telegram_chat_id = $1 AND superseded_at IS NULL",
                telegram_chat_id,
            )
        await conn.execute(
            "INSERT INTO public.stream_chat (telegram_chat_id, stream_id, registered_by) VALUES ($1, $2, $3)",
            telegram_chat_id,
            stream_id,
            registered_by_account_id,
        )
        return "registered"

    # Два одновременных /mentor_stream на один и тот же чат (два наставника
    # нажали Enter почти синхронно) могут оба пройти SELECT "нет активной
    # строки" до того, как любой из них успеет вставить — второй INSERT
    # тогда падает на частичном уникальном индексе stream_chat_active_uidx.
    # Один повтор после этого — SELECT внутри _do увидит уже закоммиченную
    # строку конкурента и корректно вернёт "already_registered" (тот же
    # поток) или проведёт supersede+insert (другой поток) — найдено холодным
    # ревью 17.09, изначальная версия пробрасывала UniqueViolationError
    # необработанной прямо в хендлер.
    try:
        result = await _with_account_context(pool, registered_by_account_id, _do)
    except asyncpg.exceptions.UniqueViolationError:
        logger.info(
            "[Mentorship] register_stream_chat: гонка на chat=%s, повтор после конфликта",
            telegram_chat_id,
        )
        result = await _with_account_context(pool, registered_by_account_id, _do)
    logger.info(
        "[Mentorship] register_stream_chat chat=%s stream=%s by=%s -> %s",
        telegram_chat_id,
        stream_id,
        registered_by_account_id,
        result,
    )
    return result


async def find_participant_id(reader_account_id: str, stream_id: str, account_id: str) -> Optional[int]:
    """participant_core.id, ЕСЛИ он уже существует — в отличие от
    get_or_create_participant НЕ создаёт новую строку. Для reply-to/forward-
    from целей: reply/forward на постороннего (третье лицо в группе, не
    участник и не читатель этого потока) не должен молча заводить его как
    участника потока — это утечка данных не по адресу (найдено холодным
    ревью 17.09)."""
    pool = await get_mentorship_pool()
    if pool is None:
        return None

    async def _do(conn: asyncpg.Connection) -> Optional[int]:
        row = await conn.fetchrow(
            "SELECT id FROM public.participant_core WHERE account_id = $1 AND stream_id = $2",
            account_id,
            stream_id,
        )
        return row["id"] if row else None

    return await _with_account_context(pool, reader_account_id, _do)


async def get_or_create_participant(reader_account_id: str, stream_id: str, participant_account_id: str) -> int:
    """participant_core.id для участника потока, создаёт запись при первом появлении.

    Выполняется под RLS-контекстом reader_account_id (любой читатель потока
    — участник сам не читатель этой базы, у него нет собственного контекста).
    """
    pool = await get_mentorship_pool()
    if pool is None:
        raise RuntimeError("MENTORSHIP_URL is not configured — mentorship module disabled")

    async def _do(conn: asyncpg.Connection) -> int:
        row = await conn.fetchrow(
            "SELECT id FROM public.participant_core WHERE account_id = $1 AND stream_id = $2",
            participant_account_id,
            stream_id,
        )
        if row is not None:
            return row["id"]
        row = await conn.fetchrow(
            "INSERT INTO public.participant_core (account_id, stream_id) VALUES ($1, $2) RETURNING id",
            participant_account_id,
            stream_id,
        )
        return row["id"]

    return await _with_account_context(pool, reader_account_id, _do)


async def write_archive_entry(
    *,
    reader_account_id: str,
    participant_id: int,
    channel: str,
    author: str,
    text: Optional[str],
    status: str,
    telegram_chat_id: int,
    telegram_message_id: int,
    consent_at_write: bool,
    message_at: datetime,
    addressed_to_mentor: bool,
    reply_to_participant_id: Optional[int] = None,
    forward_from_participant_id: Optional[int] = None,
    sent_at: Optional[datetime] = None,
) -> None:
    """Записать (или обновить при повторной/отредактированной доставке) одну
    строку архива переписки. UPSERT по UNIQUE(telegram_chat_id,
    telegram_message_id) — тот же вызов покрывает и первичную запись
    (`message`), и правку (`edited_message`, Р10: повторная проверка
    согласия на конфликте), и безопасный повтор при сетевой ошибке между
    попыткой и подтверждением (Р1: идемпотентность по UNIQUE делает повтор
    безопасным). На конфликте обновляются text/consent_at_write/
    addressed_to_mentor — все три пересчитываются заново в _process_one на
    каждый вызов (правка сообщения может добавить упоминание наставника,
    поэтому addressed_to_mentor тоже обязан обновляться, не только текст —
    найдено холодным ревью 17.09, изначальная версия эту колонку теряла)."""
    pool = await get_mentorship_pool()
    if pool is None:
        raise RuntimeError("MENTORSHIP_URL is not configured — mentorship module disabled")

    async def _do(conn: asyncpg.Connection) -> None:
        await conn.execute(
            """
            INSERT INTO public.correspondence_archive (
                participant_id, channel, author, text, status,
                telegram_chat_id, telegram_message_id, reply_to_participant_id,
                forward_from_participant_id, consent_at_write, message_at,
                sent_at, addressed_to_mentor
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            ON CONFLICT (telegram_chat_id, telegram_message_id) DO UPDATE SET
                text = EXCLUDED.text,
                consent_at_write = EXCLUDED.consent_at_write,
                addressed_to_mentor = EXCLUDED.addressed_to_mentor
            """,
            participant_id,
            channel,
            author,
            text,
            status,
            telegram_chat_id,
            telegram_message_id,
            reply_to_participant_id,
            forward_from_participant_id,
            consent_at_write,
            message_at,
            sent_at,
            addressed_to_mentor,
        )

    await _with_account_context(pool, reader_account_id, _do)
