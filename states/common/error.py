"""
Стейт: Обработка ошибок.

Вход: при ошибке в любом стейте
Выход: common.start (retry) или _previous (continue)
"""

from typing import Optional

from aiogram.types import Message

from states.base import BaseState
from i18n import t


class ErrorState(BaseState):
    """
    Стейт обработки ошибок.

    Показывает сообщение об ошибке и предлагает повторить или продолжить.
    """

    name = "common.error"
    display_name = {"ru": "Ошибка", "en": "Error", "es": "Error", "fr": "Erreur"}
    allow_global = []
    keyboard_type = "reply"

    # Тексты кнопок
    RETRY_BUTTONS = ["🔄 Повторить", "🔄 Retry", "🔄 Reintentar", "🔄 Réessayer"]
    BACK_BUTTONS = ["◀️ Назад", "◀️ Back", "◀️ Atrás", "◀️ Retour"]

    def _get_lang(self, user) -> str:
        """Получить язык пользователя."""
        if isinstance(user, dict):
            return user.get('language', 'ru')
        return getattr(user, 'language', 'ru') or 'ru'

    async def enter(self, user, context: dict = None) -> None:
        """Показываем сообщение об ошибке."""
        lang = self._get_lang(user)

        # Получаем детали ошибки из контекста
        error_message = (context or {}).get("error_message")

        if error_message:
            await self.send(user, f"❌ Произошла ошибка:\n\n{error_message}")
        else:
            await self.send(user, t('error.generic', lang))

        # WP-151 Ф3: error_shown
        from db.queries.events import log_event
        user_id = user.get('chat_id') or user.get('id') if isinstance(user, dict) else getattr(user, 'chat_id', 0)
        await log_event(user_id, 'error_shown', {
            'error_key': error_message[:200] if error_message else 'generic',
            'handler': 'ErrorState',
        })

        # Показываем кнопки
        retry_btn = "🔄 Повторить" if lang == "ru" else "🔄 Retry"
        back_btn = "◀️ Назад" if lang == "ru" else "◀️ Back"

        buttons = [[retry_btn], [back_btn]]
        await self.send_with_keyboard(
            user,
            t('error.choose_action', lang),
            buttons
        )

    async def handle(self, user, message: Message) -> Optional[str]:
        """Обрабатываем выбор пользователя."""
        text = (message.text or "").strip().lower()

        if "повторить" in text or "retry" in text or text in [b.lower() for b in self.RETRY_BUTTONS]:
            return "retry"
        elif "назад" in text or "back" in text or text in [b.lower() for b in self.BACK_BUTTONS]:
            return "continue"

        # Любой другой текст — продолжаем
        return "continue"
