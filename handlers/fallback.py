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
    excluded_prefixes = ('mode_', 'feed_')
    return not callback.data.startswith(excluded_prefixes)


@fallback_router.callback_query(_is_main_router_callback)
async def on_unknown_callback(callback: CallbackQuery, state: FSMContext):
    """Обработка callback-запросов — делегирование в State Machine."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    chat_id = callback.message.chat.id

    if dispatcher and dispatcher.is_sm_active:
        try:
            await state.clear()
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
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    chat_id = message.chat.id
    text = message.text or ''

    if dispatcher and dispatcher.is_sm_active:
        logger.info(f"[SM] Routing message to SM: chat_id={chat_id}, text={text[:50]}")
        try:
            await state.clear()
            intern = await get_intern(chat_id)
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
            intern = await get_intern(chat_id)
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
