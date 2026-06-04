"""
Fallback хендлеры — обработка неизвестных сообщений и callback-ов.

Если SM активна — делегирует в SM.
Иначе — показывает подсказку.
"""

import logging

from aiogram import Router
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from db.queries import get_intern
from i18n import t, detect_language

logger = logging.getLogger(__name__)

fallback_router = Router(name="fallback")


def _is_main_router_callback(callback: CallbackQuery) -> bool:
    """Проверяет, что callback НЕ принадлежит engines/ роутерам."""
    if not callback.data:
        return True
    excluded_prefixes = ('mode_', 'feed_', 'marathon_')
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

    if dispatcher and dispatcher.is_sm_active:
        intern = await get_intern(chat_id)

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

                # WP-392 Ф3.1: Hermes-роутер (DP.SC.167)
                # Требует явный prefix "Гермес"/"hermes" (peer-session 2026-06-04-50)
                _HERMES_PREFIXES = ("гермес", "hermes")
                _text_lower = text.lower().strip()
                _is_hermes = any(_text_lower.startswith(p) for p in _HERMES_PREFIXES)
                if not _is_hermes:
                    # Без prefix → тихий fallback в SM routing (вариант B MVP)
                    pass
                else:
                    # Проверяем тир из intern (public.users.tier — authoritative source)
                    tier_str = intern.get('tier', 'T1') if intern else 'T1'
                    tier_num = int(tier_str[1]) if (
                        isinstance(tier_str, str) and tier_str.startswith('T') and len(tier_str) == 2
                    ) else 1
                    if tier_num < 3:
                        await message.answer("Функция недоступна на твоём тире")
                        return
                    # T3+: убираем prefix + пунктуацию
                    import re
                    hermes_msg = re.sub(
                        r'^(гермес|hermes)[,:\s]+', '', text, flags=re.IGNORECASE
                    ).strip() or text
                    from clients.gateway_mcp import gateway_mcp
                    response = await gateway_mcp.hermes_chat(
                        message=hermes_msg,
                        telegram_user_id=chat_id,
                    )
                    await message.answer(response)
                    return

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
