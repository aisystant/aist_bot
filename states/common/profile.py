"""
Стейт: Профиль пользователя (/profile).

Показывает данные о пользователе и позволяет их редактировать.
Содержит: имя, занятие, интересы, цели, мотивация, режим, длительность, сложность.

Принцип: профиль = ЧТО бот знает обо мне (персонализация контента).

Вход: по кнопке "Профиль" или команде /profile
Выход: saved → mode_select, cancel → _previous
"""

import logging
from typing import Optional
from datetime import datetime

from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from states.base import BaseState
from i18n import t
from db.queries.users import get_intern, update_intern

logger = logging.getLogger(__name__)


class ProfileState(BaseState):
    """
    Стейт профиля пользователя.

    Показывает карточку с данными и кнопками редактирования.
    """

    name = "common.profile"
    display_name = {
        "ru": "Профиль",
        "en": "Profile",
        "es": "Perfil",
        "fr": "Profil"
    }
    allow_global = []

    WAITING_FIELDS = {
        'name', 'occupation', 'interests', 'motivation', 'goals'
    }

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

    async def exit(self, user) -> dict:
        """Очищаем settings_waiting_for при выходе из стейта."""
        await self._set_waiting(user, None)
        return {}

    async def enter(self, user, context: dict = None) -> None:
        """Показываем карточку профиля с кнопками редактирования."""
        chat_id = self._get_chat_id(user)
        intern = await get_intern(chat_id)
        if not intern:
            await self.send(user, t('profile.not_found', self._get_lang(user)))
            return

        lang = intern.get('language', 'ru') or 'ru'

        study_duration = intern.get('study_duration') or 15
        bloom_level = intern.get('bloom_level') or 1
        bloom_emojis = {1: '🔵', 2: '🟡', 3: '🔴'}

        marathon_start = intern.get('marathon_start_date') or intern.get('marathon_started_at')
        if marathon_start:
            if isinstance(marathon_start, datetime):
                marathon_start_str = marathon_start.strftime('%d.%m.%Y')
            else:
                marathon_start_str = str(marathon_start)
        else:
            marathon_start_str = "—"

        interests = intern.get('interests', [])
        if isinstance(interests, list):
            interests_str = ', '.join(interests) if interests else '—'
        else:
            interests_str = interests or '—'

        motivation = intern.get('motivation', '') or ''
        motivation_short = motivation[:80] + '...' if len(motivation) > 80 else motivation or '—'
        goals = intern.get('goals', '') or ''
        goals_short = goals[:80] + '...' if len(goals) > 80 else goals or '—'

        # Результат теста систематичности
        assessment_line = ""
        assessment_state = intern.get('assessment_state')
        assessment_date = intern.get('assessment_date')
        if assessment_state and assessment_date:
            try:
                from core.assessment import load_assessment
                assess_data = load_assessment('systematicity')
                group = next(
                    (g for g in assess_data.get('groups', []) if g['id'] == assessment_state),
                    None,
                ) if assess_data else None
                if group:
                    a_emoji = group.get('emoji', '')
                    a_title = group.get('title', {}).get(lang, group.get('title', {}).get('ru', assessment_state))
                    a_date = assessment_date.strftime('%d.%m.%Y') if hasattr(assessment_date, 'strftime') else str(assessment_date)
                    assessment_line = f"\n📋 {t('assessment.profile_label', lang)}: {a_emoji} {a_title} ({a_date})"
            except Exception:
                pass

        text = (
            f"👤 *{intern.get('name', '—')}*\n"
            f"💼 {intern.get('occupation', '') or '—'}\n"
            f"🎨 {interests_str}\n\n"
            f"💫 {motivation_short}\n"
            f"🎯 {goals_short}\n\n"
            f"{t(f'duration.minutes_{study_duration}', lang)}\n"
            f"{bloom_emojis.get(bloom_level, '🔵')} {t(f'bloom.level_{bloom_level}_short', lang)}\n"
            f"🗓 {marathon_start_str}\n"
            f"⏰ {t('settings.schedule_marathon', lang)}: {intern.get('schedule_time', '09:00')}, "
            f"{t('settings.schedule_feed', lang)}: {intern.get('feed_schedule_time') or intern.get('schedule_time', '09:00')} (МСК)"
            f"{assessment_line}\n\n"
            f"*{t('settings.what_to_change', lang)}*"
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 " + t('buttons.name', lang), callback_data="upd_name"),
                InlineKeyboardButton(text="💼 " + t('buttons.occupation', lang), callback_data="upd_occupation")
            ],
            [
                InlineKeyboardButton(text="🎨 " + t('buttons.interests', lang), callback_data="upd_interests"),
                InlineKeyboardButton(text="🎯 " + t('buttons.goals', lang), callback_data="upd_goals")
            ],
            [
                InlineKeyboardButton(text="⏱ " + t('buttons.duration', lang), callback_data="upd_duration"),
                InlineKeyboardButton(text="📊 " + t('buttons.difficulty', lang), callback_data="upd_bloom")
            ],
            [
                InlineKeyboardButton(text="⏰ " + t('buttons.schedule', lang), callback_data="profile_schedule"),
                InlineKeyboardButton(text="📋 " + t('buttons.commands', lang), callback_data="show_commands")
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
        lang = self._get_lang(user)
        data = callback.data

        await callback.answer()

        if data == "settings_back":
            return "cancel"

        if data == "upd_name":
            return await self._ask_for_field(user, callback, 'name')
        if data == "upd_occupation":
            return await self._ask_for_field(user, callback, 'occupation')
        if data == "upd_interests":
            return await self._ask_for_field(user, callback, 'interests')
        if data == "upd_goals":
            return await self._ask_for_field(user, callback, 'goals')
        if data == "upd_duration":
            return await self._show_duration_options(user, callback)
        if data == "upd_bloom":
            return await self._show_bloom_options(user, callback)
        if data == "upd_mode":
            try:
                await callback.message.delete()
            except Exception:
                pass
            return "saved"

        if data == "show_commands":
            return await self._show_commands(user, callback)

        if data == "profile_schedule":
            return await self._show_schedule(user, callback)
        if data == "upd_schedule_marathon":
            return await self._ask_for_schedule(user, callback, 'schedule_marathon')
        if data == "upd_schedule_feed":
            return await self._ask_for_schedule(user, callback, 'schedule_feed')

        if data.startswith("duration_"):
            return await self._save_duration(user, callback, data)
        if data.startswith("bloom_"):
            return await self._save_bloom(user, callback, data)
        if data == "settings_back_to_menu":
            try:
                await callback.message.delete()
            except Exception:
                pass
            await self.enter(user)
            return None

        return None

    async def _ask_for_field(self, user, callback: CallbackQuery, field: str) -> Optional[str]:
        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)
        intern = await get_intern(chat_id)

        prompts = {
            'name': ('update.your_name', 'update.whats_your_name', intern.get('name', '—')),
            'occupation': ('update.your_occupation', 'update.whats_your_occupation', intern.get('occupation', '') or t('profile.not_specified', lang)),
            'interests': ('update.your_interests', 'update.what_interests', ', '.join(intern.get('interests', [])) if intern.get('interests') else t('profile.not_specified', lang)),
            'goals': ('update.your_goals', 'update.what_goals', intern.get('goals', '') or t('profile.not_specified', lang)),
        }

        label_key, prompt_key, current_value = prompts.get(field, ('', '', ''))
        emoji_map = {'name': '👤', 'occupation': '💼', 'interests': '🎨', 'goals': '🎯'}

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back_to_menu")]
        ])

        await callback.message.edit_text(
            f"{emoji_map.get(field, '')} *{t(label_key, lang)}:* {current_value}\n\n"
            f"{t(prompt_key, lang)}",
            parse_mode="Markdown",
            reply_markup=keyboard
        )

        await self._set_waiting(user, field)
        return None

    async def _handle_text_input(self, user, field: str, text: str) -> Optional[str]:
        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)

        if field == 'name':
            await update_intern(chat_id, name=text)
            await self.send(user, f"✅ {t('update.name_changed', lang)}: *{text}*", parse_mode="Markdown")
        elif field == 'occupation':
            await update_intern(chat_id, occupation=text)
            await self.send(user, f"✅ {t('update.occupation_changed', lang)}: *{text}*", parse_mode="Markdown")
        elif field == 'interests':
            interests = [i.strip() for i in text.split(',') if i.strip()]
            await update_intern(chat_id, interests=interests)
            await self.send(user, f"✅ {t('update.interests_changed', lang)}", parse_mode="Markdown")
        elif field == 'goals':
            await update_intern(chat_id, goals=text)
            await self.send(user, f"✅ {t('update.goals_changed', lang)}", parse_mode="Markdown")
        elif field == 'schedule_marathon':
            import re
            if re.match(r'^([01]?[0-9]|2[0-3]):([0-5][0-9])$', text):
                h, m = text.split(':')
                normalized = f"{int(h):02d}:{int(m):02d}"
                await update_intern(chat_id, schedule_time=normalized)
                await self.send(user, f"✅ {t('settings.schedule_marathon', lang)}: *{normalized}*", parse_mode="Markdown")
            else:
                await self.send(user, t('modes.invalid_time_format', lang))
                return None
        elif field == 'schedule_feed':
            import re
            if re.match(r'^([01]?[0-9]|2[0-3]):([0-5][0-9])$', text):
                h, m = text.split(':')
                normalized = f"{int(h):02d}:{int(m):02d}"
                await update_intern(chat_id, feed_schedule_time=normalized)
                await self.send(user, f"✅ {t('settings.schedule_feed', lang)}: *{normalized}*", parse_mode="Markdown")
            else:
                await self.send(user, t('modes.invalid_time_format', lang))
                return None

        await self._set_waiting(user, None)

        await self.enter(user)
        return None

    async def _ask_for_schedule(self, user, callback: CallbackQuery, field: str) -> Optional[str]:
        """Запрашиваем ввод времени."""
        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)
        intern = await get_intern(chat_id)

        if field == 'schedule_marathon':
            current_value = intern.get('schedule_time', '09:00')
            label_key = 'settings.schedule_marathon'
        else:
            current_value = intern.get('feed_schedule_time') or intern.get('schedule_time', '09:00')
            label_key = 'settings.schedule_feed'

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back_to_menu")]
        ])

        await callback.message.edit_text(
            f"⏰ *{t(label_key, lang)}:* {current_value}\n\n"
            f"{t('update.when_remind', lang)}",
            parse_mode="Markdown",
            reply_markup=keyboard
        )

        await self._set_waiting(user, field)
        return None

    async def _show_duration_options(self, user, callback: CallbackQuery) -> Optional[str]:
        lang = self._get_lang(user)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t('duration.minutes_5', lang), callback_data="duration_5")],
            [InlineKeyboardButton(text=t('duration.minutes_15', lang), callback_data="duration_15")],
            [InlineKeyboardButton(text=t('duration.minutes_25', lang), callback_data="duration_25")],
            [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back_to_menu")]
        ])
        await callback.message.edit_text(
            f"⏱ *{t('update.how_many_minutes', lang)}*",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return None

    async def _save_duration(self, user, callback: CallbackQuery, data: str) -> Optional[str]:
        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)
        duration = int(data.replace("duration_", ""))
        await update_intern(chat_id, study_duration=duration)
        await callback.message.edit_text(
            f"✅ {t('update.duration_changed', lang)}: *{duration} {t('modes.min_suffix', lang)}*",
            parse_mode="Markdown"
        )
        await self.enter(user)
        return None

    async def _show_bloom_options(self, user, callback: CallbackQuery) -> Optional[str]:
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        intern = await get_intern(chat_id)
        current_level = intern.get('bloom_level', 1)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"🔵 {t('bloom.level_1_short', lang)}" + (" ✓" if current_level == 1 else ""),
                callback_data="bloom_1"
            )],
            [InlineKeyboardButton(
                text=f"🟡 {t('bloom.level_2_short', lang)}" + (" ✓" if current_level == 2 else ""),
                callback_data="bloom_2"
            )],
            [InlineKeyboardButton(
                text=f"🔴 {t('bloom.level_3_short', lang)}" + (" ✓" if current_level == 3 else ""),
                callback_data="bloom_3"
            )],
            [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back_to_menu")]
        ])

        emojis = {1: '🔵', 2: '🟡', 3: '🔴'}
        await callback.message.edit_text(
            f"📊 *{t('update.current_difficulty', lang)}:* {emojis.get(current_level, '🔵')} {t(f'bloom.level_{current_level}_short', lang)}\n"
            f"_{t(f'bloom.level_{current_level}_desc', lang)}_\n\n"
            f"{t('update.select_difficulty', lang)}",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return None

    async def _save_bloom(self, user, callback: CallbackQuery, data: str) -> Optional[str]:
        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)
        level = int(data.replace("bloom_", ""))
        await update_intern(chat_id, bloom_level=level)
        await callback.message.edit_text(
            f"✅ {t('update.difficulty_changed', lang)}: *{t(f'bloom.level_{level}_short', lang)}*\n\n"
            f"{t(f'bloom.level_{level}_desc', lang)}",
            parse_mode="Markdown"
        )
        await self.enter(user)
        return None

    async def _show_schedule(self, user, callback: CallbackQuery) -> Optional[str]:
        """Подменю расписания: Марафон / Лента."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        intern = await get_intern(chat_id)

        marathon_time = intern.get('schedule_time', '09:00')
        feed_time = intern.get('feed_schedule_time') or marathon_time

        text = (
            f"⏰ *{t('settings.schedule_label', lang)}* (МСК)\n\n"
            f"📚 {t('settings.schedule_marathon', lang)}: *{marathon_time}*\n"
            f"📖 {t('settings.schedule_feed', lang)}: *{feed_time}*"
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"📚 {t('settings.schedule_marathon', lang)}", callback_data="upd_schedule_marathon")],
            [InlineKeyboardButton(text=f"📖 {t('settings.schedule_feed', lang)}", callback_data="upd_schedule_feed")],
            [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back_to_menu")],
        ])

        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return None

    async def _show_commands(self, user, callback: CallbackQuery) -> Optional[str]:
        """Показываем все команды бота."""
        lang = self._get_lang(user)

        text = (
            f"📋 *{t('help.commands_title', lang)}*\n\n"
            f"*{t('commands.section_main', lang)}*\n"
            f"{t('commands.start', lang)}\n"
            f"{t('commands.mode', lang)}\n"
            f"{t('commands.learn', lang)}\n"
            f"{t('commands.feed', lang)}\n"
            f"{t('commands.progress', lang)}\n"
            f"{t('commands.test', lang)}\n"
            f"{t('commands.plan', lang)}\n\n"
            f"*{t('commands.section_settings', lang)}*\n"
            f"{t('commands.profile', lang)}\n"
            f"{t('commands.settings', lang)}\n"
            f"{t('commands.mydata', lang)}\n"
            f"{t('commands.help', lang)}\n"
            f"{t('commands.language', lang)}\n\n"
            f"*{t('commands.section_special', lang)}*\n"
            f"{t('commands.notes', lang)}\n"
            f"{t('commands.consultation', lang)}\n"
            f"{t('commands.github', lang)}\n"
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="settings_back_to_menu")]
        ])

        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return None
