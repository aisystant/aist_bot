"""
Handler for ReplyKeyboard button presses.

Routes emoji+text button presses to their corresponding command handlers.
Registered AFTER command routers, BEFORE fallback router (handlers/__init__.py).

WP-52: Progressive UI per Tier
"""

import logging

from aiogram import Router, F
from aiogram.types import Message
from aiogram.fsm.context import FSMContext

from core.tier_config import ALL_KB_TEXTS, REPLY_KB_TEXTS_TO_COMMANDS
from db.queries import get_intern
from db.queries.users import is_onboarded

logger = logging.getLogger(__name__)

reply_kb_router = Router(name="reply_keyboard")


@reply_kb_router.message(F.text.in_(ALL_KB_TEXTS))
async def on_reply_keyboard_press(message: Message, state: FSMContext):
    """Route ReplyKeyboard button press to the corresponding command."""
    command = REPLY_KB_TEXTS_TO_COMMANDS.get(message.text)
    if not command:
        return

    intern = await get_intern(message.chat.id)
    if not await is_onboarded(intern):
        return

    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    if dispatcher and dispatcher.is_sm_active:
        try:
            await state.clear()
            routed = await dispatcher.route_command(command, intern)
            if routed:
                return
        except Exception as e:
            logger.error(f"[ReplyKB] Error routing '{message.text}' -> /{command}: {e}")
            lang = intern.get('language', 'ru') or 'ru'
            from i18n import t
            await message.answer(t('errors.processing_error', lang))
            return

    # Fallback: try calling handler directly for non-SM commands
    await _fallback_route(message, state, command, intern)


async def _fallback_route(message: Message, state: FSMContext, command: str, intern: dict):
    """Fallback routing for commands not in State Machine (e.g. /progress, /settings)."""
    try:
        if command == 'progress':
            from handlers.progress import cmd_progress
            await cmd_progress(message, state)
        elif command == 'settings':
            from handlers.commands import cmd_settings
            await cmd_settings(message, state)
        elif command == 'club':
            from handlers.discourse import cmd_club
            await cmd_club(message, state)
        elif command == 'twin':
            from handlers.twin import cmd_twin
            await cmd_twin(message)
        elif command == 'link':
            from handlers.link import cmd_link
            await cmd_link(message)
        elif command == 'schedule':
            from handlers.schedule import cmd_schedule
            await cmd_schedule(message)
        elif command == 'guide':
            from handlers.guide import cmd_guide
            await cmd_guide(message)
        elif command == 'subscription':
            from handlers.subscription import cmd_subscription
            await cmd_subscription(message)
        elif command == 'contacts':
            from handlers.contacts import cmd_contacts
            await cmd_contacts(message)
        elif command == 'buy':
            from handlers.buy import cmd_buy
            await cmd_buy(message)
        elif command == 'about':
            from handlers.info import cmd_about
            await cmd_about(message)
        elif command == 'mydata':
            from handlers.commands import cmd_mydata
            await cmd_mydata(message, state)
        else:
            lang = intern.get('language', 'ru') or 'ru'
            from i18n import t
            await message.answer(t('errors.processing_error', lang))
    except Exception as e:
        logger.error(f"[ReplyKB] Fallback error for /{command}: {e}")
