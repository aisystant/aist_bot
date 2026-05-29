from __future__ import annotations

"""
Стейт: Дашборд тренировки.

Вход: из training.setup или training.assignment
Показывает прогресс по принципам. Кнопки: Продолжить, Сменить режим, Настройки.
"""

import asyncio
from typing import Optional

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from states.base import BaseState
from engines.training.engine import TrainingEngine
from config import get_logger, ZP_PRINCIPLES, TRAINING_MAX_DEPTH, TRAINING_COGNITIVE_LEVELS

logger = get_logger(__name__)


def _progress_bar(current: int, total: int) -> str:
    filled = '█' * current
    empty = '░' * (total - current)
    return f"[{filled}{empty}] {current}/{total}"


class TrainingDashboardState(BaseState):
    """Дашборд тренировки — показ прогресса и выбор принципа."""

    name = "training.dashboard"
    display_name = {
        "ru": "Дашборд тренировки",
        "en": "Training Dashboard",
    }
    allow_global = ["consultation", "notes"]

    def _get_lang(self, user) -> str:
        if isinstance(user, dict):
            return user.get('language', 'ru') or 'ru'
        return getattr(user, 'language', 'ru') or 'ru'

    async def enter(self, user, context: dict = None) -> Optional[str]:
        chat_id = self._get_chat_id_from_user(user)
        engine = TrainingEngine(chat_id)

        # Проверить setup
        if not await engine.is_setup_complete():
            return "setup_needed"

        data = await engine.get_dashboard_data()
        if not data:
            await self.send(user, "Ошибка загрузки дашборда.")
            return "back"

        principles = data.get('principles', [])
        stats = data.get('stats', {})
        mode_label = data.get('mode_label', '🔀 Все вперемешку')

        lines = [
            "🧠 *Тренировка принципов*",
            f"Режим: {mode_label}",
            "━━━━━━━━━━━━━━━━━━",
        ]

        has_incomplete = False
        for p in principles:
            if not p['enabled']:
                continue
            bar = _progress_bar(p['current_depth'], p['max_depth'])
            status = "✅" if p['completed'] else ""
            lines.append(f"{p['id']} {p['name']}  {bar} {status}")
            if not p['completed']:
                has_incomplete = True

        lines.append("━━━━━━━━━━━━━━━━━━")
        total = stats.get('total_attempts', 0)
        passed = stats.get('total_passed', 0)
        if total > 0:
            lines.append(f"Попыток: {total} | Пройдено: {passed}")

        # Кнопки
        buttons = []

        # Главная кнопка — Продолжить (если есть непройденные)
        if has_incomplete:
            buttons.append([InlineKeyboardButton(
                text="▶️ Продолжить",
                callback_data="train_continue"
            )])

        buttons.append([InlineKeyboardButton(
            text="👶 Занятие с ребёнком",
            callback_data="train_child"
        )])

        buttons.append([
            InlineKeyboardButton(text="🔄 Сменить режим", callback_data="train_change_mode"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="train_settings"),
        ])
        buttons.append([InlineKeyboardButton(
            text="← Назад",
            callback_data="train_exit"
        )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        text = '\n'.join(lines)

        await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")
        return None

    async def _show_settings(self, user, chat_id: int):
        """Показать подменю настроек."""
        engine = TrainingEngine(chat_id)
        data = await engine.get_dashboard_data()
        mode_label = data.get('mode_label', '🔀 Все вперемешку') if data else ''

        text = (
            "⚙️ *Настройки тренировки*\n\n"
            f"Режим: {mode_label}"
        )

        buttons = [
            [InlineKeyboardButton(
                text="🗑 Сбросить прогресс",
                callback_data="train_reset_ask"
            )],
            [InlineKeyboardButton(
                text="← Назад",
                callback_data="train_settings_back"
            )],
        ]

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")

    async def _show_child_select(self, user, chat_id: int):
        """Показать выбор ребёнка или создание нового."""
        engine = TrainingEngine(chat_id)
        children = await engine.get_children()

        buttons = []
        for child in children:
            buttons.append([InlineKeyboardButton(
                text=f"👶 {child['name']}",
                callback_data=f"train_child_select_{child['id']}"
            )])

        buttons.append([InlineKeyboardButton(
            text="➕ Добавить ребёнка",
            callback_data="train_child_add"
        )])
        buttons.append([InlineKeyboardButton(
            text="← Назад",
            callback_data="train_child_back"
        )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        text = "👶 *Занятие с ребёнком*\n\nВыберите ребёнка или добавьте нового:"
        await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")

    async def _show_child_age_select(self, user):
        """Показать выбор возраста ребёнка."""
        # Только детские уровни (без postformal)
        child_levels = {
            'preoperational': 'Ребёнок (3-6)',
            'concrete_operational': 'Ребёнок (7-11)',
            'formal_operational': 'Подросток (11-16)',
        }

        buttons = []
        for key, label in child_levels.items():
            buttons.append([InlineKeyboardButton(
                text=label, callback_data=f"train_child_age_{key}"
            )])
        buttons.append([InlineKeyboardButton(
            text="← Назад", callback_data="train_child_back"
        )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await self.send(
            user,
            "👶 *Укажите возраст ребёнка:*",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    async def handle(self, user, message: Message) -> Optional[str]:
        text = (message.text or '').strip()
        chat_id = self._get_chat_id_from_user(user)

        if text.startswith('train_start_'):
            principle_id = text.replace('train_start_', '')
            await self.save_state(user, {'principle_id': principle_id})
            return "select_principle"

        # Ввод имени ребёнка
        data = await self.load_state(user)
        if data.get('waiting_child_name') and text:
            cognitive_level = data.get('child_cognitive_level', 'concrete_operational')
            engine = TrainingEngine(chat_id)
            child = await engine.add_child(text, cognitive_level)
            if child:
                await self.save_state(user, {'child_id': child['id']})
                return "child_selected"
            else:
                await self.send(user, "Не удалось добавить ребёнка. Попробуйте ещё раз.")
                return None

        return None

    async def handle_callback(self, user, callback: CallbackQuery) -> Optional[str]:
        """Обработка inline-кнопок."""
        data = callback.data or ''
        chat_id = self._get_chat_id_from_user(user)

        if data == 'train_continue':
            engine = TrainingEngine(chat_id)
            # DP.SC.154 fix: circuit breaker for heavy DB callback (continuation via shield)
            task = asyncio.create_task(engine.get_next_principle())
            try:
                next_pid = await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
            except asyncio.TimeoutError:
                await callback.answer("Генерирую задание... (подождите)")
                try:
                    next_pid = await asyncio.wait_for(task, timeout=27.0)
                except asyncio.TimeoutError:
                    logger.error(f"[TrainingDashboard] HARD TIMEOUT train_continue chat_id={chat_id}")
                    await callback.answer("Не удалось загрузить задание. Попробуйте позже.", show_alert=True)
                    return None
                finally:
                    if not task.done():
                        task.cancel()
            finally:
                if not task.done():
                    task.cancel()
            if next_pid:
                await self.save_state(user, {'principle_id': next_pid})
                await callback.answer("Генерирую задание...")
                return "select_principle"
            else:
                await callback.answer("Все принципы пройдены!", show_alert=True)
                return None

        if data == 'train_change_mode':
            await self.save_state(user, {'mode_only': True})
            await callback.answer()
            return "change_mode"

        if data.startswith('train_start_'):
            principle_id = data.replace('train_start_', '')
            await self.save_state(user, {'principle_id': principle_id})
            await callback.answer("Генерирую задание...")
            return "select_principle"

        if data == 'train_child':
            await callback.answer()
            await self._show_child_select(user, chat_id)
            return None

        if data.startswith('train_child_select_'):
            child_id = int(data.replace('train_child_select_', ''))
            await self.save_state(user, {'child_id': child_id})
            await callback.answer()
            return "child_selected"

        if data == 'train_child_add':
            await callback.answer()
            await self._show_child_age_select(user)
            return None

        if data.startswith('train_child_age_'):
            level = data.replace('train_child_age_', '')
            await self.save_state(user, {
                'waiting_child_name': True,
                'child_cognitive_level': level,
            })
            await callback.answer()
            await self.send(user, "✏️ Напишите имя ребёнка:")
            return None

        if data == 'train_child_back':
            await callback.answer()
            await self.enter(user)
            return None

        if data == 'train_settings':
            await callback.answer()
            await self._show_settings(user, chat_id)
            return None

        if data == 'train_reset_ask':
            # Подтверждение сброса
            buttons = [
                [InlineKeyboardButton(
                    text="✅ Да, сбросить",
                    callback_data="train_reset_confirm"
                )],
                [InlineKeyboardButton(
                    text="← Отмена",
                    callback_data="train_settings_back"
                )],
            ]
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            await self.send(
                user,
                "⚠️ *Сбросить весь прогресс тренировки?*\n\n"
                "Все глубины принципов обнулятся. Это действие нельзя отменить.",
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
            await callback.answer()
            return None

        if data == 'train_reset_confirm':
            engine = TrainingEngine(chat_id)
            await engine.reset_progress()
            await callback.answer("Прогресс сброшен!")
            await self.enter(user)
            return None

        if data == 'train_settings_back':
            await callback.answer()
            await self.enter(user)
            return None

        if data == 'train_exit':
            await callback.answer()
            return "back"

        return None

    async def exit(self, user) -> dict:
        data = await self.load_state(user)
        await self.clear_state(user)
        return data
