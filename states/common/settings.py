"""
Стейт: Настройки системы (/settings).

Конфигурация системных параметров бота:
- Язык интерфейса
- Расписание напоминаний
- Подключения (GitHub, Цифровой двойник)

Принцип: настройки = КАК система работает (конфигурация).
Персональные данные (ЧТО бот знает обо мне) → Profile.

Вход: по кнопке "Настройки" или команде /settings, /update
Выход: saved → mode_select, cancel → _previous
"""

import logging
import re
from typing import Optional

from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from states.base import BaseState
from i18n import t, SUPPORTED_LANGUAGES
from db.queries.users import get_intern, update_intern

logger = logging.getLogger(__name__)


def get_language_name(code: str) -> str:
    """Получить название языка по коду."""
    names = {
        'ru': '🇷🇺 Русский',
        'en': '🇬🇧 English',
        'es': '🇪🇸 Español',
        'fr': '🇫🇷 Français'
    }
    return names.get(code, code)


class SettingsState(BaseState):
    """
    Стейт настроек системы.

    Показывает системные настройки: язык, расписание, подключения.
    """

    name = "common.settings"
    display_name = {
        "ru": "Настройки",
        "en": "Settings",
        "es": "Ajustes",
        "fr": "Paramètres"
    }
    allow_global = []

    WAITING_FIELDS = {'schedule'}

    def _get_lang(self, user) -> str:
        if isinstance(user, dict):
            return user.get('language', 'ru')
        return getattr(user, 'language', 'ru') or 'ru'

    def _get_chat_id(self, user) -> int:
        if isinstance(user, dict):
            return user.get('chat_id')
        return getattr(user, 'chat_id', None)

    async def enter(self, user, context: dict = None) -> None:
        """Показываем системные настройки."""
        chat_id = self._get_chat_id(user)
        intern = await get_intern(chat_id)
        if not intern:
            await self.send(user, t('profile.not_found', self._get_lang(user)))
            return

        lang = intern.get('language', 'ru') or 'ru'

        text = (
            f"⚙️ *{t('settings.title', lang)}*\n\n"
            f"🌐 {t('settings.language_label', lang)}: {get_language_name(lang)}\n"
            f"⏰ {t('settings.schedule_label', lang)}: {intern.get('schedule_time', '09:00')}\n"
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🌐 " + t('buttons.change_language', lang), callback_data="upd_language"),
                InlineKeyboardButton(text="⏰ " + t('buttons.schedule', lang), callback_data="upd_schedule"),
            ],
            [
                InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back")
            ]
        ])

        await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")

        if isinstance(user, dict):
            user['state_context'] = user.get('state_context', {})
            user['state_context']['settings_waiting_for'] = None
        else:
            if not hasattr(user, 'state_context') or user.state_context is None:
                user.state_context = {}
            user.state_context['settings_waiting_for'] = None

    async def handle(self, user, message: Message) -> Optional[str]:
        """Обрабатываем ввод пользователя."""
        if isinstance(user, dict):
            waiting_for = user.get('state_context', {}).get('settings_waiting_for')
        else:
            waiting_for = getattr(user, 'state_context', {}).get('settings_waiting_for') if hasattr(user, 'state_context') else None

        text = (message.text or "").strip()

        if waiting_for:
            return await self._handle_text_input(user, waiting_for, text)

        await self.enter(user)
        return None

    async def handle_callback(self, user, callback: CallbackQuery) -> Optional[str]:
        """Обрабатываем нажатия inline-кнопок."""
        data = callback.data

        await callback.answer()

        if data == "settings_back":
            return "cancel"

        if data == "upd_language":
            return await self._show_language_options(user, callback)

        if data == "upd_schedule":
            return await self._ask_for_field(user, callback, 'schedule')

        if data.startswith("lang_"):
            return await self._save_language(user, callback, data)

        if data == "settings_back_to_menu":
            await self.enter(user)
            return None

        return None

    async def _ask_for_field(self, user, callback: CallbackQuery, field: str) -> Optional[str]:
        """Запрашиваем ввод текстового поля."""
        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)
        intern = await get_intern(chat_id)

        prompts = {
            'schedule': ('update.current_schedule', 'update.when_remind', intern.get('schedule_time', '09:00')),
        }

        label_key, prompt_key, current_value = prompts.get(field, ('', '', ''))
        emoji_map = {'schedule': '⏰'}

        await callback.message.edit_text(
            f"{emoji_map.get(field, '')} *{t(label_key, lang)}:* {current_value}\n\n"
            f"{t(prompt_key, lang)}",
            parse_mode="Markdown"
        )

        if isinstance(user, dict):
            user['state_context'] = user.get('state_context', {})
            user['state_context']['settings_waiting_for'] = field
        else:
            if not hasattr(user, 'state_context') or user.state_context is None:
                user.state_context = {}
            user.state_context['settings_waiting_for'] = field

        return None

    async def _handle_text_input(self, user, field: str, text: str) -> Optional[str]:
        """Сохраняем текстовый ввод."""
        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)

        if field == 'schedule':
            time_pattern = r'^([01]?[0-9]|2[0-3]):([0-5][0-9])$'
            if re.match(time_pattern, text):
                await update_intern(chat_id, schedule_time=text)
                await self.send(user, f"✅ {t('update.schedule_changed', lang)}: *{text}*", parse_mode="Markdown")
            else:
                await self.send(user, t('modes.invalid_time_format', lang))
                return None

        if isinstance(user, dict):
            user['state_context']['settings_waiting_for'] = None
        else:
            user.state_context['settings_waiting_for'] = None

        await self.enter(user)
        return None

    async def _show_language_options(self, user, callback: CallbackQuery) -> Optional[str]:
        """Показываем варианты языка."""
        lang = self._get_lang(user)

        buttons = [
            [InlineKeyboardButton(text=get_language_name(l), callback_data=f"lang_{l}")]
            for l in SUPPORTED_LANGUAGES
        ]
        buttons.append([InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back_to_menu")])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        await callback.message.edit_text(
            t('settings.language.title', lang),
            reply_markup=keyboard
        )
        return None

    async def _save_language(self, user, callback: CallbackQuery, data: str) -> Optional[str]:
        """Сохраняем выбранный язык."""
        chat_id = self._get_chat_id(user)

        new_lang = data.replace("lang_", "")
        if new_lang not in SUPPORTED_LANGUAGES:
            new_lang = 'ru'

        await update_intern(chat_id, language=new_lang)

        await callback.message.edit_text(
            t('settings.language.changed', new_lang),
        )

        if isinstance(user, dict):
            user['language'] = new_lang
        else:
            user.language = new_lang

        await self.enter(user)
        return None
