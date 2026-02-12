"""
Тонкие aiogram callback хендлеры.

Роутят callback queries в Dispatcher / State Machine.
"""

import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.fsm.context import FSMContext

from db.queries import get_intern
from i18n import t

logger = logging.getLogger(__name__)

callbacks_router = Router(name="callbacks")


# === Noop (разделители меню) ===

@callbacks_router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    """Разделитель в меню — без действия."""
    await callback.answer()


# === Сервисный реестр: единая точка входа для service:* callbacks ===

@callbacks_router.callback_query(F.data.startswith("service:"))
async def cb_service_select(callback: CallbackQuery, state: FSMContext):
    """Callback из главного меню (service registry).

    Формат callback_data: "service:{service_id}"
    Роутит в entry_state сервиса из реестра.
    """
    from handlers import get_dispatcher
    from core.registry import registry

    dispatcher = get_dispatcher()
    await callback.answer()

    intern = await get_intern(callback.message.chat.id)
    if not intern:
        return

    service = registry.resolve_callback(callback.data)
    if not service:
        logger.warning(f"[CB] Unknown service callback: {callback.data}")
        return

    if not (dispatcher and dispatcher.is_sm_active):
        lang = intern.get('language', 'ru') or 'ru'
        await callback.message.answer(t('errors.processing_error', lang))
        return

    # Определяем entry_state с учётом режима (mode-aware)
    entry_state = service.get_entry_state(intern)
    logger.info(f"[CB] Service select: {service.id} → {entry_state}")

    # Записываем аналитику использования сервиса
    await registry.record_usage(callback.message.chat.id, service.id)

    await state.clear()
    await callback.message.edit_reply_markup()
    await dispatcher.go_to(intern, entry_state)


# === Legacy callbacks (обратная совместимость) ===

@callbacks_router.callback_query(F.data == "learn")
async def cb_learn(callback: CallbackQuery, state: FSMContext):
    """Callback 'Учиться' — mode-aware через Dispatcher."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    await callback.answer()
    await callback.message.edit_reply_markup()

    intern = await get_intern(callback.message.chat.id)
    if not intern:
        return

    if dispatcher and dispatcher.is_sm_active:
        await state.clear()
        await dispatcher.route_learn(intern)
        return

    lang = intern.get('language', 'ru') or 'ru'
    await callback.message.answer(t('errors.processing_error', lang))


@callbacks_router.callback_query(F.data == "later")
async def cb_later(callback: CallbackQuery):
    """Callback 'Позже'."""
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') or 'ru'
    await callback.answer()
    await callback.message.edit_text(t('fsm.see_you_later', lang, time=intern['schedule_time']))


@callbacks_router.callback_query(F.data == "feed")
async def cb_feed(callback: CallbackQuery, state: FSMContext):
    """Callback для входа в Ленту."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    await callback.answer()
    await callback.message.edit_reply_markup()

    intern = await get_intern(callback.message.chat.id)
    if not intern:
        return

    if dispatcher and dispatcher.is_sm_active:
        await state.clear()
        await dispatcher.route_command('feed', intern)
        return

    lang = intern.get('language', 'ru') or 'ru'
    await callback.message.answer(t('feed.not_available', lang))


