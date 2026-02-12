"""
Стейт: Статистика и прогресс (/progress).

Показывает прогресс пользователя по Марафону и Ленте.

Вход: по команде /progress
Выход: продолжить обучение (marathon/feed), настройки, или _previous
"""

import logging
from typing import Optional

from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from states.base import BaseState
from i18n import t
from config import MARATHON_DAYS, Mode

logger = logging.getLogger(__name__)


class ProgressState(BaseState):
    """
    Стейт статистики и прогресса.

    Показывает:
    - Короткий отчёт (enter): активность за неделю, день марафона, РП
    - Полный отчёт (handle_callback "progress_full"): статистика с начала, прогресс по дням
    """

    name = "utility.progress"
    display_name = {
        "ru": "Статистика",
        "en": "Progress",
        "es": "Progreso",
        "fr": "Progrès"
    }
    allow_global = ["consultation", "notes"]

    def _get_lang(self, user) -> str:
        """Получить язык пользователя."""
        if isinstance(user, dict):
            return user.get('language', 'ru')
        return getattr(user, 'language', 'ru') or 'ru'

    def _get_chat_id(self, user) -> int:
        """Получить chat_id пользователя."""
        if isinstance(user, dict):
            return user.get('chat_id')
        return getattr(user, 'chat_id', None)

    def _get_user_name(self, user) -> str:
        """Получить имя пользователя."""
        if isinstance(user, dict):
            return user.get('name', 'Пользователь')
        return getattr(user, 'name', 'Пользователь') or 'Пользователь'

    def _get_mode(self, user) -> str:
        """Получить режим пользователя."""
        if isinstance(user, dict):
            return user.get('mode', Mode.MARATHON)
        return getattr(user, 'mode', Mode.MARATHON) or Mode.MARATHON

    async def enter(self, user, context: dict = None) -> None:
        """Показываем короткий отчёт прогресса."""
        from db.queries.answers import get_weekly_marathon_stats, get_weekly_feed_stats
        from db.queries.activity import get_activity_stats

        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        name = self._get_user_name(user)
        mode = self._get_mode(user)

        # Получаем статистику
        try:
            activity_stats = await get_activity_stats(chat_id)
            marathon_stats = await get_weekly_marathon_stats(chat_id)
            feed_stats = await get_weekly_feed_stats(chat_id)
        except Exception as e:
            logger.error(f"Ошибка получения статистики для {chat_id}: {e}")
            activity_stats = {'days_active_this_week': 0}
            marathon_stats = {'work_products': 0}
            feed_stats = {'digests': 0, 'fixations': 0}

        # Данные для отчёта
        days_active_week = activity_stats.get('days_active_this_week', 0)
        total_wp_week = marathon_stats.get('work_products', 0)

        # Марафон: день и пройденные темы
        if isinstance(user, dict):
            completed_topics = user.get('completed_topics', [])
            marathon_started = user.get('marathon_started_at')
        else:
            completed_topics = getattr(user, 'completed_topics', []) or []
            marathon_started = getattr(user, 'marathon_started_at', None)

        done = len(completed_topics) if completed_topics else 0
        marathon_day = self._get_marathon_day(marathon_started)

        # Лента: темы
        try:
            from engines.feed.engine import FeedEngine
            feed_engine = FeedEngine(chat_id)
            feed_status = await feed_engine.get_status()
            feed_topics = feed_status.get('topics', [])
            feed_topics_text = ", ".join(feed_topics) if feed_topics else t('progress.topics_not_selected', lang)
        except Exception as e:
            logger.error(f"Ошибка получения статуса ленты для {chat_id}: {e}")
            feed_topics_text = "—"

        # Формируем текст
        text = f"{t('progress.title', lang, name=name)}\n\n"
        text += f"📈 {t('progress.active_days_week', lang)}: {days_active_week}\n\n"

        # Марафон
        text += f"🏃 *{t('progress.marathon', lang)}*\n"
        text += f"{t('progress.day', lang, day=marathon_day, total=MARATHON_DAYS)}\n"
        text += f"{t('progress.topics_completed', lang)} {done}. {t('progress.work_products', lang)}: {total_wp_week}\n\n"

        # Лента
        text += f"📚 *{t('progress.feed', lang)}*\n"
        text += f"{t('progress.digests', lang)}: {feed_stats.get('digests', 0)}. "
        text += f"{t('progress.fixations', lang)}: {feed_stats.get('fixations', 0)}\n"
        text += f"{t('progress.topics', lang)}: {feed_topics_text}"

        # Inline-кнопки
        buttons = []
        if mode == Mode.FEED:
            buttons.append([InlineKeyboardButton(text=f"📖 {t('buttons.get_digest', lang)}", callback_data="feed_get_digest")])
        else:
            buttons.append([InlineKeyboardButton(text=f"📚 {t('buttons.continue_learning', lang)}", callback_data="progress_continue")])

        buttons.append([
            InlineKeyboardButton(text=f"📊 {t('progress.full_report', lang)}", callback_data="progress_full"),
            InlineKeyboardButton(text=f"⚙️ {t('buttons.settings', lang)}", callback_data="progress_settings")
        ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")

    async def handle(self, user, message: Message) -> Optional[str]:
        """Обрабатываем текстовые сообщения."""
        return None

    async def handle_callback(self, user, callback: CallbackQuery) -> Optional[str]:
        """Обрабатываем inline-кнопки."""
        data = callback.data
        await callback.answer()

        if data == "progress_full":
            await self._show_full_progress(user)
            return "full_shown"

        if data == "progress_continue":
            mode = self._get_mode(user)
            if mode == Mode.FEED:
                return "continue_feed"
            return "continue_marathon"

        if data == "progress_settings":
            return "settings"

        if data == "progress_back":
            await self.enter(user)
            return "shown"

        return None

    async def _show_full_progress(self, user) -> None:
        """Показываем полный отчёт."""
        from db.queries.answers import get_total_stats, get_work_products_by_day

        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        name = self._get_user_name(user)

        # Получаем полную статистику
        try:
            total_stats = await get_total_stats(chat_id)
        except Exception as e:
            logger.error(f"Ошибка получения total_stats: {e}")
            total_stats = {}

        # Дата регистрации
        reg_date = total_stats.get('registered_at')
        if reg_date:
            date_str = reg_date.strftime('%d.%m.%Y')
        else:
            date_str = "—"

        days_since = total_stats.get('days_since_start', 1)
        total_active = total_stats.get('total_active_days', 0)

        # Марафон
        if isinstance(user, dict):
            completed_topics = user.get('completed_topics', [])
            marathon_started = user.get('marathon_started_at')
        else:
            completed_topics = getattr(user, 'completed_topics', []) or []
            marathon_started = getattr(user, 'marathon_started_at', None)

        marathon_day = self._get_marathon_day(marathon_started)

        # Прогресс по Урокам и Заданиям
        progress = self._get_lessons_tasks_progress(completed_topics)

        # Прогресс по дням
        try:
            from core.topics import TOPICS
            wp_by_day = await get_work_products_by_day(chat_id, TOPICS)
        except Exception as e:
            logger.error(f"Ошибка получения wp_by_day: {e}")
            wp_by_day = {}

        days_progress = self._get_days_progress(completed_topics, marathon_day)

        # Формируем текст по дням
        days_text = ""
        for d in days_progress:
            day_num = d['day']
            if day_num > marathon_day:
                break
            wp_count = wp_by_day.get(day_num, 0)

            if d['status'] == 'completed':
                emoji = "✅"
                wp_text = f" | {t('progress.wp_short', lang)}: {wp_count}" if wp_count > 0 else ""
            elif d['status'] == 'in_progress':
                emoji = "🔄"
                wp_text = f" | {t('progress.wp_short', lang)}: {wp_count}" if wp_count > 0 else ""
            elif d['status'] == 'available':
                emoji = "📍"
                wp_text = ""
            else:
                continue

            status_text = f"{d['completed']}/{d['total']}"
            days_text += f"   {emoji} {t('progress.day_text', lang, day=day_num)}: {status_text}{wp_text}\n"

        # Лента: темы
        try:
            from engines.feed.engine import FeedEngine
            feed_engine = FeedEngine(chat_id)
            feed_status = await feed_engine.get_status()
            feed_topics = feed_status.get('topics', [])
            feed_topics_text = ", ".join(feed_topics) if feed_topics else t('progress.topics_not_selected', lang)
        except Exception as e:
            logger.error(f"Ошибка получения feed_status: {e}")
            feed_topics_text = "—"

        # Формируем текст
        text = f"📊 *{t('progress.full_report_title', lang, date=date_str, name=name)}*\n\n"
        text += f"📈 *{t('progress.active_days_both', lang)}:* {total_active} / {days_since}\n\n"

        # Марафон
        text += f"🏃 *{t('progress.marathon', lang)}*\n"
        text += f"{t('progress.day', lang, day=marathon_day, total=MARATHON_DAYS)}\n"
        text += f"📖 {t('progress.lessons', lang)}: {progress['lessons']['completed']}/{progress['lessons']['total']}\n"
        text += f"📝 {t('progress.tasks', lang)}: {progress['tasks']['completed']}/{progress['tasks']['total']}\n"
        text += f"{t('progress.work_products', lang)}: {total_stats.get('total_work_products', 0)}\n"

        # По дням
        if days_text:
            text += f"\n📋 *{t('progress.by_days', lang)}:*\n{days_text}"

        # Отставание
        completed_days = sum(1 for d in days_progress if d['status'] == 'completed')
        lag = marathon_day - completed_days
        text += f"{t('progress.lag', lang)}: {lag} {t('progress.days', lang)}\n"

        # Лента
        text += f"\n📚 *{t('progress.feed', lang)}*\n"
        text += f"{t('progress.digests', lang)}: {total_stats.get('total_digests', 0)}\n"
        text += f"{t('progress.fixations', lang)}: {total_stats.get('total_fixations', 0)}\n"
        text += f"{t('progress.topics', lang)}: {feed_topics_text}"

        # Inline-кнопки
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"📚 {t('buttons.continue_learning', lang)}", callback_data="progress_continue")],
            [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="progress_back")]
        ])

        await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")

    def _get_marathon_day(self, marathon_started) -> int:
        """Вычислить текущий день марафона."""
        from datetime import datetime, timezone

        if not marathon_started:
            return 1

        now = datetime.now(timezone.utc)
        if marathon_started.tzinfo is None:
            marathon_started = marathon_started.replace(tzinfo=timezone.utc)

        days_passed = (now - marathon_started).days + 1
        return min(max(days_passed, 1), MARATHON_DAYS)

    def _get_lessons_tasks_progress(self, completed_topics: list) -> dict:
        """Получить прогресс по урокам и заданиям."""
        try:
            from core.topics import TOPICS
        except ImportError:
            return {
                'lessons': {'completed': 0, 'total': 0},
                'tasks': {'completed': 0, 'total': 0}
            }

        completed_set = set(completed_topics) if completed_topics else set()

        lessons_total = sum(1 for t in TOPICS if t.get('type') == 'theory')
        lessons_completed = sum(1 for i, t in enumerate(TOPICS)
                               if t.get('type') == 'theory' and i in completed_set)

        tasks_total = sum(1 for t in TOPICS if t.get('type') == 'practice')
        tasks_completed = sum(1 for i, t in enumerate(TOPICS)
                             if t.get('type') == 'practice' and i in completed_set)

        return {
            'lessons': {'completed': lessons_completed, 'total': lessons_total},
            'tasks': {'completed': tasks_completed, 'total': tasks_total}
        }

    def _get_days_progress(self, completed_topics: list, marathon_day: int) -> list:
        """Получить прогресс по дням марафона."""
        try:
            from core.topics import TOPICS
        except ImportError:
            return []

        days = []
        completed_set = set(completed_topics) if completed_topics else set()

        for day in range(1, MARATHON_DAYS + 1):
            day_topics = [(i, t) for i, t in enumerate(TOPICS) if t.get('day') == day]
            completed_count = sum(1 for i, _ in day_topics if i in completed_set)

            status = 'locked'
            if day <= marathon_day:
                if completed_count == len(day_topics):
                    status = 'completed'
                elif completed_count > 0:
                    status = 'in_progress'
                else:
                    status = 'available'

            days.append({
                'day': day,
                'total': len(day_topics),
                'completed': completed_count,
                'status': status
            })

        return days
