"""
Fallback хендлеры — обработка неизвестных сообщений и callback-ов.

Если SM активна — делегирует в SM.
Иначе — показывает подсказку.
"""

import logging
import re

from aiogram import Router
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from db.queries import get_intern
from core.tier_detector import detect_ui_tier
from i18n import t, detect_language

logger = logging.getLogger(__name__)

fallback_router = Router(name="fallback")

_HERMES_PREFIXES_RE = re.compile(r"^(гермес|hermes)[,:\s]+", re.IGNORECASE)
_HERMES_UNAVAILABLE_TIER_MSG = "Функция недоступна на твоём тире"

# WP-392 Ф3.1b: session_id для hermes_chat (память диалога T4-full)
_HERMES_SESSION_MAP: dict[int, str] = {}


def _is_role_addressed_question(text: str) -> bool:
    """WP-498 Ф12 (05.09): «?»-вопрос, адресованный Наставнику/Диагносту/Навигатору.

    Та же лексика, что определяет роль внутри самой консультации
    (states.common.consultation._detect_role) — не дублируем список паттернов,
    только применяем его здесь ещё раз, чтобы решить это ДО T4-редиректа в
    Hermes (Ф3.1b), который иначе перехватывает сообщение раньше, чем машина
    состояний вообще увидит «?».
    """
    if not text.startswith('?'):
        return False
    from states.common.consultation import _detect_role
    return _detect_role(text[1:].strip()) is not None


def _is_main_router_callback(callback: CallbackQuery) -> bool:
    """Проверяет, что callback НЕ принадлежит engines/ или connect/ роутерам."""
    if not callback.data:
        return True
    excluded_prefixes = ('mode_', 'feed_', 'marathon_', 'iwe_')
    return not callback.data.startswith(excluded_prefixes)


@fallback_router.callback_query(_is_main_router_callback)
async def on_unknown_callback(callback: CallbackQuery, state: FSMContext):
    """Обработка callback-запросов — делегирование в State Machine.

    ВАЖНО: НЕ очищаем FSM state здесь — если callback попал в fallback
    из-за транзиентной ошибки DB при проверке state-фильтра,
    очистка state навсегда сломает пользователю текущий flow (онбординг и др.).
    """
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    chat_id = callback.message.chat.id

    # Проверяем, есть ли активный FSM state — если да, это транзиентный сбой,
    # НЕ надо перехватывать callback у FSM-хендлеров
    current_state = await state.get_state()
    if current_state is not None:
        logger.warning(
            f"[Fallback] Callback '{callback.data}' from user {callback.from_user.id} "
            f"reached fallback despite active FSM state '{current_state}'. "
            f"Likely transient DB error during state filter check. NOT clearing state."
        )
        await callback.answer(t('errors.try_again', 'ru'), show_alert=False)
        return

    if dispatcher and dispatcher.is_sm_active:
        try:
            intern = await get_intern(chat_id)
            if intern:
                handled = await dispatcher.route_callback(intern, callback)
                if handled:
                    return
        except Exception as e:
            logger.error(f"[SM] Error routing callback: {e}")
            import traceback
            logger.error(traceback.format_exc())

    # SM не обработала или не активна — показываем "кнопка устарела"
    logger.warning(f"Unhandled callback: {callback.data} from user {callback.from_user.id}")
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') if intern else 'ru'
    await callback.answer(t('fsm.button_expired', lang), show_alert=True)