@callbacks_router.callback_query(F.data.startswith("feed_"))
async def cb_feed_actions(callback: CallbackQuery, state: FSMContext):
    """Обработка всех Feed-специфичных callback-ов через SM."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    intern = await get_intern(callback.message.chat.id)
    if not intern:
        await callback.answer()
        return

    if not (dispatcher and dispatcher.is_sm_active):
        await callback.answer()
        lang = intern.get('language', 'ru') or 'ru'
        await callback.message.answer(t('feed.not_available', lang))
        return

    data = callback.data
    logger.info(f"[CB] Feed callback '{data}' for chat_id={callback.message.chat.id}")

    try:
        current_state = intern.get('current_state', '')

        if data == "feed_get_digest":
            await callback.answer()
            await callback.message.edit_reply_markup()
            await state.clear()
            await dispatcher.go_to(intern, "feed.digest")

        elif data == "feed_topics_menu":
            await callback.answer()
            await callback.message.edit_reply_markup()
            await state.clear()
            await dispatcher.go_to(intern, "feed.digest", context={"show_topics_menu": True})

        elif current_state.startswith("feed."):
            # Пользователь уже в Feed-стейте — передаём callback в SM
            await dispatcher.route_callback(intern, callback)

        else:
            logger.warning(f"[CB] User in state '{current_state}' clicked '{data}', routing to feed.digest")
            await callback.answer()
            await callback.message.edit_reply_markup()
            await state.clear()
            await dispatcher.go_to(intern, "feed.digest")

    except Exception as e:
        logger.error(f"[CB] Error handling feed callback: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await callback.answer()
        lang = intern.get('language', 'ru') or 'ru'
        await callback.message.answer(t('errors.try_again', lang))


async def _is_in_sm_profile_or_settings_state(callback: CallbackQuery) -> bool:
    """Фильтр: пользователь в common.profile или common.settings стейте SM."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    if not (dispatcher and dispatcher.is_sm_active):
        return False
    intern = await get_intern(callback.message.chat.id)
    if not intern:
        return False
    current = intern.get('current_state', '')
    return current in ("common.profile", "common.settings")


@callbacks_router.callback_query(
    F.data.startswith("upd_") | F.data.startswith("settings_") | F.data.startswith("duration_") | F.data.startswith("bloom_") | F.data.startswith("lang_") | F.data.startswith("conn_") | F.data.startswith("github_") | (F.data == "show_commands"),
    _is_in_sm_profile_or_settings_state
)
async def cb_settings_actions(callback: CallbackQuery, state: FSMContext):
    """Profile/Settings callback-ы через SM."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    intern = await get_intern(callback.message.chat.id)
    if not intern:
        await callback.answer()
        return

    logger.info(f"[CB] Profile/Settings callback '{callback.data}' for chat_id={callback.message.chat.id}")
    try:
        await dispatcher.route_callback(intern, callback)
    except Exception as e:
        logger.error(f"[CB] Error handling profile/settings callback: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await callback.answer()
        lang = intern.get('language', 'ru') or 'ru'
        await callback.message.answer(t('errors.try_again', lang))


@callbacks_router.callback_query(F.data == "go_update")
async def cb_go_update(callback: CallbackQuery, state: FSMContext):
    """Переход к настройкам из progress и других мест."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    await callback.answer()
    intern = await get_intern(callback.message.chat.id)

    if dispatcher and dispatcher.is_sm_active and intern:
        try:
            await state.clear()
            await callback.message.delete()
            await dispatcher.route_command('update', intern)
            return
        except Exception as e:
            logger.error(f"[CB] Error routing go_update: {e}")

    # Legacy fallback
    if not intern:
        return
    from handlers.settings import _show_update_screen
    await _show_update_screen(callback.message, intern, state)


@callbacks_router.callback_query(F.data == "go_progress")
async def cb_go_progress(callback: CallbackQuery):
    """Переход к прогрессу."""
    await callback.answer()
    # Импортируем cmd_progress — он остаётся в bot.py на этом шаге
    from handlers.progress import cmd_progress
    await cmd_progress(callback.message)


async def _is_in_sm_assessment_state(callback: CallbackQuery) -> bool:
    """Фильтр: пользователь в workshop.assessment.* стейте SM."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    if not (dispatcher and dispatcher.is_sm_active):
        return False
    intern = await get_intern(callback.message.chat.id)
    if not intern:
        return False
    return (intern.get('current_state') or '').startswith("workshop.assessment.")


@callbacks_router.callback_query(
    F.data.startswith("assess_"),
    _is_in_sm_assessment_state
)
async def cb_assessment_actions(callback: CallbackQuery, state: FSMContext):
    """Assessment callback-ы через SM."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    intern = await get_intern(callback.message.chat.id)
    if not intern:
        await callback.answer()
        return

    logger.info(f"[CB] Assessment callback '{callback.data}' for chat_id={callback.message.chat.id}")
    try:
        await dispatcher.route_callback(intern, callback)
    except Exception as e:
        logger.error(f"[CB] Error handling assessment callback: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await callback.answer()
        lang = intern.get('language', 'ru') or 'ru'
        await callback.message.answer(t('errors.try_again', lang))
