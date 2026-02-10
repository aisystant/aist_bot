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
    display_name = {"ru": "Выбор режима", "en": "Mode Select", "es": "Selección de modo", "fr": "Sélection du mode"}
    allow_global = ["consultation", "notes"]

    # Тексты кнопок по языкам: {lang: text}
    MARATHON_LABELS = {"ru": "📚 Марафон", "en": "📚 Marathon", "es": "📚 Maratón", "fr": "📚 Marathon"}
    FEED_LABELS = {"ru": "📖 Лента", "en": "📖 Feed", "es": "📖 Feed", "fr": "📖 Fil"}
    SETTINGS_LABELS = {"ru": "⚙️ Настройки", "en": "⚙️ Settings", "es": "⚙️ Ajustes", "fr": "⚙️ Paramètres"}

    # Все варианты кнопок (для сравнения в handle)
    MARATHON_BUTTONS = list(MARATHON_LABELS.values())
    FEED_BUTTONS = list(FEED_LABELS.values())
    SETTINGS_BUTTONS = list(SETTINGS_LABELS.values())

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
        """
        Показываем меню выбора режима.

        Если context содержит day_completed=True, значит пользователь
        только что завершил день — не показываем меню, просто ждём.
        """
        context = context or {}
        lang = self._get_lang(user)

        # После завершения дня не показываем меню режимов
        if context.get('day_completed'):
            # Пользователь завершил день, меню не нужно
            # Он может использовать /learn или /mode когда захочет
            return

        # Формируем список доступных режимов
        buttons = [
            [self.MARATHON_LABELS.get(lang, self.MARATHON_LABELS["en"])],
            [self.FEED_LABELS.get(lang, self.FEED_LABELS["en"])],
            [self.SETTINGS_LABELS.get(lang, self.SETTINGS_LABELS["en"])],
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
