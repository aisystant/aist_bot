"""
Тонкие aiogram хендлеры для команд.

Каждый хендлер: получить пользователя → делегировать в Dispatcher.
Вся бизнес-логика — в State Machine (states/).
"""

import logging
import traceback

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from db.queries import get_intern
from i18n import t

logger = logging.getLogger(__name__)

commands_router = Router(name="commands")


async def _safe_route(message: Message, state: FSMContext, intern: dict, route_coro):
    """Обёртка: clear FSM → route через SM → catch ошибки."""
    lang = intern.get('language', 'ru') or 'ru'
    try:
        await state.clear()
        await route_coro
    except Exception as e:
        logger.error(f"[CMD] SM routing error for chat_id={message.chat.id}: {e}")
        logger.error(traceback.format_exc())
        await message.answer(t('errors.processing_error', lang))


@commands_router.message(Command("mode"))
async def cmd_mode(message: Message, state: FSMContext):
    """Главное меню через Dispatcher → common.mode_select."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    intern = await get_intern(message.chat.id)
    if not intern or not intern.get('onboarding_completed'):
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(t('profile.first_start', lang))
        return

    if dispatcher and dispatcher.is_sm_active:
        await _safe_route(message, state, intern, dispatcher.route_command('mode', intern))
        return

    lang = intern.get('language', 'ru') or 'ru'
    await message.answer(t('errors.processing_error', lang))


@commands_router.message(Command("learn"))
async def cmd_learn(message: Message, state: FSMContext):
    """Начать обучение — mode-aware через Dispatcher."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    intern = await get_intern(message.chat.id)
    if not intern or not intern.get('onboarding_completed'):
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(t('profile.first_start', lang))
        return

    if dispatcher and dispatcher.is_sm_active:
        await _safe_route(message, state, intern, dispatcher.route_learn(intern))
        return

    lang = intern.get('language', 'ru') if intern else 'ru'
    await message.answer(t('errors.processing_error', lang))


@commands_router.message(Command("feed"))
async def cmd_feed(message: Message, state: FSMContext):
    """Вход в режим Лента через Dispatcher."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    intern = await get_intern(message.chat.id)
    if not intern or not intern.get('onboarding_completed'):
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(t('profile.first_start', lang))
        return

    if dispatcher and dispatcher.is_sm_active:
        await _safe_route(message, state, intern, dispatcher.route_command('feed', intern))
        return

    lang = intern.get('language', 'ru') or 'ru'
    await message.answer(t('feed.not_available', lang))


@commands_router.message(Command("profile"))
async def cmd_profile(message: Message, state: FSMContext):
    """Профиль пользователя через Dispatcher → common.profile."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    intern = await get_intern(message.chat.id)
    if not intern or not intern.get('onboarding_completed'):
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(t('errors.try_again', lang) + " /start")
        return

    if dispatcher and dispatcher.is_sm_active:
        await _safe_route(message, state, intern, dispatcher.route_command('profile', intern))
        return

    lang = intern.get('language', 'ru') or 'ru'
    await message.answer(t('errors.processing_error', lang))


@commands_router.message(Command("settings"))
@commands_router.message(Command("update"))
async def cmd_settings(message: Message, state: FSMContext):
    """Настройки системы через Dispatcher → common.settings."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    intern = await get_intern(message.chat.id)
    if not intern or not intern.get('onboarding_completed'):
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(t('errors.try_again', lang) + " /start")
        return

    if dispatcher and dispatcher.is_sm_active:
        await _safe_route(message, state, intern, dispatcher.route_command('settings', intern))
        return

    # Legacy fallback — show update screen directly
    from handlers.settings import _show_update_screen
    await _show_update_screen(message, intern, state)


@commands_router.message(Command("test"))
@commands_router.message(Command("assessment"))
async def cmd_assessment(message: Message, state: FSMContext):
    """Запуск теста оценки систематичности через Dispatcher."""
    logger.info(f"[CMD] /test received from chat_id={message.chat.id}")
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    try:
        intern = await get_intern(message.chat.id)
    except Exception as e:
        logger.error(f"[CMD] /test get_intern failed: {e}")
        await message.answer("⚠️ Ошибка загрузки профиля. Попробуйте позже.")
        return

    if not intern or not intern.get('onboarding_completed'):
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(t('profile.first_start', lang))
        return

    if dispatcher and dispatcher.is_sm_active:
        await _safe_route(message, state, intern, dispatcher.route_command('assessment', intern))
        return

    logger.warning(f"[CMD] /test: no active SM (dispatcher={bool(dispatcher)})")
    lang = intern.get('language', 'ru') or 'ru'
    await message.answer(t('errors.processing_error', lang))
