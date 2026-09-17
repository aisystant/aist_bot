"""
WP-578 Ф2 — наблюдатель архива переписки наставник↔участник.

ArchiveTapMiddleware — единственная точка, которую видит горячий путь бота:
кладёт лёгкий снимок сообщения в ограниченную очередь и сразу отдаёт
управление дальше (Р1, DRR-f2 §1). Никакого обращения к БД в __call__.
mentorship_archive_worker() — фоновая задача, дренирует очередь, делает все
DB-проверки (регистрация чата/участника, роль автора, согласие) и решает,
писать ли строку вообще: молчаливо пропускает всё, что не относится ни к
одному зарегистрированному потоку.

Регистрация (bot.py): dp.message.middleware(ArchiveTapMiddleware()) и
dp.edited_message.middleware(ArchiveTapEditMiddleware()) сразу после
UpdateDedupMiddleware, ДО RateLimitMiddleware — чтобы дроп по частоте
сообщений не терял переписку, которую наставник обязан видеть.

Разбор решения: DS-my-strategy/inbox/WP-578/DRR-f2-telegram-bridge.md
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Union

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from engines.mentorship.classify import MentionSignals, compute_addressed_to_mentor, is_bot_command
from helpers.dual_write import resolve_ory_id_from_chat

logger = logging.getLogger(__name__)

_QUEUE_MAXSIZE = 1000
_MAX_WRITE_ATTEMPTS = 4  # первая попытка + 3 повтора — по одному на каждое значение _BACKOFF_SECONDS
_BACKOFF_SECONDS = (0.5, 2.0, 5.0)

_dropped_counters: dict[str, int] = {"queue_full": 0, "write_failed": 0}

# (chat_id, message_thread_id) -> создатель темы наставник?. In-memory,
# заполняется воркером из forum_topic_created-событий (см.
# _process_topic_created). Не переживает рестарт бота (кэш пуст) — осознанно
# узкое смягчение: теряется только уточнение "reply внутри темы наставника"
# (classify.MentionSignals.topic_creator_is_mentor), не сама переписка —
# сообщение архивируется в любом случае, просто как фоновое, пока тема не
# встретится воркеру заново.
_topic_creator_is_mentor_cache: dict[tuple[int, int], bool] = {}


@dataclass(frozen=True)
class RawMessageEvent:
    """Лёгкий снимок сообщения переписки — обычные типы Python, не объекты
    aiogram: не держим сетевые ресурсы через границу очереди, легко
    тестировать worker без aiogram-моков."""

    telegram_chat_id: int
    chat_type: str  # "group" | "supergroup" | "private"
    telegram_message_id: int
    telegram_user_id: int
    text: str
    message_at: datetime
    is_edit: bool
    reply_to_user_id: Optional[int]
    forward_from_user_id: Optional[int]
    message_thread_id: Optional[int]
    is_reply: bool
    mentioned_user_ids: tuple[int, ...]


@dataclass(frozen=True)
class TopicCreatedEvent:
    """Служебное forum_topic_created — не переписка, используется только
    чтобы заполнить _topic_creator_is_mentor_cache."""

    telegram_chat_id: int
    message_thread_id: int
    creator_telegram_user_id: int


QueueItem = Union[RawMessageEvent, TopicCreatedEvent]


def get_archive_queue() -> asyncio.Queue:
    """Модульный синглтон — та же очередь для middleware и worker'а."""
    global _archive_queue
    try:
        return _archive_queue
    except NameError:
        _archive_queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        return _archive_queue


def queue_stats() -> dict:
    """Для /ready (Р1: глубина очереди отдаётся туда) и логов."""
    q = get_archive_queue()
    return {
        "queue_depth": q.qsize(),
        "queue_maxsize": _QUEUE_MAXSIZE,
        "dropped_queue_full": _dropped_counters["queue_full"],
        "dropped_write_failed": _dropped_counters["write_failed"],
    }


