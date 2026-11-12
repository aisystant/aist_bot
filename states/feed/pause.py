"""
Feed pause / resume (WP-5 F76).

Pause stops the daily digest notification without touching the feed itself:
the scheduler only picks readers with feed_status='active', so flipping the
status is enough. Feed weeks, topics and sessions stay as they are; a completed
week is re-activated by the scheduler after resume as usual.

Both transitions are idempotent: a repeated tap (old notification, double tap)
changes nothing and writes no event.
"""

from typing import Optional

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from config import FeedStatus, get_logger
from db.queries.events import log_event
from db.queries.users import derive_mode, update_intern
from i18n import t

logger = get_logger(__name__)

PAUSE_CALLBACK = "feed_pause"
RESUME_CALLBACK = "feed_resume"


def toggle_button(feed_status: str, lang: str) -> Optional[InlineKeyboardButton]:
    """Pause button for an active feed, resume button for a paused one, else None."""
    if feed_status == FeedStatus.ACTIVE:
        return InlineKeyboardButton(text=t('buttons.feed_pause', lang), callback_data=PAUSE_CALLBACK)
    if feed_status == FeedStatus.PAUSED:
        return InlineKeyboardButton(text=t('buttons.feed_resume', lang), callback_data=RESUME_CALLBACK)
    return None


def toggle_row(feed_status: str, lang: str) -> list:
    """Keyboard rows with the toggle button, or no rows when the feed was never started."""
    button = toggle_button(feed_status, lang)
    return [[button]] if button else []


async def set_feed_status(intern: dict, target: str) -> bool:
    """Move the feed to `target` (ACTIVE or PAUSED). Returns False when it is already there."""
    if (intern.get('feed_status') or FeedStatus.NOT_STARTED) == target:
        return False
    chat_id = intern['chat_id']
    new_mode = derive_mode(intern.get('marathon_status') or 'not_started', target)
    trigger = 'feed_pause' if target == FeedStatus.PAUSED else 'feed_resume'
    await update_intern(chat_id, feed_status=target, mode=new_mode)
    await log_event(chat_id, 'mode_changed', {
        'from_mode': intern.get('mode'),
        'to_mode': new_mode,
        'trigger': trigger,
    })
    logger.info(f"[FeedPause] {trigger}: chat_id={chat_id} mode={new_mode}")
    return True


async def handle_toggle(callback: CallbackQuery, intern: dict) -> None:
    """Callback handler for the pause / resume buttons; works from any screen and state."""
    lang = intern.get('language', 'ru') or 'ru'
    pause = callback.data == PAUSE_CALLBACK
    target = FeedStatus.PAUSED if pause else FeedStatus.ACTIVE

    if not await set_feed_status(intern, target):
        await callback.answer(t('feed.pause_already_paused' if pause else 'feed.pause_already_active', lang))
        return

    # The tapped message keeps its keyboard (menus stay usable); a stale toggle there is a no-op.
    await callback.answer()
    await callback.message.answer(
        t('feed.paused_confirm' if pause else 'feed.resumed_confirm', lang),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=toggle_row(target, lang)),
    )
