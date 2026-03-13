"""
Тонкие aiogram хендлеры для команд.

Каждый хендлер: получить пользователя → делегировать в Dispatcher.
Вся бизнес-логика — в State Machine (states/).
"""

import asyncio
import logging
import traceback

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from db.queries import get_intern
from db.queries.users import is_onboarded
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
    if not await is_onboarded(intern):
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(t('profile.first_start', lang))
        return

    if dispatcher and dispatcher.is_sm_active:
        await _safe_route(message, state, intern, dispatcher.route_command('mode', intern, context={'source': 'mode'}))
        return

    lang = intern.get('language', 'ru') or 'ru'
    await message.answer(t('errors.processing_error', lang))


@commands_router.message(Command("learn"))
async def cmd_learn(message: Message, state: FSMContext):
    """Начать обучение — mode-aware через Dispatcher."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    intern = await get_intern(message.chat.id)
    if not await is_onboarded(intern):
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
    if not await is_onboarded(intern):
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(t('profile.first_start', lang))
        return

    if dispatcher and dispatcher.is_sm_active:
        await _safe_route(message, state, intern, dispatcher.route_command('feed', intern))
        return

    lang = intern.get('language', 'ru') or 'ru'
    await message.answer(t('feed.not_available', lang))


@commands_router.message(Command("train"))
async def cmd_train(message: Message, state: FSMContext):
    """Тренировка принципов через Dispatcher → training.dashboard."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    intern = await get_intern(message.chat.id)
    if not await is_onboarded(intern):
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(t('profile.first_start', lang))
        return

    if dispatcher and dispatcher.is_sm_active:
        await _safe_route(message, state, intern, dispatcher.route_command('train', intern))
        return

    lang = intern.get('language', 'ru') or 'ru'
    await message.answer(t('errors.processing_error', lang))


@commands_router.message(Command("profile"))
async def cmd_profile(message: Message, state: FSMContext):
    """Профиль пользователя через Dispatcher → common.profile."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    intern = await get_intern(message.chat.id)
    if not await is_onboarded(intern):
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(t('errors.try_again', lang) + " /start")
        return

    if dispatcher and dispatcher.is_sm_active:
        await _safe_route(message, state, intern, dispatcher.route_command('profile', intern))
        return

    lang = intern.get('language', 'ru') or 'ru'
    await message.answer(t('errors.processing_error', lang))


@commands_router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext):
    """Настройки системы через Dispatcher → common.settings."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    intern = await get_intern(message.chat.id)
    if not await is_onboarded(intern):
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(t('errors.try_again', lang) + " /start")
        return

    if dispatcher and dispatcher.is_sm_active:
        await _safe_route(message, state, intern, dispatcher.route_command('settings', intern))
        return

    # Legacy fallback — show update screen directly
    from handlers.settings import _show_update_screen
    await _show_update_screen(message, intern, state)


@commands_router.message(Command("mydata"))
async def cmd_mydata(message: Message, state: FSMContext):
    """Персональный дата-центр через Dispatcher → utility.mydata."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    intern = await get_intern(message.chat.id)
    if not await is_onboarded(intern):
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(t('profile.first_start', lang))
        return

    if dispatcher and dispatcher.is_sm_active:
        await _safe_route(message, state, intern, dispatcher.route_command('mydata', intern))
        return

    lang = intern.get('language', 'ru') or 'ru'
    await message.answer(t('errors.processing_error', lang))


@commands_router.message(Command("waka"))
async def cmd_waka(message: Message, state: FSMContext):
    """WakaTime — статистика рабочего времени пользователя."""
    intern = await get_intern(message.chat.id)
    if not await is_onboarded(intern):
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(t('profile.first_start', lang))
        return

    lang = intern.get('language', 'ru') or 'ru'

    # Per-user key из БД (как GitHub — каждый подключает свой)
    from db.queries.wakatime import get_wakatime_connection
    waka_conn = await get_wakatime_connection(message.chat.id)

    if not waka_conn:
        await message.answer(
            t('settings.waka_intro', lang) + "\n\n"
            + t('settings.waka_enter_key', lang).replace(':', '') + " → /settings",
            parse_mode="Markdown",
        )
        return

    api_key = waka_conn.get('api_key')
    from clients.wakatime import wakatime_client

    try:
        today_sum, yesterday_sum, week = await asyncio.gather(
            wakatime_client.get_day_summary(api_key, day=None, today=True),
            wakatime_client.get_day_summary(api_key),
            wakatime_client.get_week_summary(api_key),
        )
        text = wakatime_client.format_telegram(today_sum, yesterday_sum, week)
        await message.answer(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"[CMD] /waka error for chat_id={message.chat.id}: {e}")
        await message.answer(t('errors.processing_error', lang))


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

    if not await is_onboarded(intern):
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(t('profile.first_start', lang))
        return

    if dispatcher and dispatcher.is_sm_active:
        await _safe_route(message, state, intern, dispatcher.route_command('assessment', intern))
        return

    logger.warning(f"[CMD] /test: no active SM (dispatcher={bool(dispatcher)})")
    lang = intern.get('language', 'ru') or 'ru'
    await message.answer(t('errors.processing_error', lang))