def _snapshot(message: Message, *, is_edit: bool) -> Optional[RawMessageEvent]:
    if message.from_user is None or not message.text:
        return None
    reply_to_user_id = None
    is_reply = message.reply_to_message is not None
    if message.reply_to_message is not None and message.reply_to_message.from_user is not None:
        reply_to_user_id = message.reply_to_message.from_user.id
    forward_from_user_id = None
    origin = getattr(message, "forward_origin", None)
    origin_user = getattr(origin, "sender_user", None) if origin is not None else None
    if origin_user is not None:
        forward_from_user_id = origin_user.id
    mentioned_ids = tuple(
        entity.user.id
        for entity in (message.entities or [])
        if entity.type == "text_mention" and entity.user is not None
    )
    message_at = message.date if message.date.tzinfo is not None else message.date.replace(tzinfo=timezone.utc)
    return RawMessageEvent(
        telegram_chat_id=message.chat.id,
        chat_type=message.chat.type,
        telegram_message_id=message.message_id,
        telegram_user_id=message.from_user.id,
        text=message.text,
        message_at=message_at,
        is_edit=is_edit,
        reply_to_user_id=reply_to_user_id,
        forward_from_user_id=forward_from_user_id,
        message_thread_id=message.message_thread_id,
        is_reply=is_reply,
        mentioned_user_ids=mentioned_ids,
    )


class _QueueWriterMixin:
    """Общий безопасный put_nowait + счётчик дропа, и общий __call__ (P2:
    было продублировано между двумя middleware ниже, вынесено сюда).

    __call__ ловит ЛЮБОЕ исключение из _maybe_enqueue — наблюдатель не имеет
    права уронить основной путь бота ни при каких обстоятельствах (найдено
    холодным ревью 17.09: неперехваченное исключение здесь сносило бы
    обработку сообщения ЛЮБОГО пользователя, не только участников WP-578, —
    тот же класс отказа, что 14-часовой инцидент из CLAUDE.md §10.37, только
    через доступ к атрибуту, а не через ImportError)."""

    def __init__(self, queue: Optional[asyncio.Queue] = None):
        self._queue = queue if queue is not None else get_archive_queue()

    async def __call__(self, handler, event: TelegramObject, data: dict):
        try:
            self._maybe_enqueue(event)
        except Exception:  # noqa: BLE001 — наблюдатель не должен ронять основной путь бота
            logger.exception("[MentorshipArchive] сбой в _maybe_enqueue, сообщение не архивировано")
        return await handler(event, data)

    def _put(self, item: QueueItem) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            _dropped_counters["queue_full"] += 1
            logger.error(
                "[MentorshipArchive] очередь переполнена (%d), потеряно chat_id=%s",
                _QUEUE_MAXSIZE,
                getattr(item, "telegram_chat_id", "?"),
            )


class ArchiveTapMiddleware(_QueueWriterMixin, BaseMiddleware):
    """Наблюдатель: enqueue-only, никогда не блокирует и не отменяет обработку."""

    def _maybe_enqueue(self, event: TelegramObject) -> None:
        if not isinstance(event, Message):
            return
        if event.chat.type not in ("group", "supergroup", "private"):
            return
        if getattr(event, "forum_topic_created", None) and event.from_user is not None:
            self._put(
                TopicCreatedEvent(
                    telegram_chat_id=event.chat.id,
                    message_thread_id=event.message_id,
                    creator_telegram_user_id=event.from_user.id,
                )
            )
        if is_bot_command(event.text):
            return
        snapshot = _snapshot(event, is_edit=False)
        if snapshot is not None:
            self._put(snapshot)


class ArchiveTapEditMiddleware(_QueueWriterMixin, BaseMiddleware):
    """Тот же наблюдатель для dp.edited_message — снимок с is_edit=True."""

    def _maybe_enqueue(self, event: TelegramObject) -> None:
        if not isinstance(event, Message):
            return
        if event.chat.type not in ("group", "supergroup", "private"):
            return
        if is_bot_command(event.text):
            return
        snapshot = _snapshot(event, is_edit=True)
        if snapshot is not None:
            self._put(snapshot)


