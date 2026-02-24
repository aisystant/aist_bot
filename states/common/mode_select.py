"""
Стейт: Главное меню — tier-based ReplyKeyboard (WP-52).

Вход: после онбординга, /mode, /start (existing user), возврат из сервиса
Выход: через ReplyKeyboard → reply_keyboard handler → dispatcher

Меню = tier-based 2x2 ReplyKeyboard (tier_config.py / tier_ui.py).
Сервисы не на клавиатуре доступны через /command или /help.
"""

from typing import Optional

from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from states.base import BaseState
from core.tier_ui import build_reply_keyboard, sync_menu_commands
from core.tier_detector import detect_ui_tier
from core.tier_config import TIER_DISPLAY
from i18n import t, SUPPORTED_LANGUAGES
from db.queries.users import get_intern, update_intern


class ModeSelectState(BaseState):
    """
    Стейт главного меню.

    Отправляет tier-based ReplyKeyboard (2x2).
    Навигация через handlers/reply_keyboard.py.
    """

    name = "common.mode_select"
    keyboard_type = "reply"  # WP-52: SM knows this is a reply-keyboard state
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
        Показываем tier-based ReplyKeyboard.

        Если context содержит day_completed=True — не показываем меню.
        """
        context = context or {}
        lang = self._get_lang(user)

        # После завершения дня не показываем меню
        if context.get('day_completed'):
            return

        user_dict = self._user_dict(user)

        # Tier-based ReplyKeyboard + Menu ☰ sync (WP-52 v4)
        chat_id = user_dict['chat_id']
        tier = await detect_ui_tier(chat_id)
        keyboard = build_reply_keyboard(tier, lang)

        name = user_dict.get('name', '')
        tier_label = TIER_DISPLAY.get(tier, f"T{tier}")
        greeting = t('welcome.menu_greeting', lang, name=name, tier=tier_label)
        await self.send(user, greeting, reply_markup=keyboard, parse_mode="Markdown")
        await sync_menu_commands(self.bot, chat_id, tier, lang)

    async def handle(self, user, message: Message) -> Optional[str]:
        """Текстовый ввод в главном меню → показываем меню заново."""
        await self.enter(user)
        return None  # Остаёмся в стейте

    async def handle_callback(self, user, callback: CallbackQuery) -> Optional[str]:
        """Inline-кнопки — backwards compat для старых сообщений в чате."""
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
        """Show language selector (backwards compat for old inline messages)."""
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
        # Инвалидация пре-генерированного контента (мог быть на старом языке)
        from db.queries.marathon import invalidate_user_content
        await invalidate_user_content(chat_id)
        if isinstance(user, dict):
            user['language'] = new_lang

        await callback.answer(t('settings.language.changed', new_lang))
        await self.enter(user)
        return None

    def _get_chat_id(self, user) -> int:
        if isinstance(user, dict):
            return user.get('chat_id')
        return getattr(user, 'chat_id', None)
