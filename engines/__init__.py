"""
Движки режимов бота.

Содержит:
- marathon/: движок режима Марафон
- feed/: движок режима Лента
- shared/: общие компоненты (question_handler)
- mode_selector.py: UI выбора режима
- integration.py: подключение роутеров
"""

from .integration import setup_routers, get_commands_list
from .mode_selector import mode_router

__all__ = [
    'setup_routers',
    'get_commands_list',
    'mode_router',
]
