"""
Стейт: Планы (интеграция со Стратегом).

Хаб для просмотра DayPlan, WeekPlan и итогов недели из GitHub-репо стратега.
Итоги недели читаются из секции «Итоги W{N}» в WeekPlan (fallback на WeekReport).
Требует подключённого GitHub и выбранного strategy_repo.

Вход: по кнопке «Планы» из меню или через callback "service:plans"
Выход: back → mode_select
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from states.base import BaseState

MOSCOW_TZ = timezone(timedelta(hours=3))
from helpers.telegram_format import format_strategy_content
from helpers.message_split import truncate_safe
from i18n import t

logger = logging.getLogger(__name__)


class PlansState(BaseState):
    """
    Стейт планов — хаб с 3 кнопками.

    Проверяет GitHub → показывает меню → по клику загружает контент.
    """

    name = "common.plans"
    display_name = {
        "ru": "Планы",
        "en": "Plans",
        "es": "Planes",
        "fr": "Plans"
    }
    allow_global = []

    def _get_lang(self, user) -> str:
        if isinstance(user, dict):
            return user.get('language', 'ru')
        return getattr(user, 'language', 'ru') or 'ru'

    def _get_chat_id(self, user) -> int:
        if isinstance(user, dict):
            return user.get('chat_id')
        return getattr(user, 'chat_id', None)

    def _truncate(self, text: str) -> str:
        return truncate_safe(text)

    def _format_content(self, content: str, repo_url: str = None, branch: str = "main") -> str:
        text = format_strategy_content(content)
        text = self._truncate(text)
        if repo_url:
            text += f'\n\n<a href="{repo_url}/tree/{branch}/current">Открыть в GitHub</a>'
        return text

    async def _check_github(self, user) -> tuple[bool, Optional[str]]:
        """Проверяет GitHub подключение и strategy_repo.

        Returns:
            (ready, strategy_repo) — если ready=False, сообщение уже отправлено.
        """
        from clients.github_oauth import github_oauth

        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)

        if not await github_oauth.is_connected(chat_id):
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔗 GitHub",
                    callback_data="conn_github",
                )],
                [InlineKeyboardButton(
                    text=t('buttons.back', lang),
                    callback_data="plans_back",
                )],
            ])
            await self.send(user, t('plans.github_not_connected', lang), reply_markup=keyboard)
            return False, None

        strategy_repo = await github_oauth.get_strategy_repo(chat_id)
        if not strategy_repo:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="📁 " + t('plans.repo_not_selected', lang),
                    callback_data="strategy_select_repo",
                )],
                [InlineKeyboardButton(
                    text=t('buttons.back', lang),
                    callback_data="plans_back",
                )],
            ])
            await self.send(user, t('plans.repo_not_selected', lang), reply_markup=keyboard)
            return False, None

        return True, strategy_repo

    async def enter(self, user, context: dict = None) -> None:
        """Показываем хаб планов с 3 кнопками."""
        lang = self._get_lang(user)

        ready, _ = await self._check_github(user)
        if not ready:
            return

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t('plans.day_plan', lang),
                    callback_data="plans_day",
                ),
                InlineKeyboardButton(
                    text=t('plans.week_plan', lang),
                    callback_data="plans_rp",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t('plans.week_report', lang),
                    callback_data="plans_report",
                ),
                InlineKeyboardButton(
                    text=t('buttons.back', lang),
                    callback_data="plans_back",
                ),
            ],
        ])

        await self.send(user, t('plans.title', lang), reply_markup=keyboard)

    async def handle(self, user, message: Message) -> Optional[str]:
        """Текст в стейте → показываем хаб заново."""
        await self.enter(user)
        return None

    async def handle_callback(self, user, callback: CallbackQuery) -> Optional[str]:
        """Обрабатываем нажатия inline-кнопок."""
        data = callback.data
        await callback.answer()

        if data == "plans_back":
            return "back"

        if data == "plans_back_to_hub":
            await self.enter(user)
            return None

        if data in ("plans_day", "plans_rp", "plans_report"):
            return await self._show_content(user, callback, data)

        return None

    async def _show_content(self, user, callback: CallbackQuery, action: str) -> Optional[str]:
        """Загружает и показывает контент из GitHub."""
        from clients.github_strategy import github_strategy

        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)

        if action == "plans_day":
            content = await github_strategy.get_day_plan(chat_id)
            not_found_key = 'plans.not_found_day'
        elif action == "plans_rp":
            content = await github_strategy.get_week_plan(chat_id)
            not_found_key = 'plans.not_found_week'
        else:
            content = await github_strategy.get_week_report(chat_id)
            not_found_key = 'plans.not_found_report'

        # Кнопки: два других плана + назад
        all_actions = {
            "plans_day": ("plans.day_plan", "plans_day"),
            "plans_rp": ("plans.week_plan", "plans_rp"),
            "plans_report": ("plans.week_report", "plans_report"),
        }
        nav_buttons = []
        for key, (label_key, cb_data) in all_actions.items():
            if key != action:
                nav_buttons.append([InlineKeyboardButton(
                    text=t(label_key, lang), callback_data=cb_data,
                )])
        nav_buttons.append([InlineKeyboardButton(
            text=t('buttons.back', lang), callback_data="plans_back_to_hub",
        )])
        keyboard = InlineKeyboardMarkup(inline_keyboard=nav_buttons)

        if not content:
            # Monday fallback: DayPlan is not generated on Mondays, point to WeekPlan
            is_monday = datetime.now(MOSCOW_TZ).weekday() == 0
            if action == "plans_day" and is_monday:
                monday_buttons = [
                    [InlineKeyboardButton(
                        text=t('plans.week_plan', lang), callback_data="plans_rp",
                    )],
                    [InlineKeyboardButton(
                        text=t('plans.week_report', lang), callback_data="plans_report",
                    )],
                    [InlineKeyboardButton(
                        text=t('buttons.back', lang), callback_data="plans_back_to_hub",
                    )],
                ]
                await self.send(user, t('plans.not_found_day_monday', lang),
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=monday_buttons))
                return None
            await self.send(user, t(not_found_key, lang), reply_markup=keyboard)
            return None

        repo_url = await github_strategy.get_strategy_repo_url(chat_id)
        branch = await github_strategy.get_strategy_branch(chat_id)
        text = self._format_content(content, repo_url, branch)

        await self.send(user, text, parse_mode="HTML", reply_markup=keyboard)
        return None
