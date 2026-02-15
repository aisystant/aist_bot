"""
Стейт: Главное меню (генерируется из сервисного реестра).

Вход: после онбординга или по команде /mode, /start (existing user)
Выход: в entry_state выбранного сервиса

Меню строится из ServiceRegistry:
  menu(user) = registry.filter(access).render()

Добавление нового сервиса = 1 запись в services_init.py → меню обновляется.
"""

from typing import Optional

from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from states.base import BaseState
from core.registry import registry
from core import callback_protocol
from i18n import t, SUPPORTED_LANGUAGES
from db.queries.users import get_intern, update_intern


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

        # Language button — always in English for discoverability by non-native speakers
        all_buttons.append([InlineKeyboardButton(
            text="🌐 Language",
            callback_data="show_language",
        )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=all_buttons)

        await self.send(user, t('menu.main_title', lang), reply_markup=keyboard)

    async def handle(self, user, message: Message) -> Optional[str]:
        """Текстовый ввод в главном меню → показываем меню заново."""
        await self.enter(user)
        return None  # Остаёмся в стейте

    async def handle_callback(self, user, callback: CallbackQuery) -> Optional[str]:
        """Inline-кнопки в главном меню."""
        data = callback.data

        if data == "show_language":
            await callback.answer()
            return await self._show_language_options(user, callback)

        if data.startswith("lang_"):
            return await self._save_language(user, callback, data)

        return None

    def _get_language_name(self, code: str) -> str:
        names = {
            'ru': '🇷🇺 Русский', 'en': '🇬🇧 English',
            'es': '🇪🇸 Español', 'fr': '🇫🇷 Français', 'zh': '🇨🇳 中文',
        }
        return names.get(code, code)

    async def _show_language_options(self, user, callback: CallbackQuery) -> Optional[str]:
        """Show language selector."""
        lang = self._get_lang(user)
        buttons = [
            [InlineKeyboardButton(text=self._get_language_name(l), callback_data=f"lang_{l}")]
            for l in SUPPORTED_LANGUAGES
        ]
        buttons.append([InlineKeyboardButton(text=t('buttons.back', lang), callback_data="lang_back")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.edit_text(t('settings.language.title', lang), reply_markup=keyboard)
        return None

    async def _save_language(self, user, callback: CallbackQuery, data: str) -> Optional[str]:
        """Save selected language and rebuild menu."""
        if data == "lang_back":
            await callback.answer()
            await self.enter(user)
            return None

        chat_id = self._get_chat_id(user)
        new_lang = data.replace("lang_", "")
        if new_lang not in SUPPORTED_LANGUAGES:
            new_lang = 'ru'

        await update_intern(chat_id, language=new_lang)
        if isinstance(user, dict):
            user['language'] = new_lang

        await callback.answer(t('settings.language.changed', new_lang))
        await self.enter(user)
        return None

    def _get_chat_id(self, user) -> int:
        if isinstance(user, dict):
            return user.get('chat_id')
        return getattr(user, 'chat_id', None)
