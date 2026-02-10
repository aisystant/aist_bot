"""
Тонкие aiogram хендлеры для команд.

Каждый хендлер: получить пользователя → делегировать в Dispatcher.
Вся бизнес-логика — в State Machine (states/).
"""

import logging

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from db.queries import get_intern
from i18n import t

logger = logging.getLogger(__name__)

commands_router = Router(name="commands")


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
        await state.clear()
        await dispatcher.route_learn(intern)
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
        await state.clear()
        await dispatcher.route_command('feed', intern)
        return

    lang = intern.get('language', 'ru') or 'ru'
    await message.answer(t('feed.not_available', lang))


@commands_router.message(Command("update"))
async def cmd_update(message: Message, state: FSMContext):
    """Настройки через Dispatcher → common.settings."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    intern = await get_intern(message.chat.id)
    if not intern or not intern.get('onboarding_completed'):
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(t('errors.try_again', lang) + " /start")
        return

    if dispatcher and dispatcher.is_sm_active:
        await state.clear()
        await dispatcher.route_command('update', intern)
        return

    # Legacy fallback — show update screen directly
    from handlers.settings import _show_update_screen
    await _show_update_screen(message, intern, state)
