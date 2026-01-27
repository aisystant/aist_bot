"""
Стейт: Выбор режима работы.

Вход: после онбординга или по команде /mode
Выход: workshop.marathon.lesson, feed.topics и т.д.
"""

from typing import Optional

from aiogram.types import Message

from states.base import BaseState
from i18n import t
from db.queries import update_intern


class ModeSelectState(BaseState):
    """
    Стейт выбора режима работы.

    Показывает доступные режимы (мастерские, консультанты) и переходит
    в выбранный режим.
    """

    name = "common.mode_select"
    display_name = {"ru": "Выбор режима", "en": "Mode Select"}
    allow_global = ["consultation", "notes"]

    # Тексты кнопок (для сравнения)
    MARATHON_BUTTONS = ["📚 Марафон", "📚 Marathon", "📚 Maratón"]
    FEED_BUTTONS = ["📖 Лента", "📖 Feed", "📖 Feed"]
    SETTINGS_BUTTONS = ["⚙️ Настройки", "⚙️ Settings", "⚙️ Ajustes"]

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

    async def enter(self, user, context: dict = None) -> None:
        """Показываем меню выбора режима."""
        lang = self._get_lang(user)

        # Формируем список доступных режимов
        buttons = [
            ["📚 Марафон" if lang == "ru" else "📚 Marathon"],
            ["📖 Лента" if lang == "ru" else "📖 Feed"],
            ["⚙️ Настройки" if lang == "ru" else "⚙️ Settings"],
        ]

        await self.send_with_keyboard(
            user,
            t('mode.select_mode', lang),
            buttons,
            one_time=False
        )

    async def handle(self, user, message: Message) -> Optional[str]:
        """Обрабатываем выбор режима."""
        text = (message.text or "").strip()
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        # Марафон
        if text in self.MARATHON_BUTTONS or "марафон" in text.lower() or "marathon" in text.lower():
            if chat_id:
                await update_intern(chat_id, mode='marathon')
            return "marathon"

        # Лента
        if text in self.FEED_BUTTONS or "лента" in text.lower() or "feed" in text.lower():
            if chat_id:
                await update_intern(chat_id, mode='feed')
            return "feed"

        # Настройки
        if text in self.SETTINGS_BUTTONS or "настройки" in text.lower() or "settings" in text.lower():
            return "settings"

        # Неизвестный выбор — показываем меню снова
        await self.send(user, t('mode.select_mode', lang))
        return None  # Остаёмся в стейте
