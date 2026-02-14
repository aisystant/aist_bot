"""
Стейт: Обратная связь (/feedback).

Пользователь за 3-4 клика сообщает о проблеме или предложении.
Данные сохраняются в Neon (feedback_reports).
Красные баги → мгновенное уведомление разработчику.

Вход: /feedback или ! shortcut
Выход: _previous
"""

import json
import logging
import os
from typing import Optional

from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from states.base import BaseState
from i18n import t

logger = logging.getLogger(__name__)

# Сценарии (для кнопок выбора)
SCENARIOS = [
    ('marathon', '\U0001f4da'),   # 📚
    ('feed', '\U0001f4d6'),       # 📖
    ('test', '\U0001f9ea'),       # 🧪
    ('consultant', '\u2753'),     # ❓
    ('progress', '\U0001f4ca'),   # 📊
    ('other', '\U0001f4dd'),      # 📝
]

# Шаги flow
STEP_CATEGORY = 'category'
STEP_SCENARIO = 'scenario'
STEP_SEVERITY = 'severity'
STEP_TEXT = 'text'


class FeedbackState(BaseState):
    """Стейт сбора обратной связи."""

    name = "utility.feedback"
    display_name = {
        "ru": "Обратная связь",
        "en": "Feedback",
        "es": "Comentarios",
        "fr": "Retour"
    }
    allow_global = ["consultation", "notes"]

    def _get_lang(self, user) -> str:
        if isinstance(user, dict):
            return user.get('language', 'ru') or 'ru'
        return getattr(user, 'language', 'ru') or 'ru'

    def _get_chat_id(self, user) -> int:
        if isinstance(user, dict):
            return user.get('chat_id')
        return getattr(user, 'chat_id', None)

    async def _save_step(self, chat_id: int, step: str, data: dict):
        """Сохранить текущий шаг в current_context (namespaced)."""
        from db.queries import get_intern, update_intern
        intern = await get_intern(chat_id)
        ctx = {}
        if intern and intern.get('current_context'):
            try:
                ctx = json.loads(intern['current_context']) if isinstance(intern['current_context'], str) else intern['current_context']
            except (json.JSONDecodeError, TypeError):
                ctx = {}
        data['step'] = step
        ctx['feedback_step'] = data
        await update_intern(chat_id, current_context=ctx)

    async def _load_step(self, chat_id: int) -> dict:
        """Загрузить текущий шаг из current_context."""
        from db.queries import get_intern
        intern = await get_intern(chat_id)
        if not intern or not intern.get('current_context'):
            return {}
        try:
            ctx = json.loads(intern['current_context']) if isinstance(intern['current_context'], str) else intern['current_context']
        except (json.JSONDecodeError, TypeError):
            return {}
        return ctx.get('feedback_step', {})

    async def _clear_step(self, chat_id: int):
        """Очистить feedback_step из current_context."""
        from db.queries import get_intern, update_intern
        intern = await get_intern(chat_id)
        ctx = {}
        if intern and intern.get('current_context'):
            try:
                ctx = json.loads(intern['current_context']) if isinstance(intern['current_context'], str) else intern['current_context']
            except (json.JSONDecodeError, TypeError):
                ctx = {}
        ctx.pop('feedback_step', None)
        await update_intern(chat_id, current_context=ctx)

    async def enter(self, user, context: dict = None) -> Optional[str]:
        """Вход в стейт.

        Два пути:
        1. quick_message → сохранить сразу (severity=yellow)
        2. Обычный → показать выбор категории
        """
        context = context or {}
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        # Quick shortcut (от ! handler)
        if context.get('quick_message'):
            report_id = await self._save_report(chat_id, {
                'category': 'bug',
                'scenario': 'other',
                'severity': 'yellow',
                'message': context['quick_message'],
            })
            if report_id:
                await self.send(user, t('feedback.quick_saved', lang, id=report_id))
            else:
                await self.send(user, t('feedback.error', lang))
            return "submitted"

        # Обычный flow — категория
        await self._save_step(chat_id, STEP_CATEGORY, {})
        await self._show_category(user, lang)
        return None

    async def _show_category(self, user, lang: str):
        """Показать выбор категории."""
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"\U0001f41b {t('feedback.cat_bug', lang)}",
                    callback_data="feedback:cat:bug",
                ),
                InlineKeyboardButton(
                    text=f"\U0001f4a1 {t('feedback.cat_suggestion', lang)}",
                    callback_data="feedback:cat:suggestion",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"\u2190 {t('buttons.back', lang)}",
                    callback_data="feedback:cancel",
                ),
            ],
        ])
        await self.send(user, t('feedback.welcome', lang), reply_markup=keyboard)

    async def _show_scenarios(self, user, lang: str, callback: CallbackQuery):
        """Показать выбор сценария."""
        rows = []
        for i in range(0, len(SCENARIOS), 2):
            row = []
            for scenario_id, icon in SCENARIOS[i:i+2]:
                row.append(InlineKeyboardButton(
                    text=f"{icon} {t(f'feedback.scenario_{scenario_id}', lang)}",
                    callback_data=f"feedback:scenario:{scenario_id}",
                ))
            rows.append(row)
        rows.append([
            InlineKeyboardButton(
                text=f"\u2190 {t('buttons.back', lang)}",
                callback_data="feedback:cancel",
            ),
        ])
        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
        try:
            await callback.message.edit_text(
                t('feedback.scenario_prompt', lang),
                reply_markup=keyboard,
            )
        except Exception:
            await self.send(user, t('feedback.scenario_prompt', lang), reply_markup=keyboard)

    async def _show_severity(self, user, lang: str, callback: CallbackQuery):
        """Показать выбор серьёзности."""
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"\U0001f534 {t('feedback.severity_red', lang)}",
                callback_data="feedback:severity:red",
            )],
            [InlineKeyboardButton(
                text=f"\U0001f7e1 {t('feedback.severity_yellow', lang)}",
                callback_data="feedback:severity:yellow",
            )],
            [InlineKeyboardButton(
                text=f"\u2190 {t('buttons.back', lang)}",
                callback_data="feedback:cancel",
            )],
        ])
        try:
            await callback.message.edit_text(
                t('feedback.severity_prompt', lang),
                reply_markup=keyboard,
            )
        except Exception:
            await self.send(user, t('feedback.severity_prompt', lang), reply_markup=keyboard)

    async def _show_text_prompt(self, user, lang: str, callback: CallbackQuery):
        """Показать промпт для ввода текста."""
        try:
            await callback.message.edit_text(t('feedback.text_prompt', lang))
        except Exception:
            await self.send(user, t('feedback.text_prompt', lang))

    async def handle(self, user, message: Message) -> Optional[str]:
        """Обработка текстового ввода (только на шаге STEP_TEXT)."""
        text = (message.text or "").strip()
        if not text:
            return None

        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)
        step_data = await self._load_step(chat_id)

        if step_data.get('step') != STEP_TEXT:
            return None

        report_id = await self._save_report(chat_id, {
            'category': step_data.get('category', 'bug'),
            'scenario': step_data.get('scenario', 'other'),
            'severity': step_data.get('severity', 'yellow'),
            'message': text,
        })

        if report_id:
            await self.send(user, t('feedback.saved', lang, id=report_id))
        else:
            await self.send(user, t('feedback.error', lang))

        return "submitted"

    async def handle_callback(self, user, callback: CallbackQuery) -> Optional[str]:
        """Обработка inline-кнопок."""
        data = callback.data
        await callback.answer()

        if not data.startswith("feedback:"):
            return None

        parts = data.split(":", 2)
        action = parts[1] if len(parts) > 1 else ""
        payload = parts[2] if len(parts) > 2 else ""

        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)

        if action == "cat":
            if payload == "suggestion":
                # Предложение → severity=green auto, сразу к тексту
                await self._save_step(chat_id, STEP_TEXT, {
                    'category': 'suggestion',
                    'scenario': 'general',
                    'severity': 'green',
                })
                await self._show_text_prompt(user, lang, callback)
            else:
                # Баг → выбор сценария
                await self._save_step(chat_id, STEP_SCENARIO, {
                    'category': 'bug',
                })
                await self._show_scenarios(user, lang, callback)
            return None

        elif action == "scenario":
            step_data = await self._load_step(chat_id)
            step_data['scenario'] = payload
            await self._save_step(chat_id, STEP_SEVERITY, step_data)
            await self._show_severity(user, lang, callback)
            return None

        elif action == "severity":
            step_data = await self._load_step(chat_id)
            step_data['severity'] = payload
            await self._save_step(chat_id, STEP_TEXT, step_data)
            await self._show_text_prompt(user, lang, callback)
            return None

        elif action == "cancel":
            try:
                await callback.message.edit_text(t('feedback.cancel', lang))
            except Exception:
                pass
            return "cancel"

        return None

    async def exit(self, user) -> dict:
        """Очистить step data."""
        chat_id = self._get_chat_id(user)
        await self._clear_step(chat_id)
        return {}

    # === Internal ===

    async def _save_report(self, chat_id: int, data: dict) -> Optional[int]:
        """Сохранить отчёт в БД + отправить красное уведомление."""
        from db.queries.feedback import save_feedback

        try:
            report_id = await save_feedback(
                chat_id=chat_id,
                category=data['category'],
                scenario=data['scenario'],
                severity=data['severity'],
                message=data['message'],
            )
        except Exception as e:
            logger.error(f"[Feedback] Save error: {e}")
            return None

        # Красный → немедленное уведомление
        if data['severity'] == 'red' and report_id:
            await self._notify_developer_red(report_id, data)

        return report_id

    async def _notify_developer_red(self, report_id: int, data: dict):
        """Отправить 🔴 уведомление разработчику."""
        dev_chat_id = os.getenv("DEVELOPER_CHAT_ID")
        if not dev_chat_id:
            return

        scenario_label = data.get('scenario', 'other')
        message_preview = data['message'][:200]
        text = (
            f"\U0001f534 <b>BUG #{report_id}</b> | {scenario_label}\n\n"
            f"\"{message_preview}\""
        )

        try:
            await self.bot.send_message(int(dev_chat_id), text, parse_mode="HTML")
            logger.info(f"[Feedback] Red alert sent to developer: #{report_id}")
        except Exception as e:
            logger.error(f"[Feedback] Failed to notify developer: {e}")
