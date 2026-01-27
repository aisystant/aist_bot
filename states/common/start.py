"""
Стейт: Начало работы, онбординг нового пользователя.

Вход: при /start или первом сообщении
Выход: common.mode_select после завершения онбординга
"""

from typing import Optional

from aiogram.types import Message

from states.base import BaseState
from i18n import t
from db.queries import update_intern


class StartState(BaseState):
    """
    Стейт начала работы.

    Для новых пользователей: онбординг (запрос имени и т.д.)
    Для существующих: переход к выбору режима
    """

    name = "common.start"
    display_name = {"ru": "Начало", "en": "Start"}
    allow_global = []  # Глобальные команды недоступны на старте

    def _get_lang(self, user) -> str:
        """Получить язык пользователя."""
        if isinstance(user, dict):
            return user.get('language', 'ru')
        return getattr(user, 'language', 'ru') or 'ru'

    def _get_chat_id(self, user) -> int:
        """Получить chat_id пользователя."""
        if isinstance(user, dict):
            return user.get('chat_id')
        return getattr(user, 'chat_id', None)

    def _get_name(self, user) -> str:
        """Получить имя пользователя."""
        if isinstance(user, dict):
            return user.get('name', '')
        return getattr(user, 'name', '') or ''

    async def enter(self, user, context: dict = None) -> None:
        """Показываем приветствие."""
        lang = self._get_lang(user)
        user_name = self._get_name(user)

        if user_name:
            # Существующий пользователь
            await self.send(user, t('onboarding.welcome_back', lang, name=user_name))
        else:
            # Новый пользователь
            await self.send(user, t('onboarding.welcome', lang))
            await self.send(user, t('onboarding.ask_name', lang))

    async def handle(self, user, message: Message) -> Optional[str]:
        """
        Обрабатываем ответ пользователя.

        Если пользователь существующий — сразу переходим к mode_select.
        Если новый — сохраняем имя и переходим к mode_select.
        """
        lang = self._get_lang(user)
        user_name = self._get_name(user)

        if user_name:
            # Существующий пользователь — сразу переходим
            return "existing_user"

        # Новый пользователь — сохраняем имя
        name = (message.text or "").strip()

        if not name or len(name) < 2:
            await self.send(user, t('onboarding.ask_name', lang))
            return None  # Остаёмся в стейте

        # Сохраняем имя в БД
        chat_id = self._get_chat_id(user)
        if chat_id:
            await update_intern(chat_id, name=name)

        # Приветствуем
        await self.send(user, t('onboarding.nice_to_meet', lang, name=name))

        return "onboarding_complete"

    async def exit(self, user) -> dict:
        """Передаём контекст следующему стейту."""
        return {"from_onboarding": not self._get_name(user)}