async def _telegram_user_is_mentor_of_stream(telegram_user_id: Optional[int], stream_id: str) -> bool:
    """Общий резолв "этот telegram-пользователь — читатель (mentor/pilot)
    потока?" — используется для reply-цели, упоминаний и создателя темы
    (P2: было бы 3x-повторением одного и того же запроса)."""
    if telegram_user_id is None:
        return False
    account_id = await resolve_ory_id_from_chat(telegram_user_id)
    if account_id is None:
        return False
    from db.queries.mentorship import get_stream_reader_role

    return await get_stream_reader_role(account_id, stream_id) is not None


async def _resolve_related_participant(
    telegram_user_id: Optional[int],
    *,
    reader_account_id: str,
    stream_id: str,
    exclude_account_id: str,
) -> Optional[int]:
    """participant_id для reply_to/forward_from цели — None, если цели нет,
    аккаунт не резолвится, это тот же человек, что и сам participant_id
    строки (нет смысла ссылаться самому на себя), либо цель ещё НЕ известна
    этому потоку. Намеренно ИЩЕТ, не создаёт: reply/forward на постороннего
    (третье лицо в группе, не участник и не читатель этого потока) не должен
    молча заводить его как участника — утечка данных не по адресу (найдено
    холодным ревью 17.09, была через get_or_create_participant)."""
    if telegram_user_id is None:
        return None
    from db.queries.mentorship import find_participant_id

    account_id = await resolve_ory_id_from_chat(telegram_user_id)
    if account_id is None or account_id == exclude_account_id:
        return None
    return await find_participant_id(reader_account_id, stream_id, account_id)


async def _process_topic_created(event: TopicCreatedEvent) -> None:
    from db.queries.mentorship import lookup_stream_chat

    ctx = await lookup_stream_chat(event.telegram_chat_id)
    if ctx is None:
        return  # тема в чате, ещё не зарегистрированном за потоком — нечего кэшировать
    is_mentor = await _telegram_user_is_mentor_of_stream(event.creator_telegram_user_id, ctx.stream_id)
    _topic_creator_is_mentor_cache[(event.telegram_chat_id, event.message_thread_id)] = is_mentor


