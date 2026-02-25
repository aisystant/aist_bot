"""
Стейт: Дашборд тренировки.

Вход: из training.setup или training.assignment
Показывает прогресс по принципам. Кнопки: Продолжить, Сменить режим, Настройки.
"""

from typing import Optional, Dict

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from states.base import BaseState
from engines.training.engine import TrainingEngine
from config import get_logger, ZP_PRINCIPLES, TRAINING_MAX_DEPTH

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

    _user_data: Dict[int, Dict] = {}

    def _get_chat_id(self, user) -> int:
        if isinstance(user, dict):
            return user.get('chat_id')
        return getattr(user, 'chat_id', None)

    def _get_lang(self, user) -> str:
        if isinstance(user, dict):
            return user.get('language', 'ru') or 'ru'
        return getattr(user, 'language', 'ru') or 'ru'

    async def enter(self, user, context: dict = None) -> Optional[str]:
        chat_id = self._get_chat_id(user)
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
        cognitive_label = data.get('cognitive_label', 'Взрослый')
        mode_label = data.get('mode_label', '🔀 Все вперемешку')

        lines = [
            "🧠 *Тренировка принципов*",
            f"Режим: {mode_label}",
            f"Уровень: {cognitive_label}",
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
        cognitive_label = data.get('cognitive_label', 'Взрослый') if data else 'Взрослый'
        mode_label = data.get('mode_label', '🔀 Все вперемешку') if data else ''

        text = (
            "⚙️ *Настройки тренировки*\n\n"
            f"Режим: {mode_label}\n"
            f"Уровень: {cognitive_label}"
        )

        buttons = [
            [InlineKeyboardButton(
                text="🧠 Изменить уровень",
                callback_data="train_settings_cognitive"
            )],
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

    async def handle(self, user, message: Message) -> Optional[str]:
        text = message.text or ''

        if text.startswith('train_start_'):
            principle_id = text.replace('train_start_', '')
            chat_id = self._get_chat_id(user)
            self._user_data[chat_id] = {'principle_id': principle_id}
            return "select_principle"

        return None

    async def handle_callback(self, user, callback: CallbackQuery) -> Optional[str]:
        """Обработка inline-кнопок."""
        data = callback.data or ''
        chat_id = self._get_chat_id(user)

        if data == 'train_continue':
            engine = TrainingEngine(chat_id)
            next_pid = await engine.get_next_principle()
            if next_pid:
                self._user_data[chat_id] = {'principle_id': next_pid}
                await callback.answer("Генерирую задание...")
                return "select_principle"
            else:
                await callback.answer("Все принципы пройдены!", show_alert=True)
                return None

        if data == 'train_change_mode':
            self._user_data[chat_id] = {'mode_only': True}
            await callback.answer()
            return "change_mode"

        if data.startswith('train_start_'):
            principle_id = data.replace('train_start_', '')
            self._user_data[chat_id] = {'principle_id': principle_id}
            await callback.answer("Генерирую задание...")
            return "select_principle"

        if data == 'train_settings':
            await callback.answer()
            await self._show_settings(user, chat_id)
            return None

        if data == 'train_settings_cognitive':
            await callback.answer()
            return "settings"  # → training.setup (полный setup)

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
            # Перерисовать дашборд
            return "reset_done"

        if data == 'train_settings_back':
            await callback.answer()
            # Перерисовать дашборд
            return "refresh"

        if data == 'train_exit':
            await callback.answer()
            return "back"

        return None

    async def exit(self, user) -> dict:
        chat_id = self._get_chat_id(user)
        data = self._user_data.pop(chat_id, {})
        return data
