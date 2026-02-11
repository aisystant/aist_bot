"""
Центральный диспетчер — единая точка роутинга всех входящих сообщений.

Все entry points (команды, callbacks, scheduler) проходят через Dispatcher.
Добавление нового режима = добавить строку в MODE_STATE_MAP (core/helpers.py).
Добавление новой команды = добавить строку в COMMAND_MAP.
"""

import logging
from typing import Optional

from aiogram import Bot

from core.helpers import get_user_mode_state

logger = logging.getLogger(__name__)


# Реестр команд → целевой стейт SM
COMMAND_MAP = {
    'feed': 'feed.topics',
    'mode': 'common.mode_select',
    'update': 'common.settings',
    'settings': 'common.settings',
    'assessment': 'workshop.assessment.flow',
}


class Dispatcher:
    """
    Центральный диспетчер бота.

    Единая точка роутинга, заменяющая разбросанные
    ``if state_machine is not None`` проверки в bot.py.
    """

    def __init__(self, state_machine, bot: Bot):
        self.sm = state_machine
        self.bot = bot

    @property
    def is_sm_active(self) -> bool:
        """State Machine инициализирована и доступна."""
        return self.sm is not None

    async def route_command(self, command: str, user: dict) -> bool:
        """Роутинг команды → SM стейт из COMMAND_MAP.

        Returns:
            True если обработано через SM, False если нет маппинга.
        """
        if not self.sm:
            return False
        target = COMMAND_MAP.get(command)
        if target:
            logger.info(f"[Dispatcher] route_command: /{command} → {target}")
            await self.sm.go_to(user, target)
            return True
        return False

    async def route_learn(self, user: dict) -> None:
        """Единая точка входа для /learn — mode-aware.

        Определяет режим пользователя (marathon/feed) и направляет
        в соответствующий стейт SM.
        """
        target = get_user_mode_state(user)
        logger.info(f"[Dispatcher] route_learn: mode={user.get('mode')} → {target}")
        await self.sm.go_to(user, target)

    async def route_message(self, user: dict, message) -> bool:
        """Роутинг произвольных сообщений через SM.

        Returns:
            True если обработано.
        """
        if not self.sm:
            return False
        await self.sm.handle(user, message)
        return True

    async def route_callback(self, user: dict, callback) -> bool:
        """Роутинг callback query → SM.

        Returns:
            True если обработано.
        """
        if not self.sm:
            return False
        await self.sm.handle_callback(user, callback)
        return True

    async def route_scheduled(self, user: dict) -> None:
        """Роутинг для scheduler — mode-aware, аналогично route_learn."""
        await self.route_learn(user)

    async def go_to(self, user: dict, state_name: str, context: dict = None) -> None:
        """Прямой переход в указанный стейт SM."""
        if self.sm:
            await self.sm.go_to(user, state_name, context)
