"""
Стейт: Главное меню (генерируется из сервисного реестра).

Вход: после онбординга или по команде /mode, /start (existing user)
Выход: в entry_state выбранного сервиса

Меню строится из ServiceRegistry:
  menu(user) = registry.filter(access).render()

Добавление нового сервиса = 1 запись в services_init.py → меню обновляется.
"""

from typing import Optional

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from states.base import BaseState
from core.registry import registry
from core import callback_protocol
from i18n import t


class ModeSelectState(BaseState):
    """
    Стейт главного меню.

    Генерирует inline keyboard из сервисного реестра.
    Callback-ы обрабатываются в handlers/callbacks.py (cb_service_select).
    """

    name = "common.mode_select"
    display_name = {"ru": "Главное меню", "en": "Main Menu", "es": "Menú principal", "fr": "Menu principal"}
    allow_global = ["consultation", "notes"]

    def _get_lang(self, user) -> str:
        """Получить язык пользователя."""
        if isinstance(user, dict):
            return user.get('language', 'ru')
        return getattr(user, 'language', 'ru') or 'ru'

    def _user_dict(self, user) -> dict:
        """Привести user к dict для реестра."""
        if isinstance(user, dict):
            return user
        return {
            'chat_id': getattr(user, 'chat_id', None),
            'language': getattr(user, 'language', 'ru'),
            'mode': getattr(user, 'mode', 'marathon'),
        }

    async def enter(self, user, context: dict = None) -> None:
        """
        Показываем главное меню из сервисного реестра.

        Если context содержит day_completed=True — не показываем меню.
        """
        context = context or {}
        lang = self._get_lang(user)

        # После завершения дня не показываем меню
        if context.get('day_completed'):
            return

        user_dict = self._user_dict(user)

        # Собираем видимые сервисы по категориям (scenario + system)
        scenario_services = await registry.for_user(user_dict, category="scenario")
        system_services = await registry.for_user(user_dict, category="system")

        all_buttons = []

        # Каждый сервис — отдельная строка (полная ширина, корректно на Desktop)
        for services in [scenario_services, system_services]:
            for s in services:
                all_buttons.append([InlineKeyboardButton(
                    text=f"{s.icon} {t(s.i18n_key, lang)}",
                    callback_data=callback_protocol.encode("service", s.id),
                )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=all_buttons)

        await self.send(user, t('menu.main_title', lang), reply_markup=keyboard)

    async def handle(self, user, message: Message) -> Optional[str]:
        """Текстовый ввод в главном меню → показываем меню заново."""
        await self.enter(user)
        return None  # Остаёмся в стейте
