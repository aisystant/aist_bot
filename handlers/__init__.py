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
    from .linear import linear_router
    from .twin import twin_router

    dp.include_router(onboarding_router)
    dp.include_router(commands_router)
    dp.include_router(callbacks_router)
    dp.include_router(settings_router)
    dp.include_router(progress_router)
    dp.include_router(linear_router)
    dp.include_router(twin_router)


def setup_fallback(dp: AiogramDispatcher) -> None:
    """Подключает fallback роутер (catch-all).

    ДОЛЖЕН вызываться ПОСЛЕ всех остальных роутеров.
    """
    from .fallback import fallback_router
    dp.include_router(fallback_router)
