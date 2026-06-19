"""
Регистрация всех aiogram хендлеров.

Все хендлеры — тонкие обёртки, делегирующие логику в core/dispatcher.py.
"""

from aiogram import Dispatcher as AiogramDispatcher
from core.dispatcher import Dispatcher as BotDispatcher

# Module-level reference, set during setup_handlers()
_dispatcher: BotDispatcher = None


def get_dispatcher() -> BotDispatcher:
    """Get the bot dispatcher. Must be called after setup_handlers()."""
    return _dispatcher


def setup_handlers(dp: AiogramDispatcher, dispatcher: BotDispatcher) -> None:
    """Подключает commands и callbacks роутеры.

    Вызывается ПЕРЕД подключением bot.py router.
    Fallback подключается отдельно через setup_fallback() ПОСЛЕ bot.py router.

    Args:
        dp: Aiogram Dispatcher
        dispatcher: Наш core.dispatcher.Dispatcher
    """
    global _dispatcher
    _dispatcher = dispatcher

    from .onboarding import onboarding_router
    from .commands import commands_router
    from .callbacks import callbacks_router
    from .settings import settings_router
    from .progress import progress_router
    from .points import points_router
    from .consent import consent_router
    from .twin import twin_router
    from .github import github_router
    from .google_calendar import gcal_router
    from .wakatime import wakatime_router
    from .strategist import strategist_router
    from .feedback import feedback_router
    from .dev import dev_router
    from .subscription_stars import subscription_stars_router
    from .payments import payments_router
    from .discourse import discourse_router
    from .link import link_router
    from .schedule import schedule_router
    from .guide import guide_router
    from .subscription import subscription_router
    from .contacts import contacts_router
    from .buy import buy_router
    from .info import info_router
    from .features import features_router
    from .channels import channels_router
    from .workshop import workshop_router
    from .showcase import showcase_router
    from .ory_register import ory_register_router
    from .delivery_prefs import delivery_prefs_router
    from .connect import connect_router
    from .status import status_router
    from .legal import legal_router
    from .reflect import reflect_router
    from .slot import slot_router
    from .diagnose import diagnose_router
    from .simulator import simulator_router
    from .remind import remind_router
    from .marathon import marathon_router
    from .support import support_router
    from .setup import setup_router
    from .tier_upgrade import tier_upgrade_router
    from .referral import referral_router
    from .day import day_router
    from .external_session import external_session_router
    from .iwe import iwe_router
    from .hermes import hermes_router
    from .byok import byok_router

    dp.include_router(onboarding_router)
    dp.include_router(workshop_router)
    dp.include_router(showcase_router)
    dp.include_router(subscription_stars_router)
    dp.include_router(payments_router)
    dp.include_router(commands_router)
    dp.include_router(callbacks_router)
    dp.include_router(settings_router)
    dp.include_router(progress_router)
    dp.include_router(points_router)
    dp.include_router(consent_router)
    dp.include_router(twin_router)
    dp.include_router(strategist_router)
    dp.include_router(dev_router)
    dp.include_router(feedback_router)
    dp.include_router(github_router)
    dp.include_router(gcal_router)
    dp.include_router(wakatime_router)
    dp.include_router(discourse_router)
    dp.include_router(link_router)
    dp.include_router(schedule_router)
    dp.include_router(guide_router)
    dp.include_router(subscription_router)
    dp.include_router(contacts_router)
    dp.include_router(buy_router)
    dp.include_router(info_router)
    dp.include_router(features_router)
    dp.include_router(channels_router)
    dp.include_router(ory_register_router)
    dp.include_router(delivery_prefs_router)
    dp.include_router(connect_router)
    dp.include_router(status_router)
    dp.include_router(legal_router)
    dp.include_router(reflect_router)
    dp.include_router(slot_router)
    dp.include_router(diagnose_router)
    dp.include_router(simulator_router)
    dp.include_router(remind_router)
    dp.include_router(marathon_router)
    dp.include_router(support_router)
    dp.include_router(setup_router)
    dp.include_router(tier_upgrade_router)
    dp.include_router(referral_router)
    # WP-428: day_router, iwe_router ДО hermes_router.
    dp.include_router(day_router)
    dp.include_router(iwe_router)
    # WP-392: hermes_router ДО external_session — «Гермес» адресует Hermes-рантайм,
    # а не активную Claude-сессию (которая иначе перехватила бы текст первой).
    dp.include_router(hermes_router)
    dp.include_router(byok_router)
    dp.include_router(external_session_router)

    # ReplyKeyboard text → command routing (AFTER all command routers, BEFORE fallback)
    from .reply_keyboard import reply_kb_router
    dp.include_router(reply_kb_router)


def setup_fallback(dp: AiogramDispatcher) -> None:
    """Подключает fallback роутер (catch-all).

    ДОЛЖЕН вызываться ПОСЛЕ всех остальных роутеров.
    """
    from .fallback import fallback_router
    dp.include_router(fallback_router)
