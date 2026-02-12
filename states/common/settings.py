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

    async def _set_waiting(self, user, field: str | None) -> None:
        """Сохраняем waiting_for в current_context (персистентно в БД)."""
        chat_id = self._get_chat_id(user)
        ctx = user.get('current_context', {}) if isinstance(user, dict) else {}
        ctx['settings_waiting_for'] = field
        await update_intern(chat_id, current_context=ctx)
        if isinstance(user, dict):
            user['current_context'] = ctx

    def _get_waiting(self, user) -> str | None:
        """Читаем waiting_for из current_context."""
        if isinstance(user, dict):
            return user.get('current_context', {}).get('settings_waiting_for')
        return None

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
                InlineKeyboardButton(text="🔗 " + t('settings.connections_label', lang), callback_data="upd_connections"),
            ],
            [
                InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back")
            ]
        ])

        await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")

        await self._set_waiting(user, None)

    async def handle(self, user, message: Message) -> Optional[str]:
        """Обрабатываем ввод пользователя."""
        waiting_for = self._get_waiting(user)

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

        if data == "upd_connections":
            return await self._show_connections(user, callback)

        if data.startswith("lang_"):
            return await self._save_language(user, callback, data)

        if data == "conn_github":
            return await self._handle_github_connection(user, callback)

        if data == "conn_twin":
            # Цифровой двойник — в разработке
            return None

        if data == "github_select_repo":
            return await self._github_select_repo(user, callback)

        if data.startswith("github_repo:"):
            return await self._github_repo_selected(user, callback, data)

        if data == "github_disconnect":
            return await self._github_disconnect(user, callback)

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

        await self._set_waiting(user, field)

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

        await self._set_waiting(user, None)

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

    async def _show_connections(self, user, callback: CallbackQuery) -> Optional[str]:
        """Показываем подключения к сторонним сервисам."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        # Проверяем GitHub подключение из github_connections таблицы
        from db.queries.github import get_github_connection
        gh_conn = await get_github_connection(chat_id)

        if gh_conn:
            gh_username = gh_conn.get('github_username', '')
            gh_repo = gh_conn.get('target_repo', '')
            if gh_username and gh_repo:
                github_status = f"✅ @{gh_username} → `{gh_repo}`"
            elif gh_username:
                github_status = f"✅ @{gh_username}"
            else:
                github_status = "✅ " + t('settings.connected', lang)
        else:
            github_status = t('settings.not_connected', lang)

        text = (
            f"🔗 *{t('settings.connections_label', lang)}*\n\n"
            f"🐙 GitHub: {github_status}\n"
            f"🤖 {t('settings.twin_label', lang)}: {t('settings.coming_soon', lang)}\n"
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🐙 GitHub", callback_data="conn_github")],
            [InlineKeyboardButton(text="🤖 " + t('settings.twin_label', lang), callback_data="conn_twin")],
            [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back_to_menu")]
        ])

        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return None

    async def _handle_github_connection(self, user, callback: CallbackQuery) -> Optional[str]:
        """Показываем статус GitHub или кнопку подключения."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from clients.github_oauth import github_oauth

        is_connected = await github_oauth.is_connected(chat_id)

        if is_connected:
            user_info = await github_oauth.get_user(chat_id)
            login = user_info.get("login", "user") if user_info else "user"
            target_repo = await github_oauth.get_target_repo(chat_id)
            notes_path = await github_oauth.get_notes_path(chat_id)

            lines = [f"🐙 *GitHub {t('settings.connected', lang)}*\n"]
            lines.append(f"{t('settings.github_user', lang)}: *{login}*")

            buttons = []
            if target_repo:
                lines.append(f"{t('settings.github_repo', lang)}: `{target_repo}`")
                lines.append(f"{t('settings.github_path', lang)}: `{notes_path}`")
            else:
                buttons.append([InlineKeyboardButton(
                    text=t('settings.github_select_repo', lang),
                    callback_data="github_select_repo",
                )])

            buttons.append([InlineKeyboardButton(
                text=t('settings.github_disconnect', lang),
                callback_data="github_disconnect",
            )])
            buttons.append([InlineKeyboardButton(
                text=t('buttons.back', lang),
                callback_data="upd_connections",
            )])

            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            await callback.message.edit_text(
                "\n".join(lines), parse_mode="Markdown", reply_markup=keyboard
            )
        else:
            try:
                auth_url, state = github_oauth.get_authorization_url(chat_id)
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=t('settings.github_connect', lang), url=auth_url)],
                    [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")],
                ])
                await callback.message.edit_text(
                    f"🐙 *GitHub*\n\n{t('settings.github_connect_desc', lang)}",
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
            except (ValueError, Exception) as e:
                logger.error(f"GitHub OAuth error: {e}")
                await callback.message.edit_text(
                    f"🐙 *GitHub*\n\n{t('settings.github_unavailable', lang)}",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")]
                    ]),
                )

        return None

    async def _github_select_repo(self, user, callback: CallbackQuery) -> Optional[str]:
        """Показываем список репозиториев для выбора."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from clients.github_oauth import github_oauth

        if not await github_oauth.is_connected(chat_id):
            await callback.message.edit_text(
                t('settings.not_connected', lang),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="conn_github")]
                ]),
            )
            return None

        repos = await github_oauth.get_repos(chat_id, limit=20)
        if not repos:
            await callback.message.edit_text(
                f"🐙 {t('settings.github_no_repos', lang)}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="conn_github")]
                ]),
            )
            return None

        buttons = []
        for repo in repos[:10]:
            full_name = repo.get("full_name", "")
            name = repo.get("name", "")
            buttons.append([InlineKeyboardButton(
                text=name, callback_data=f"github_repo:{full_name}",
            )])
        buttons.append([InlineKeyboardButton(
            text=t('buttons.back', lang), callback_data="conn_github",
        )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.edit_text(
            f"🐙 *{t('settings.github_select_repo', lang)}:*",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return None

    async def _github_repo_selected(self, user, callback: CallbackQuery, data: str) -> Optional[str]:
        """Сохраняем выбранный репозиторий."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from clients.github_oauth import github_oauth

        repo_full_name = data.split(":", 1)[1]
        await github_oauth.set_target_repo(chat_id, repo_full_name)
        notes_path = await github_oauth.get_notes_path(chat_id)

        await callback.message.edit_text(
            f"✅ {t('settings.github_repo', lang)}: `{repo_full_name}`\n"
            f"{t('settings.github_path', lang)}: `{notes_path}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="conn_github")]
            ]),
        )
        return None

    async def _github_disconnect(self, user, callback: CallbackQuery) -> Optional[str]:
        """Отключаем GitHub."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        from clients.github_oauth import github_oauth

        if await github_oauth.is_connected(chat_id):
            await github_oauth.disconnect(chat_id)

        await callback.message.edit_text(
            f"🐙 GitHub {t('settings.not_connected', lang)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="upd_connections")]
            ]),
        )
        return None
