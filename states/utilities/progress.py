from __future__ import annotations

"""
Стейт: Статистика и прогресс (/progress).

Хаб с обзорной карточкой + 6 секций (inline-кнопки, edit_text).
Prefetch: enter() загружает ВСЕ данные одним батчем → current_context.
Секции рендерят из кеша — 0 DB-запросов при переключении.

Вход: по команде /progress
Выход: продолжить обучение (marathon/feed), настройки, или _previous
"""

import asyncio
import json
import logging
from datetime import timedelta
from typing import Optional

from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from states.base import BaseState
from i18n import t
from config import MARATHON_DAYS, Mode

logger = logging.getLogger(__name__)


class ProgressState(BaseState):
    """
    Стейт статистики и прогресса.

    enter() загружает все данные в current_context['progress_cache'].
    Секции рендерят из кеша — мгновенное переключение.
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
        if isinstance(user, dict):
            return user.get('language', 'ru')
        return getattr(user, 'language', 'ru') or 'ru'

    def _get_chat_id(self, user) -> int:
        if isinstance(user, dict):
            return user.get('chat_id')
        return getattr(user, 'chat_id', None)

    def _get_user_name(self, user) -> str:
        if isinstance(user, dict):
            return user.get('name', '')
        return getattr(user, 'name', '') or ''

    def _get_mode(self, user) -> str:
        if isinstance(user, dict):
            return user.get('mode', Mode.MARATHON)
        return getattr(user, 'mode', Mode.MARATHON) or Mode.MARATHON

    # ─── PREFETCH ─────────────────────────────────────────────

    async def _fetch_points(self, account_id: Optional[str]):
        """Баланс баллов через rewards.point_balances. Read-only.

        account_id = intern['dt_user_id'] (Ory UUID, хранится в public.users).
        Возвращает Decimal или None (нет dt_user_id / нет начислений / ошибка).
        """
        if not account_id:
            return None
        try:
            from db.queries.rewards import get_points_balance
            return await get_points_balance(account_id)
        except Exception as e:
            logger.error(f"[Progress] _fetch_points error: {e}")
            return None

    async def _prefetch(self, chat_id: int) -> dict:
        """Загрузить ВСЕ данные одним батчем (asyncio.gather)."""
        from db.queries import get_intern
        from db.queries.answers import (
            get_weekly_marathon_stats, get_weekly_feed_stats,
            get_total_stats, get_work_products_by_day,
            get_theory_count_at_level,
        )
        from db.queries.activity import get_activity_stats, get_activity_calendar
        from db.queries.qa import get_user_qa_stats
        from db.queries.github import get_github_connection
        from core.topics import get_marathon_day, get_display_day, TOPICS

        intern = await get_intern(chat_id)
        if not intern:
            return {}

        (
            activity_stats,
            calendar,
            marathon_week,
            feed_week,
            total_stats,
            qa_stats,
            github,
            points_balance,
        ) = await asyncio.gather(
            get_activity_stats(chat_id),
            get_activity_calendar(chat_id, weeks=4),
            get_weekly_marathon_stats(chat_id),
            get_weekly_feed_stats(chat_id),
            get_total_stats(chat_id),
            get_user_qa_stats(chat_id),
            get_github_connection(chat_id),
            self._fetch_points(intern.get('dt_user_id')),
            return_exceptions=True,
        )

        # Безопасная обработка ошибок
        if isinstance(activity_stats, Exception):
            logger.error(f"[Progress] activity_stats error: {activity_stats}")
            activity_stats = {'total': 0, 'streak': 0, 'longest_streak': 0, 'days_active_this_week': 0}
        if isinstance(calendar, Exception):
            logger.error(f"[Progress] calendar error: {calendar}")
            calendar = []
        if isinstance(marathon_week, Exception):
            logger.error(f"[Progress] marathon_week error: {marathon_week}")
            marathon_week = {'work_products': 0}
        if isinstance(feed_week, Exception):
            logger.error(f"[Progress] feed_week error: {feed_week}")
            feed_week = {'digests': 0, 'fixations': 0}
        if isinstance(total_stats, Exception):
            logger.error(f"[Progress] total_stats error: {total_stats}")
            total_stats = {}
        if isinstance(qa_stats, Exception):
            logger.error(f"[Progress] qa_stats error: {qa_stats}")
            qa_stats = {'total': 0, 'helpful': 0, 'not_helpful': 0, 'this_week': 0, 'top_topics': []}
        if isinstance(github, Exception):
            logger.error(f"[Progress] github error: {github}")
            github = None
        if isinstance(points_balance, Exception):
            logger.error(f"[Progress] points_balance error: {points_balance}")
            points_balance = None

        # Марафон
        completed_topics = intern.get('completed_topics', [])
        if isinstance(completed_topics, str):
            try:
                completed_topics = json.loads(completed_topics)
            except Exception:
                completed_topics = []

        marathon_day = get_marathon_day(intern)
        display_day = get_display_day(intern)
        days_progress = self._get_days_progress(completed_topics, marathon_day)
        lessons_tasks = self._get_lessons_tasks_progress(completed_topics)

        try:
            wp_by_day = await get_work_products_by_day(chat_id, TOPICS)
        except Exception:
            wp_by_day = {}

        # Лента: темы
        feed_topics = []
        try:
            from engines.feed.engine import FeedEngine
            feed_engine = FeedEngine(chat_id)
            feed_status = await feed_engine.get_status()
            feed_topics = feed_status.get('topics', [])
        except Exception:
            pass

        # Feed weeks count
        feed_weeks_count = 0
        try:
            from db.connection import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT COUNT(DISTINCT fw.id) AS cnt
                       FROM feed_weeks fw
                       WHERE fw.chat_id = $1
                         AND EXISTS (SELECT 1 FROM feed_sessions fs WHERE fs.week_id = fw.id)""",
                    chat_id,
                )
                feed_weeks_count = row['cnt'] if row else 0
        except Exception:
            pass

        # Assessment
        last_assessment = None
        try:
            from db.connection import get_learning_pool
            pool = await get_learning_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    'SELECT scores, dominant_state, created_at FROM assessments WHERE chat_id = $1 ORDER BY created_at DESC LIMIT 1',
                    chat_id,
                )
                if row:
                    last_assessment = {
                        'scores': row['scores'],
                        'dominant_state': row['dominant_state'],
                        'created_at': row['created_at'].strftime('%d.%m.%Y') if row['created_at'] else None,
                    }
        except Exception:
            pass

        # CP-профиль (WP-318 Ф10)
        cp_profile = None
        try:
            account_id = intern.get('dt_user_id')
            if account_id:
                from db.queries.cp_assessment import get_latest_cp_assessment
                cp_profile = await get_latest_cp_assessment(account_id)
        except Exception as e:
            logger.error(f"[Progress] cp_profile error: {e}")

        assessment_date = intern.get('assessment_date')
        if assessment_date and hasattr(assessment_date, 'isoformat'):
            assessment_date = assessment_date.isoformat()

        # Registered date
        reg_date = total_stats.get('registered_at')
        reg_date_str = reg_date.strftime('%d.%m.%Y') if reg_date and hasattr(reg_date, 'strftime') else '—'

        # Calendar → serializable + most active weekday
        cal_data = []
        weekday_counts = {}
        for day in calendar:
            cal_data.append({
                'date': day['date'].isoformat() if hasattr(day['date'], 'isoformat') else str(day['date']),
                'weekday': day['weekday'],
                'active': day['active'],
            })
            if day['active']:
                wd = day['weekday']
                weekday_counts[wd] = weekday_counts.get(wd, 0) + 1
        most_active_wd = max(weekday_counts, key=weekday_counts.get) if weekday_counts else None

        # SOTA.012: вычисляем из answers (Event Sourcing), не из мутируемого счётчика
        complexity_level = intern.get('complexity_level', 1)
        topics_at_level = await get_theory_count_at_level(chat_id, complexity_level)

        return {
            'name': self._get_user_name(intern),
            'reg_date': reg_date_str,
            'complexity_level': complexity_level,
            'topics_at_current_complexity': topics_at_level,
            # Activity
            'streak': activity_stats.get('streak', 0),
            'longest_streak': activity_stats.get('longest_streak', 0),
            'active_days_total': activity_stats.get('total', 0),
            'days_active_week': activity_stats.get('days_active_this_week', 0),
            # Rewards (WP-253 Ф9.3 проекция)
            'points_balance': points_balance,
            # Calendar
            'calendar': cal_data,
            'most_active_wd': most_active_wd,
            'calendar_active_count': sum(1 for d in calendar if d['active']),
            'calendar_total_days': len(calendar),
            # Marathon
            'marathon_day': marathon_day,
            'display_day': display_day,
            'marathon_total': MARATHON_DAYS,
            'done_count': len(completed_topics) if completed_topics else 0,
            'lessons': lessons_tasks['lessons'],
            'tasks': lessons_tasks['tasks'],
            'wp_total': total_stats.get('total_work_products', 0),
            'wp_week': marathon_week.get('work_products', 0),
            'days_progress': days_progress,
            'wp_by_day': {str(k): v for k, v in wp_by_day.items()},
            'lag': marathon_day - sum(1 for d in days_progress if d['status'] == 'completed'),
            # Feed
            'feed_topics': feed_topics,
            'feed_digests_total': total_stats.get('total_digests', 0),
            'feed_fixations_total': total_stats.get('total_fixations', 0),
            'feed_digests_week': feed_week.get('digests', 0),
            'feed_fixations_week': feed_week.get('fixations', 0),
            'feed_weeks_count': feed_weeks_count,
            # QA
            'qa': qa_stats,
            # Assessment
            'assessment_state': intern.get('assessment_state', '') or '',
            'assessment_date': assessment_date,
            'last_assessment': last_assessment,
            # CP-профиль (WP-318 Ф10)
            'cp_profile': cp_profile,
            # GitHub
            'github': {
                'connected': github is not None,
                'username': github.get('github_username', '') if github else '',
                'repo': github.get('target_repo', '') if github else '',
                'notes_path': github.get('notes_path', '') if github else '',
            },
        }

    # ─── ENTER ────────────────────────────────────────────────

    async def enter(self, user, context: dict = None) -> None:
        """Обзорная карточка + навигационные кнопки."""
        from db.queries.users import update_intern
        from db.queries.marathon_newcomer import has_active_marathon_progress

        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        # WP-330 B3: guard — если пользователь в активном WP-330 марафоне,
        # SM ProgressState показывает нулевую legacy-статистику. Редиректим.
        # Универсальная проверка: и dict, и object.
        marathon_status = user.get('marathon_status') if isinstance(user, dict) else getattr(user, 'marathon_status', None)
        if marathon_status == 'active':
            if await has_active_marathon_progress(chat_id):
                await self.send(
                    user,
                    "🏃 У тебя активный марафон «Первые шаги в IWE».\n\n"
                    "📊 Статистика марафона: /marathon_progress"
                )
                return

        try:
            cache = await self._prefetch(chat_id)
        except Exception as e:
            logger.error(f"[Progress] Prefetch error: {e}")
            import traceback
            logger.error(traceback.format_exc())
            await self.send(user, t('progress.full_report_error', lang))
            return

        # Сохраняем кеш в current_context (мержим, не перезаписываем)
        try:
            existing_ctx = {}
            ctx_raw = user.get('current_context', '{}') if isinstance(user, dict) else getattr(user, 'current_context', '{}')
            if isinstance(ctx_raw, str):
                try:
                    existing_ctx = json.loads(ctx_raw) or {}
                except Exception:
                    existing_ctx = {}
            elif isinstance(ctx_raw, dict):
                existing_ctx = ctx_raw
            existing_ctx['progress_cache'] = cache
            await update_intern(chat_id, current_context=json.dumps(
                existing_ctx, ensure_ascii=False, default=str
            ))
        except Exception as e:
            logger.error(f"[Progress] Cache save error: {e}")

        await self._render_overview(user, cache, lang)

    async def _render_overview(self, user, cache: dict, lang: str, callback: CallbackQuery = None) -> None:
        """Рендер обзорной карточки."""
        name = cache.get('name', '')
        streak = cache.get('streak', 0)
        longest = cache.get('longest_streak', 0)
        total_active = cache.get('active_days_total', 0)
        week_active = cache.get('days_active_week', 0)
        complexity = cache.get('complexity_level', 1)
        reg_date = cache.get('reg_date', '—')

        points = cache.get('points_balance')

        text = f"<b>{t('progress.title_hub', lang, name=name)}</b>\n\n"
        text += f"🔥 {t('progress.streak_line', lang)}: {streak} {t('progress.days', lang)} | {t('progress.record', lang)}: {longest} {t('progress.days', lang)}\n"
        text += f"📅 {t('progress.activity_line', lang)}: {total_active} {t('progress.total_word', lang)} | {week_active}/7 {t('progress.this_week', lang)}\n"
        text += f"🎯 {t('progress.complexity_line', lang)}: {complexity}\n"
        if points is not None:
            text += f"🏆 Бонусы: {int(points)}\n"
        text += f"📆 {t('progress.since', lang)}: {reg_date}"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=f"📅 {t('progress.sec_calendar', lang)}", callback_data="progress_calendar"),
                InlineKeyboardButton(text=f"🏃 {t('progress.sec_marathon', lang)}", callback_data="progress_marathon"),
            ],
            [
                InlineKeyboardButton(text=f"📚 {t('progress.sec_feed', lang)}", callback_data="progress_feed"),
                InlineKeyboardButton(text=f"❓ {t('progress.sec_qa', lang)}", callback_data="progress_qa"),
            ],
            [
                InlineKeyboardButton(text=f"🧪 {t('progress.sec_assessment', lang)}", callback_data="progress_assessment"),
                InlineKeyboardButton(text=f"🔗 {t('progress.sec_integrations', lang)}", callback_data="progress_integrations"),
            ],
            [InlineKeyboardButton(text="🔬 Диагностический профиль", callback_data="progress_cp_profile")],
            [InlineKeyboardButton(text=t('buttons.back', lang), callback_data="progress_exit")],
        ])
        await self._show_section(user, text, keyboard, callback)

    # ─── DISPLAY HELPER ────────────────────────────────────────

    async def _show_section(self, user, text: str, reply_markup, callback: CallbackQuery = None) -> None:
        """Edit existing message (callback) or send new (enter)."""
        if callback and callback.message:
            try:
                await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
                return
            except Exception as e:
                logger.debug(f"[Progress] edit_text failed, sending new: {e}")
        await self.send(user, text, reply_markup=reply_markup, parse_mode="HTML")

    # ─── SECTIONS ─────────────────────────────────────────────

    def _back_button(self, lang: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"« {t('progress.back_to_overview', lang)}", callback_data="progress_back")]
        ])

    async def _show_calendar(self, user, cache: dict, lang: str, callback: CallbackQuery = None) -> None:
        cal = cache.get('calendar', [])
        active_count = cache.get('calendar_active_count', 0)
        total_days = cache.get('calendar_total_days', 0)
        most_active_wd = cache.get('most_active_wd')

        wd_names = {
            'ru': ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'],
            'en': ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su'],
        }
        names = wd_names.get(lang, wd_names['en'])

        text = f"<b>📅 {t('progress.calendar_title', lang)}</b>\n\n"

        # Группировка по неделям
        from datetime import date as dt_date
        weeks = {}
        for day in cal:
            d = dt_date.fromisoformat(day['date'])
            week_start = d - timedelta(days=d.weekday())
            if week_start not in weeks:
                weeks[week_start] = ['⬜'] * 7
            weeks[week_start][d.weekday()] = '🟩' if day['active'] else '⬜'

        text += f"<code>      {' '.join(names)}</code>\n"
        for week_start in sorted(weeks.keys()):
            label = week_start.strftime('%d.%m')
            row = ' '.join(weeks[week_start])
            text += f"<code>{label}</code> {row}\n"

        text += f"\n{t('progress.active_of_total', lang)}: {active_count} / {total_days}"

        if most_active_wd is not None:
            text += f"\n{t('progress.most_active_day', lang)}: {names[most_active_wd]}"

        await self._show_section(user, text, self._back_button(lang), callback)

    async def _show_marathon(self, user, cache: dict, lang: str, callback: CallbackQuery = None) -> None:
        day = cache.get('display_day', cache.get('marathon_day', 1))
        total = cache.get('marathon_total', MARATHON_DAYS)
        lessons = cache.get('lessons', {'completed': 0, 'total': 0})
        tasks = cache.get('tasks', {'completed': 0, 'total': 0})
        wp_total = cache.get('wp_total', 0)
        wp_week = cache.get('wp_week', 0)
        complexity = cache.get('complexity_level', 1)
        topics_at = cache.get('topics_at_current_complexity', 0)
        lag = cache.get('lag', 0)
        days_progress = cache.get('days_progress', [])
        wp_by_day = cache.get('wp_by_day', {})

        text = f"<b>🏃 {t('progress.marathon_title', lang)}</b>\n\n"
        text += f"📈 {t('progress.day', lang, day=day, total=total)}\n"
        text += f"📖 {t('progress.lessons', lang)}: {lessons['completed']}/{lessons['total']}\n"
        text += f"📝 {t('progress.tasks', lang)}: {tasks['completed']}/{tasks['total']}\n"
        text += f"📦 {t('progress.work_products', lang)}: {wp_total} {t('progress.total_word', lang)} ({wp_week} {t('progress.this_week', lang)})\n"
        text += f"🎯 {t('progress.complexity_line', lang)}: {complexity} ({topics_at} {t('progress.topics_at_level', lang)})\n"
        text += f"⏱ {t('progress.lag', lang)}: {lag} {t('progress.days', lang)}\n"

        if days_progress:
            text += f"\n📋 <b>{t('progress.by_days', lang)}:</b>\n"
            for d in days_progress:
                day_num = d['day']
                if day_num > day:
                    break
                wp_count = wp_by_day.get(str(day_num), 0)
                if d['status'] == 'completed':
                    emoji = "✅"
                elif d['status'] == 'in_progress':
                    emoji = "🔄"
                elif d['status'] == 'available':
                    emoji = "📍"
                else:
                    continue
                status_text = f"{d['completed']}/{d['total']}"
                wp_text = f" | {t('progress.wp_short', lang)}: {wp_count}" if wp_count > 0 else ""
                text += f"   {emoji} {t('progress.day_text', lang, day=day_num)}: {status_text}{wp_text}\n"

        await self._show_section(user, text, self._back_button(lang), callback)

    async def _show_feed(self, user, cache: dict, lang: str, callback: CallbackQuery = None) -> None:
        digests_total = cache.get('feed_digests_total', 0)
        fixations_total = cache.get('feed_fixations_total', 0)
        digests_week = cache.get('feed_digests_week', 0)
        fixations_week = cache.get('feed_fixations_week', 0)
        weeks_count = cache.get('feed_weeks_count', 0)
        topics = cache.get('feed_topics', [])

        topics_text = ", ".join(topics) if topics else t('progress.topics_not_selected', lang)

        text = f"<b>📚 {t('progress.feed_title', lang)}</b>\n\n"
        text += f"📖 {t('progress.digests', lang)}: {digests_total} {t('progress.total_word', lang)} ({digests_week} {t('progress.this_week', lang)})\n"
        text += f"✍️ {t('progress.fixations', lang)}: {fixations_total} {t('progress.total_word', lang)} ({fixations_week} {t('progress.this_week', lang)})\n"
        text += f"📅 {t('progress.weeks_completed', lang)}: {weeks_count}\n"
        text += f"🎯 {t('progress.topics', lang)}: {topics_text}"

        await self._show_section(user, text, self._back_button(lang), callback)

    async def _show_qa(self, user, cache: dict, lang: str, callback: CallbackQuery = None) -> None:
        qa = cache.get('qa', {})
        total = qa.get('total', 0)
        helpful = qa.get('helpful', 0)
        not_helpful = qa.get('not_helpful', 0)
        this_week = qa.get('this_week', 0)
        top_topics = qa.get('top_topics', [])

        rated = helpful + not_helpful
        rate_text = f"{helpful}/{rated} ({round(helpful * 100 / rated)}%)" if rated > 0 else "—"

        text = f"<b>❓ {t('progress.qa_title', lang)}</b>\n\n"
        text += f"📊 {t('progress.qa_total', lang)}: {total}\n"
        text += f"📅 {t('progress.this_week', lang)}: {this_week}\n"
        text += f"👍 {t('progress.qa_helpful', lang)}: {rate_text}\n"

        if top_topics:
            text += f"\n{t('progress.qa_top_topics', lang)}:\n"
            for topic in top_topics:
                text += f"• {topic['topic']} ({topic['cnt']})\n"

        await self._show_section(user, text, self._back_button(lang), callback)

    # Маппинг assessment state → emoji + i18n (из systematicity.yaml)
    _ASSESSMENT_LABELS = {
        'chaos': {'emoji': '😵', 'ru': 'Хаос', 'en': 'Chaos', 'es': 'Caos', 'fr': 'Chaos', 'zh': '混乱'},
        'deadlock': {'emoji': '🧱', 'ru': 'Тупик', 'en': 'Deadlock', 'es': 'Estancamiento', 'fr': 'Impasse', 'zh': '僵局'},
        'turning_point': {'emoji': '🔁', 'ru': 'Поворот', 'en': 'Turning Point', 'es': 'Punto de giro', 'fr': 'Tournant', 'zh': '转折点'},
    }

    def _translate_state(self, key: str, lang: str) -> str:
        info = self._ASSESSMENT_LABELS.get(key)
        if info:
            return f"{info['emoji']} {info.get(lang, info.get('en', key))}"
        return key

    async def _show_assessment(self, user, cache: dict, lang: str, callback: CallbackQuery = None) -> None:
        last = cache.get('last_assessment')

        text = f"<b>🧪 {t('progress.assessment_title', lang)}</b>\n\n"

        if last:
            dominant = last.get('dominant_state', '—')
            dominant_label = self._translate_state(dominant, lang)

            text += f"📅 {t('progress.last_assessment', lang)}: {last.get('created_at', '—')}\n"
            text += f"🏷 {t('progress.assessment_result', lang)}: {dominant_label}\n"

            scores_raw = last.get('scores', '{}')
            if isinstance(scores_raw, str):
                try:
                    scores = json.loads(scores_raw)
                except Exception:
                    scores = {}
            else:
                scores = scores_raw or {}

            if scores:
                text += f"\n📊 {t('progress.scores', lang)}:\n"
                for key, value in scores.items():
                    label = self._translate_state(key, lang)
                    text += f"• {label}: {value}\n"
        else:
            text += t('progress.no_assessment', lang)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🧪 {t('progress.take_test', lang)}", callback_data="progress_go_assessment")],
            [InlineKeyboardButton(text=f"« {t('progress.back_to_overview', lang)}", callback_data="progress_back")],
        ])
        await self._show_section(user, text, keyboard, callback)

    _CP_STAGE_LABELS = {
        1: "Случайный",
        2: "Практикующий",
        3: "Систематический",
        4: "Дисциплинированный",
        5: "Проактивный",
    }

    _CP_SLOT_LABELS = {
        "cp.rhy": "Ритм",
        "cp.wld": "Мировоззрение",
        "cp.skl": "Навыки",
        "cp.iwe": "IWE",
        "cp.int": "Интеграция",
        "cp.agt": "Агентность",
    }

    async def _show_cp_profile(self, user, cache: dict, lang: str, callback: CallbackQuery = None) -> None:
        cp = cache.get('cp_profile')
        text = "<b>🔬 Диагностический профиль</b>\n\n"

        if cp:
            stage = cp.get('stage', '—')
            stage_label = self._CP_STAGE_LABELS.get(stage, str(stage))
            _btn_raw = cp.get('bottleneck_slot')
            bottleneck = '' if not _btn_raw or _btn_raw == 'none' else _btn_raw  # WP-370: None/'none' при stage≥4
            stream = cp.get('recommended_stream', '—')
            valid_until = cp.get('valid_until') or ''
            valid_str = valid_until[:10] if isinstance(valid_until, str) and valid_until else '—'

            text += f"🎯 Ступень: <b>{stage} — {stage_label}</b>\n"
            if bottleneck:
                bottleneck_label = self._CP_SLOT_LABELS.get(bottleneck, bottleneck)
                text += f"⚡ Узкое место: <b>{bottleneck_label}</b>\n"
            else:
                text += "⚡ Узких мест нет\n"
            text += f"📚 Поток: <b>{stream}</b>\n"
            text += f"📅 Действует до: {valid_str}\n"

            scores = cp.get('cp_scores') or {}
            if scores:
                text += "\n<b>Профиль по слотам:</b>\n"
                for slot, score in scores.items():
                    label = self._CP_SLOT_LABELS.get(slot, slot)
                    filled = int(score)
                    bar = "●" * filled + "○" * (5 - filled)
                    text += f"• {label}: {bar} {score}\n"
        else:
            text += "Диагностика ещё не пройдена.\n\nПройдите /diagnose — займёт около 3 минут."

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔬 Пройти диагностику", callback_data="progress_go_diagnose")],
            [InlineKeyboardButton(text=f"« {t('progress.back_to_overview', lang)}", callback_data="progress_back")],
        ])
        await self._show_section(user, text, keyboard, callback)

    async def _show_integrations(self, user, cache: dict, lang: str, callback: CallbackQuery = None) -> None:
        gh = cache.get('github', {})

        text = f"<b>🔗 {t('progress.integrations_title', lang)}</b>\n\n"

        if gh.get('connected'):
            text += f"🐙 GitHub: ✅ {gh.get('username', '')}\n"
            if gh.get('repo'):
                text += f"   📂 {t('progress.repo', lang)}: {gh['repo']}\n"
            if gh.get('notes_path'):
                text += f"   📝 {t('progress.notes_path', lang)}: {gh['notes_path']}\n"
        else:
            text += f"🐙 GitHub: ❌ {t('progress.not_connected', lang)}\n"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"⚙️ {t('buttons.settings', lang)}", callback_data="progress_settings")],
            [InlineKeyboardButton(text=f"« {t('progress.back_to_overview', lang)}", callback_data="progress_back")],
        ])
        await self._show_section(user, text, keyboard, callback)

    # ─── HANDLERS ─────────────────────────────────────────────

    async def handle(self, user, message: Message) -> Optional[str]:
        return None

    async def handle_callback(self, user, callback: CallbackQuery) -> Optional[str]:
        data = callback.data
        await callback.answer()

        lang = self._get_lang(user)
        cache = self._load_cache(user)

        if not cache:
            await self.enter(user)
            return "shown"

        section_map = {
            "progress_back": "_render_overview",
            "progress_calendar": "_show_calendar",
            "progress_marathon": "_show_marathon",
            "progress_feed": "_show_feed",
            "progress_qa": "_show_qa",
            "progress_assessment": "_show_assessment",
            "progress_integrations": "_show_integrations",
            "progress_cp_profile": "_show_cp_profile",
            "progress_full": "_show_marathon",  # legacy
        }

        if data in section_map:
            method = getattr(self, section_map[data])
            await method(user, cache, lang, callback=callback)
            return "section_shown" if data != "progress_back" else "shown"

        if data == "progress_go_assessment":
            return "go_assessment"
        if data == "progress_go_diagnose":
            await callback.answer("Используйте /diagnose (~3 мин)", show_alert=True)
            return None
        if data == "progress_settings":
            return "settings"
        if data == "progress_exit":
            return "back"
        if data == "progress_continue":
            mode = self._get_mode(user)
            return "continue_feed" if mode == Mode.FEED else "continue_marathon"

        return None

    def _load_cache(self, user) -> dict:
        if isinstance(user, dict):
            ctx_raw = user.get('current_context', '{}')
        else:
            ctx_raw = getattr(user, 'current_context', '{}')

        if isinstance(ctx_raw, str):
            try:
                ctx = json.loads(ctx_raw)
            except Exception:
                return {}
        else:
            ctx = ctx_raw or {}

        return ctx.get('progress_cache', {})

    # ─── HELPERS ──────────────────────────────────────────────

    def _get_lessons_tasks_progress(self, completed_topics: list) -> dict:
        try:
            from core.topics import TOPICS
        except ImportError:
            return {'lessons': {'completed': 0, 'total': 0}, 'tasks': {'completed': 0, 'total': 0}}

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
            days.append({'day': day, 'total': len(day_topics), 'completed': completed_count, 'status': status})
        return days