@fallback_router.message()
async def on_unknown_message(message: Message, state: FSMContext):
    """Обработка сообщений — делегирование в State Machine."""
    # SC.118: Не обрабатывать сообщения из каналов и групп в fallback
    if message.chat.type in ('channel', 'group', 'supergroup'):
        return

    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    chat_id = message.chat.id
    text = message.text or ''

    # WP-501 (2026-07-26): reaching fallback means the FSM-based session path
    # (handlers/external_session.py:handle_session_text, StateFilter(ExternalSession.active))
    # did NOT claim this message — either no session is active for this chat, or the
    # session was created by the VS Code bridge script (vscode-session-handoff.py),
    # which never sets that FSM state. Check GitHub directly as a secondary path
    # before falling through to SM/onboarding routing below. Cost: one GitHub list +
    # per-file GET only on messages that would otherwise be unhandled (not on every
    # message), acceptable at current session volume (see WP-501 review finding).
    if text and not text.startswith('/'):
        from handlers.external_session import find_active_bridge_session_for_chat, _append_pilot_turn
        found = await find_active_bridge_session_for_chat(chat_id)
        if found:
            session_id, next_turn_n, token, repo, branch = found
            ok = await _append_pilot_turn(session_id, message.message_id, text, next_turn_n, token, repo, branch)
            if ok:
                logger.info(
                    "[fallback] appended pilot turn %d to bridge session %s (chat %s)",
                    next_turn_n, session_id, chat_id,
                )
                return
            logger.error(
                "[fallback] failed to append pilot turn to bridge session %s (chat %s) — falling through",
                session_id, chat_id,
            )

    if dispatcher and dispatcher.is_sm_active:
        intern = await get_intern(chat_id)
        tier_num = await detect_ui_tier(chat_id)

        # WP-392 Ф3.1b: T4-full — ВСЁ в Hermes (без префикса, без консультанта).
        # WP-498 Ф12 (05.09, решение пилота): кроме «?»-вопроса, адресованного
        # Наставнику/Диагносту/Навигатору — та роль всегда и для всех тиров, идёт
        # в SM (машина войдёт в консультацию и определит роль сама), не в Hermes.
        if tier_num >= 4 and text and not text.startswith('/') and not _is_role_addressed_question(text):
            # Не перехватывать у SM, ожидающей ответ (фиксация, марафон и др.)
            from handlers.external_session import _sm_is_expecting_reply
            from states.feed.digest import FeedDigestState
            if await _sm_is_expecting_reply(chat_id) or FeedDigestState.is_waiting_fixation(chat_id):
                logger.info("[fallback] T4-full skipped: SM or feed expecting reply for chat %s", chat_id)
                # fall through to SM dispatch below
            else:
                # Снять префикс «Гермес,» если есть — T4 не обязан его писать,
                # но если написал, Hermes не должен видеть служебный токен.
                hermes_text = _HERMES_PREFIXES_RE.sub("", text).strip() or text
                # Б2: сбросить FSM-стейт (напр. Settings) чтобы follow-up не попал в SM
                await state.clear()
                from clients.gateway_mcp import gateway_mcp
                from handlers.hermes import _send_unavailable
                if not gateway_mcp.is_connected(chat_id):
                    # Нет токена — не жжём 401-цикл, сразу зовём на повторный вход.
                    await _send_unavailable(message, None, chat_id)
                    return
                session_id = _HERMES_SESSION_MAP.get(chat_id)
                try:
                    from helpers.typing_indicator import keep_typing
                    async with keep_typing(message):
                        response = await gateway_mcp.hermes_chat(
                            message=hermes_text,
                            telegram_user_id=chat_id,
                            session_id=session_id,
                        )
                except Exception:
                    logger.exception("[fallback] hermes_chat (T4-full) failed for chat %s", chat_id)
                    response = None
                # session_id управляется на стороне gateway/hermes по telegram_user_id
                if response:
                    await message.answer(response)
                else:
                    await _send_unavailable(message, None, chat_id)
                return

        # WP-392 Ф3.1: явный вызов Hermes через префикс — ДО онбординг-интентов.
        # Fail-safe: если hermes_router пропустил (SkipHandler при marathon SM),
        # fallback перехватывает и маршрутизирует в Hermes напрямую.
        # Исключение: feed.digest ждёт фиксацию — передать в SM, не в Hermes.
        if text and _HERMES_PREFIXES_RE.search(text):
            from states.feed.digest import FeedDigestState
            if FeedDigestState.is_waiting_fixation(chat_id):
                logger.info("[fallback] hermes prefix skipped: feed waiting fixation for chat %s", chat_id)
                # fall through to SM dispatch below
            else:
                logger.info("[fallback] hermes prefix chat_id=%s tier_str=%r tier_num=%s", chat_id, (intern or {}).get("tier"), tier_num)
                if tier_num < 3:
                    await message.answer(_HERMES_UNAVAILABLE_TIER_MSG)
                    return
                from clients.gateway_mcp import gateway_mcp
                from handlers.hermes import _send_unavailable
                if not gateway_mcp.is_connected(chat_id):
                    await _send_unavailable(message, None, chat_id)
                    return
                hermes_msg = _HERMES_PREFIXES_RE.sub("", text).strip() or text
                session_id = _HERMES_SESSION_MAP.get(chat_id)
                try:
                    from helpers.typing_indicator import keep_typing
                    async with keep_typing(message):
                        response = await gateway_mcp.hermes_chat(
                            message=hermes_msg,
                            telegram_user_id=chat_id,
                            session_id=session_id,
                        )
                except Exception:
                    logger.exception("[fallback] hermes_chat failed for chat %s", chat_id)
                    response = None
                if response:
                    await message.answer(response)
                else:
                    await _send_unavailable(message, None, chat_id)
                return

        # Ф22 (WP-349): текстовый роутинг онбординг-интентов.
        # Условие: пользователь онбордирован, нет ни FSM-стейта ни SM custom state, текст не команда.
        if (text and not text.startswith('/') and
                intern and intern.get('onboarding_completed') and
                not intern.get('current_state')):
            current_state = await state.get_state()
            if current_state is None:
                from handlers.onboarding_intent import route_onboarding_intent
                handled = await route_onboarding_intent(text, chat_id, message, state)
                if handled:
                    return
                # WP-392: «Гермес»/«hermes» обрабатывается выше (префиксный триггер)
                # или hermes_router (handlers/hermes.py), зарегистрированный ДО fallback.

        logger.info(f"[SM] Routing message to SM: chat_id={chat_id}, len={len(text)}")
        try:
            await state.clear()
            if intern:
                await dispatcher.route_message(intern, message)
                return
            else:
                await dispatcher.sm.start({'telegram_id': chat_id}, context={'message': message})
                return
        except Exception as e:
            logger.error(f"[SM] Error in SM: {e}")
            import traceback
            logger.error(traceback.format_exc())
            lang = intern.get('language', 'ru') if intern else 'ru'
            await message.answer(
                f"⚠️ {t('errors.processing_error', lang)}\n\n"
                f"{t('errors.try_again_later', lang)}"
            )
            return

    # SM не активна — показываем подсказку
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') if intern else 'ru'
    await message.answer(t('errors.processing_error', lang))