async def _process_one(event: RawMessageEvent) -> None:
    """Полная классификация + запись одного снимка переписки. DB-и-только-DB
    — здесь, не в middleware."""
    from db.queries.consent import get_consent_grant
    from db.queries.mentorship import (
        get_or_create_participant,
        get_stream_reader_role,
        lookup_participant_stream,
        lookup_stream_chat,
        write_archive_entry,
    )

    account_id = await resolve_ory_id_from_chat(event.telegram_user_id)
    if account_id is None:
        return  # аккаунт не привязан — вне периметра (Р4: не пишем даже событие)

    # Личка резолвится через participant_core (участник должен быть уже
    # известен по своей группе), группа — через stream_chat. DRR-f2 §2.
    ctx = await lookup_participant_stream(account_id) if event.chat_type == "private" else await lookup_stream_chat(event.telegram_chat_id)
    if ctx is None:
        return  # вне периметра ни одного зарегистрированного потока

    author_is_mentor = await get_stream_reader_role(account_id, ctx.stream_id) is not None
    channel = "dm" if event.chat_type == "private" else "group"

    if author_is_mentor:
        # Наставник пишет об участнике — адресата берём из reply (честное
        # ограничение MVP: без reply некому положить ответ наставника).
        if event.reply_to_user_id is None:
            return
        participant_account_id = await resolve_ory_id_from_chat(event.reply_to_user_id)
        if participant_account_id is None:
            return
    else:
        participant_account_id = account_id

    consent_scope = "mentor_archive_dm" if channel == "dm" else "mentor_archive_group"
    consent_granted = await get_consent_grant(participant_account_id, consent_scope)

    reader_account_id = account_id if author_is_mentor else ctx.reader_account_id
    participant_id = await get_or_create_participant(reader_account_id, ctx.stream_id, participant_account_id)

    reply_to_participant_id = await _resolve_related_participant(
        event.reply_to_user_id,
        reader_account_id=reader_account_id,
        stream_id=ctx.stream_id,
        exclude_account_id=participant_account_id,
    )
    forward_from_participant_id = await _resolve_related_participant(
        event.forward_from_user_id,
        reader_account_id=reader_account_id,
        stream_id=ctx.stream_id,
        exclude_account_id=participant_account_id,
    )

    mentions_mentor = False
    for user_id in event.mentioned_user_ids:
        if await _telegram_user_is_mentor_of_stream(user_id, ctx.stream_id):
            mentions_mentor = True
            break

    topic_creator_is_mentor = None
    if event.message_thread_id is not None:
        topic_creator_is_mentor = _topic_creator_is_mentor_cache.get((event.telegram_chat_id, event.message_thread_id))

    signals = MentionSignals(
        author_is_mentor=author_is_mentor,
        channel_is_dm=(channel == "dm"),
        reply_to_is_mentor=False
        if author_is_mentor
        else await _telegram_user_is_mentor_of_stream(event.reply_to_user_id, ctx.stream_id),
        mentions_mentor=mentions_mentor,
        is_reply_in_topic=event.is_reply and event.message_thread_id is not None,
        topic_creator_is_mentor=topic_creator_is_mentor,
    )
    addressed = compute_addressed_to_mentor(signals)

    await _write_with_retry(
        write_archive_entry=write_archive_entry,
        reader_account_id=reader_account_id,
        participant_id=participant_id,
        channel=channel,
        author="mentor" if author_is_mentor else "participant",
        text=event.text if consent_granted else None,
        status="sent",
        telegram_chat_id=event.telegram_chat_id,
        telegram_message_id=event.telegram_message_id,
        consent_at_write=bool(consent_granted),
        message_at=event.message_at,
        addressed_to_mentor=addressed,
        reply_to_participant_id=reply_to_participant_id,
        forward_from_participant_id=forward_from_participant_id,
        sent_at=event.message_at,
    )


async def _write_with_retry(*, write_archive_entry, **kwargs) -> None:
    last_error: Optional[Exception] = None
    for attempt, delay in enumerate((0.0, *_BACKOFF_SECONDS), start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            await write_archive_entry(**kwargs)
            return
        except Exception as exc:  # noqa: BLE001 — воркер обязан пережить любую ошибку одной записи
            last_error = exc
            logger.warning(
                "[MentorshipArchive] попытка %d/%d записи не удалась chat=%s msg=%s: %s",
                attempt,
                _MAX_WRITE_ATTEMPTS,
                kwargs.get("telegram_chat_id"),
                kwargs.get("telegram_message_id"),
                exc,
            )
            if attempt >= _MAX_WRITE_ATTEMPTS:
                break
    _dropped_counters["write_failed"] += 1
    logger.error(
        "[MentorshipArchive] запись отброшена после %d попыток chat=%s msg=%s: %s",
        _MAX_WRITE_ATTEMPTS,
        kwargs.get("telegram_chat_id"),
        kwargs.get("telegram_message_id"),
        last_error,
    )


async def mentorship_archive_worker() -> None:
    """Фоновая задача: `asyncio.create_task(mentorship_archive_worker())` в bot.py.

    Каждый элемент очереди обрабатывается независимо — ошибка на одном не
    останавливает дренаж (см. try/except внутри цикла)."""
    queue = get_archive_queue()
    logger.info("[MentorshipArchive] worker started")
    while True:
        item = await queue.get()
        try:
            depth = queue.qsize()
            if depth > _QUEUE_MAXSIZE // 2:
                logger.warning("[MentorshipArchive] очередь заполнена на %d/%d", depth, _QUEUE_MAXSIZE)
            if isinstance(item, TopicCreatedEvent):
                await _process_topic_created(item)
            else:
                await _process_one(item)
        except Exception:  # noqa: BLE001 — воркер не должен падать целиком из-за одного элемента
            logger.exception("[MentorshipArchive] необработанная ошибка обработки элемента очереди: %r", item)
        finally:
            queue.task_done()
