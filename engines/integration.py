"""
Интеграция движков в основной бот.

Этот файл содержит функцию для подключения всех роутеров.
Добавьте в bot.py после создания диспетчера:

    from engines.integration import setup_routers
    setup_routers(dp)

Или вручную:

    from engines.feed import feed_router
    from engines.mode_selector import mode_router

    dp.include_router(mode_router)
    dp.include_router(feed_router)
"""

from aiogram import Dispatcher

from config import get_logger

logger = get_logger(__name__)


def setup_routers(dp: Dispatcher):
    """Подключает все роутеры движков к диспетчеру

    Args:
        dp: Dispatcher aiogram
    """
    # Роутер выбора режима
    from .mode_selector import mode_router
    dp.include_router(mode_router)
    logger.info("✓ Подключен mode_router (/mode)")

    # Роутер режима Лента
    from .feed import feed_router
    dp.include_router(feed_router)
    logger.info("✓ Подключен feed_router (/feed)")

    # TODO: Роутер режима Марафон (когда будет вынесен из bot.py)
    # from .marathon import marathon_router
    # dp.include_router(marathon_router)

    logger.info("✅ Все роутеры движков подключены")


def get_commands_list() -> list:
    """Возвращает список команд для регистрации в боте"""
    return [
        ("mode", "Выбор режима (Марафон/Лента)"),
        ("feed", "Режим Лента — персональные темы"),
        ("feed_status", "Статус режима Лента"),
    ]
