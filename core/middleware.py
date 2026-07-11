"""
Middleware для aiogram.

LoggingMiddleware — логирование входящих сообщений.
TracingMiddleware — request-scoped трейсинг с записью в Neon.
RateLimitMiddleware — per-user rate limiting (sliding window, in-memory).
UpdateDedupMiddleware — отбрасывает повторную доставку одного update_id (webhook retry).
"""

import asyncio
import collections
import logging
import time

from aiogram import BaseMiddleware
from aiogram.enums import ChatAction
from aiogram.types import Message, CallbackQuery, TelegramObject

from config.settings import (
    DEVELOPER_CHAT_ID,
    MAINTENANCE_MODE,
    ALLOWED_TESTERS,
    MAINTENANCE_REDIRECT_BOT,
)
from core.tracing import start_trace, finish_trace

logger = logging.getLogger(__name__)


class UpdateDedupMiddleware(BaseMiddleware):
    """Отбрасывает повторно доставленный update (webhook retry при медленном ответе).

    Bot работает в webhook-режиме (aiogram SimpleRequestHandler): апдейт
    обрабатывается синхронно внутри HTTP-запроса. Если обработка занимает
    долго (holodный старт после деплоя, несколько DB round-trip подряд —
    например core/onboarder/x2.py: get_status + get_context + 2×save +
    log_event), Telegram считает доставку неуспешной и повторяет ОДИН И ТОТ
    ЖЕ update. aiogram/SimpleRequestHandler не дедуплицирует update_id из
    коробки → повторная доставка запускает тот же handler заново → дубли
    сообщений (инцидент 2026-07-10: X2-приветствие повторялось 3-5 раз).

    In-memory TTL-set: bot — единственный процесс на update_id (single
    Railway instance), Redis не нужен. TTL 120с покрывает Telegram's
    retry window с запасом.
    """

    def __init__(self, ttl_seconds: int = 120, max_size: int = 2000):
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._seen: "collections.OrderedDict[int, float]" = collections.OrderedDict()

    def _is_duplicate(self, update_id: int) -> bool:
        now = time.monotonic()
        # Периодическая чистка устаревших записей (амортизированно, не на каждый вызов)
        while self._seen and next(iter(self._seen.values())) < now - self._ttl:
            self._seen.popitem(last=False)
        if update_id in self._seen:
            return True
        self._seen[update_id] = now
        if len(self._seen) > self._max_size:
            self._seen.popitem(last=False)
        return False

    async def __call__(self, handler, event: TelegramObject, data: dict):
        update_id = data.get("event_update", {})
        update_id = getattr(update_id, "update_id", None)
        if update_id is not None and self._is_duplicate(update_id):
            logger.warning("[UpdateDedup] Discarding duplicate update_id=%s (webhook retry)", update_id)
            return None
        return await handler(event, data)


class RateLimitMiddleware(BaseMiddleware):
    """Per-user rate limiting — sliding window, in-memory.

    Defaults: 20 messages per 60 seconds per user.
    Превышение: молча игнорируем (не отвечаем, не спамим).
    Администраторы (DEVELOPER_CHAT_ID) не ограничены.
    """

    def __init__(self, max_messages: int = 20, window_seconds: int = 60):
        self._max = max_messages
        self._window = window_seconds
        # user_id -> deque of timestamps
        self._windows: dict[int, collections.deque] = {}

    def _is_allowed(self, user_id: int) -> bool:
        now = time.monotonic()
        dq = self._windows.setdefault(user_id, collections.deque())
        # Убираем устаревшие записи
        while dq and now - dq[0] > self._window:
            dq.popleft()
        if len(dq) >= self._max:
            return False
        dq.append(now)
        return True

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user_id = None
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id

        if user_id and user_id != DEVELOPER_CHAT_ID and not self._is_allowed(user_id):
            logger.warning(f"[RateLimit] user_id={user_id} превысил лимит {self._max}/{self._window}s — запрос отброшен")
            return  # молча игнорируем

        return await handler(event, data)


class MaintenanceMiddleware(BaseMiddleware):
    """Блокирует всех пользователей кроме ALLOWED_TESTERS.

    Включается переменной MAINTENANCE_MODE=true.
    Пользователям показывается сообщение с редиректом на основного бота.
    """

    async def __call__(self, handler, event: TelegramObject, data: dict):
        if not MAINTENANCE_MODE:
            return await handler(event, data)

        # Определяем chat_id
        chat_id = None
        if isinstance(event, Message) and event.from_user:
            chat_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            chat_id = event.from_user.id

        # Разрешённые тестировщики проходят
        if chat_id and chat_id in ALLOWED_TESTERS:
            return await handler(event, data)

        # Остальные получают сообщение
        if isinstance(event, Message) and chat_id:
            await event.answer(
                f"🔧 Этот бот используется для тестирования.\n\n"
                f"Пожалуйста, используйте основного бота: {MAINTENANCE_REDIRECT_BOT}"
            )
        elif isinstance(event, CallbackQuery):
            await event.answer("🔧 Бот на тестировании", show_alert=True)

        return  # не пропускаем дальше


class LoggingMiddleware(BaseMiddleware):
    """Middleware для логирования всех входящих сообщений"""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        if isinstance(event, Message):
            # data['raw_state'] — aiogram кэширует FSM state до middleware; не нужен лишний get_state()
            current_state = data.get('raw_state')
            # Security: НЕ логируем текст сообщения (PII, пароли, личные данные)
            msg_type = "command" if (event.text and event.text.startswith("/")) else "text" if event.text else "media"
            logger.info(f"[MIDDLEWARE] Получено сообщение: chat_id={event.chat.id}, "
                       f"user_id={event.from_user.id if event.from_user else None}, "
                       f"type={msg_type}, "
                       f"state={current_state}")

            # Typing indicator — fire-and-forget, не блокируем обработку запроса
            async def _send_typing():
                try:
                    await event.bot.send_chat_action(chat_id=event.chat.id, action=ChatAction.TYPING)
                except Exception:
                    pass
            asyncio.create_task(_send_typing())

            # Fire-and-forget typing tracking (WP-327 Phase 3)
            if event.text and len(event.text) >= 20:
                try:
                    from helpers.typing_tracker import track_typing
                    asyncio.create_task(track_typing(event.chat.id, event.text, event.message_id))
                except Exception:
                    pass

            # Fire-and-forget: сохранить/обновить tg_username + снять bot_blocked
            if event.from_user:
                try:
                    from db.queries.users import clear_bot_blocked
                    asyncio.create_task(clear_bot_blocked(event.from_user.id))
                except Exception:
                    pass
                if event.from_user.username:
                    try:
                        from db.queries.users import update_tg_username
                        asyncio.create_task(update_tg_username(event.from_user.id, event.from_user.username))
                    except Exception:
                        pass

        elif isinstance(event, CallbackQuery) and event.message:
            # Typing для callbacks — fire-and-forget
            async def _send_typing_cb():
                try:
                    await event.bot.send_chat_action(
                        chat_id=event.message.chat.id, action=ChatAction.TYPING
                    )
                except Exception:
                    pass
            asyncio.create_task(_send_typing_cb())

        return await handler(event, data)


class ConsultationPassthroughMiddleware(BaseMiddleware):
    """Пробрасывает ?-вопросы в SM consultation, даже если активен FSM state.

    Проблема: aiogram FSM-хендлеры (ClubStates, UpdateStates, OnboardingStates)
    перехватывают ВСЕ сообщения пользователя, блокируя global events SM.
    Решение: middleware очищает FSM state для ?-сообщений ДО роутинга,
    поэтому ни один FSM-хендлер не сматчит → fallback → SM → consultation.
    """

    async def __call__(self, handler, event: TelegramObject, data: dict):
        if isinstance(event, Message) and event.text and event.text.strip().startswith("?"):
            from aiogram.fsm.context import FSMContext
            state: FSMContext = data.get('state')
            if state:
                current = await state.get_state()
                if current is not None:
                    logger.info(
                        f"[ConsultationPassthrough] Clearing FSM state '{current}' "
                        f"for ?-question from user {event.from_user.id}"
                    )
                    await state.clear()
                    # aiogram caches state in data['raw_state'] for StateFilter
                    # before outer middleware runs — must reset it too
                    data['raw_state'] = None

        return await handler(event, data)


class TracingMiddleware(BaseMiddleware):
    """Middleware для трейсинга: замер полного времени обработки запроса.

    Создаёт Trace для каждого Message/CallbackQuery,
    записывает в Neon по завершении обработки.
    """

    async def __call__(self, handler, event: TelegramObject, data: dict):
        # Определяем user_id и command
        user_id = 0
        command = "unknown"

        if isinstance(event, Message):
            user_id = event.from_user.id if event.from_user else 0
            text = event.text or ""
            if text.startswith("/"):
                command = text.split()[0][:50]
            else:
                command = "msg:text" if text else "msg:empty"
        elif isinstance(event, CallbackQuery):
            user_id = event.from_user.id if event.from_user else 0
            command = f"cb:{event.data[:40]}" if event.data else "cb:empty"
        else:
            # Для других типов событий — пропускаем трейсинг
            return await handler(event, data)

        # data['raw_state'] — aiogram кэширует FSM state до middleware; избегаем второго get_state()
        sm_state = data.get('raw_state') or "unknown"

        # Создаём trace (Neon DB)
        trace = start_trace(
            user_id=user_id,
            command=command,
            state=sm_state or "unknown",
        )

        # Langfuse trace (dual-write, graceful)
        from core.langfuse_client import langfuse_trace, langfuse_end_trace
        langfuse_trace(
            user_id=user_id,
            name=command,
            trace_id=trace.trace_id,
            metadata={"state": sm_state or "unknown"},
        )

        try:
            result = await handler(event, data)
            return result
        finally:
            try:
                await finish_trace(trace)
            except Exception as e:
                logger.warning(f"[TracingMiddleware] Failed to finish trace: {e}")
            langfuse_end_trace()
            # Session tracking (fire-and-forget, не блокирует запрос)
            if user_id:
                try:
                    from db.queries.sessions import get_or_create_session
                    asyncio.create_task(get_or_create_session(user_id, command))
                except Exception:
                    pass
                # DAU fix (WP-7): touch_last_active_date обновляет last_active_date
                # для ЛЮБОГО взаимодействия. record_active_day теперь проверяет
                # activity_log (не last_active_date) — конфликт устранён.
                try:
                    from db.queries.activity import touch_last_active_date
                    asyncio.create_task(touch_last_active_date(user_id))
                except Exception:
                    pass
