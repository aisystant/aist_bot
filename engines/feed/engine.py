"""
Основной движок режима Лента.

Управляет жизненным циклом:
1. Начало недели → предложение тем
2. Принятие тем пользователем
3. Ежедневные сессии
4. Фиксация (закрытие дня)
5. Завершение недели → статистика
"""

from datetime import datetime, date, timedelta
from typing import Optional, Dict, List, Tuple
import json

from config import (
    get_logger,
    Mode,
    FeedStatus,
    FeedWeekStatus,
    FEED_DAYS_PER_WEEK,
    FEED_SESSION_DURATION_MIN,
    FEED_SESSION_DURATION_MAX,
)
from db.queries.users import get_intern, update_intern
from db.queries.feed import (
    create_feed_week,
    get_current_feed_week,
    update_feed_week,
    create_feed_session,
    update_feed_session,
    get_feed_session,
)
from db.queries.activity import record_active_day, get_activity_stats

from .planner import suggest_weekly_topics, generate_topic_content

logger = get_logger(__name__)


class FeedEngine:
    """Движок режима Лента"""

    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self._intern = None
        self._current_week = None

    async def get_intern(self) -> dict:
        """Получает профиль пользователя"""
        if not self._intern:
            self._intern = await get_intern(self.chat_id)
        return self._intern

    async def get_current_week(self) -> Optional[dict]:
        """Получает текущую неделю"""
        if not self._current_week:
            self._current_week = await get_current_feed_week(self.chat_id)
        return self._current_week

    # ==================== СТАРТ И НАСТРОЙКА ====================

    async def start_feed(self) -> Tuple[bool, str]:
        """Запускает режим Лента для пользователя

        Returns:
            (success, message)
        """
        intern = await self.get_intern()
        if not intern:
            return False, "Профиль не найден. Пройдите /start для регистрации."

        # Обновляем режим
        await update_intern(self.chat_id, {
            'mode': Mode.FEED,
            'feed_status': FeedStatus.ACTIVE,
            'feed_started_at': datetime.utcnow(),
        })

        # Проверяем, есть ли активная неделя
        week = await self.get_current_week()
        if week and week['status'] == FeedWeekStatus.ACTIVE:
            return True, "Лента уже активна! Используйте /feed для продолжения."

        return True, "Режим Лента активирован! Сейчас предложу темы на неделю."

    async def suggest_topics(self) -> Tuple[List[Dict], str]:
        """Генерирует и возвращает предложения тем на неделю

        Returns:
            (topics, message)
        """
        intern = await self.get_intern()

        # Генерируем темы
        topics = await suggest_weekly_topics(intern)

        if not topics:
            return [], "Не удалось сгенерировать темы. Попробуйте позже."

        # Создаём неделю в статусе PLANNING
        await create_feed_week(
            chat_id=self.chat_id,
            suggested_topics=[t['title'] for t in topics],
            accepted_topics=[],
        )

        # Очищаем кеш
        self._current_week = None

        return topics, "Выберите темы для изучения на этой неделе:"

    async def accept_topics(self, accepted_titles: List[str]) -> Tuple[bool, str]:
        """Принимает выбранные пользователем темы

        Args:
            accepted_titles: список названий выбранных тем

        Returns:
            (success, message)
        """
        week = await self.get_current_week()
        if not week:
            return False, "Сначала запустите /feed для получения предложений."

        if week['status'] != FeedWeekStatus.PLANNING:
            return False, "Темы на эту неделю уже выбраны."

        # Обновляем неделю
        await update_feed_week(week['id'], {
            'accepted_topics': accepted_titles,
            'status': FeedWeekStatus.ACTIVE,
            'current_day': 1,
        })

        # Очищаем кеш
        self._current_week = None

        count = len(accepted_titles)
        return True, f"Отлично! Выбрано {count} тем. Начинаем!"

    # ==================== ЕЖЕДНЕВНЫЕ СЕССИИ ====================

    async def get_today_session(self) -> Tuple[Optional[Dict], str]:
        """Получает или создаёт сессию на сегодня

        Returns:
            (session_data, message)
        """
        week = await self.get_current_week()
        if not week:
            return None, "Нет активной недели. Используйте /feed для старта."

        if week['status'] != FeedWeekStatus.ACTIVE:
            if week['status'] == FeedWeekStatus.PLANNING:
                return None, "Сначала выберите темы на неделю."
            return None, "Неделя завершена. Используйте /feed для новой."

        # Проверяем, есть ли сессия на сегодня
        today = date.today()
        existing = await get_feed_session(week['id'], today)

        if existing:
            if existing['status'] == 'completed':
                return existing, "Сегодняшняя сессия уже завершена. До завтра!"
            return existing, "Продолжаем сессию..."

        # Создаём новую сессию
        intern = await self.get_intern()
        topics = week.get('accepted_topics', [])
        current_day = week.get('current_day', 1)

        if current_day > len(topics):
            # Все темы пройдены
            await self._complete_week()
            return None, "Поздравляем! Все темы недели пройдены."

        # Выбираем тему дня
        topic_title = topics[current_day - 1] if topics else "Системное мышление"

        topic = {
            'title': topic_title,
            'description': '',
            'keywords': topic_title.split()[:3],
        }

        # Генерируем контент
        duration = (FEED_SESSION_DURATION_MIN + FEED_SESSION_DURATION_MAX) // 2
        content = await generate_topic_content(topic, intern, duration)

        # Создаём сессию
        session = await create_feed_session(
            week_id=week['id'],
            day_number=current_day,
            topic_title=topic_title,
            content=content,
            session_date=today,
        )

        return session, content.get('intro', 'Начинаем сессию!')

    async def get_session_content(self, session_id: int) -> Optional[Dict]:
        """Получает контент сессии по ID"""
        # Получаем сессию из БД
        week = await self.get_current_week()
        if not week:
            return None

        session = await get_feed_session(week['id'], date.today())
        if session and session['id'] == session_id:
            return session.get('content', {})

        return None

    async def submit_fixation(self, text: str) -> Tuple[bool, str]:
        """Принимает фиксацию (закрытие дня текстом)

        Args:
            text: текст фиксации от пользователя

        Returns:
            (success, message)
        """
        week = await self.get_current_week()
        if not week:
            return False, "Нет активной недели."

        today = date.today()
        session = await get_feed_session(week['id'], today)

        if not session:
            return False, "Сначала начните сессию на сегодня."

        if session['status'] == 'completed':
            return False, "Сегодняшняя сессия уже завершена."

        # Сохраняем фиксацию
        await update_feed_session(session['id'], {
            'fixation_text': text,
            'status': 'completed',
            'completed_at': datetime.utcnow(),
        })

        # Записываем активность
        await record_active_day(
            chat_id=self.chat_id,
            activity_type='feed_fixation',
            mode='feed',
            reference_id=session['id'],
        )

        # Увеличиваем день недели
        new_day = week.get('current_day', 1) + 1
        await update_feed_week(week['id'], {'current_day': new_day})

        # Очищаем кеш
        self._current_week = None

        # Проверяем, завершена ли неделя
        if new_day > len(week.get('accepted_topics', [])):
            await self._complete_week()
            return True, "Отлично! Неделя завершена. Используйте /feed для новых тем."

        return True, "Фиксация сохранена! До завтра."

    # ==================== ЗАВЕРШЕНИЕ НЕДЕЛИ ====================

    async def _complete_week(self):
        """Завершает текущую неделю"""
        week = await self.get_current_week()
        if not week:
            return

        await update_feed_week(week['id'], {
            'status': FeedWeekStatus.COMPLETED,
            'ended_at': datetime.utcnow(),
        })

        # Очищаем кеш
        self._current_week = None

    async def get_week_summary(self) -> Dict:
        """Возвращает статистику недели"""
        week = await self.get_current_week()
        if not week:
            return {'error': 'Нет активной недели'}

        stats = await get_activity_stats(self.chat_id)

        return {
            'week_number': week.get('week_number', 1),
            'topics_count': len(week.get('accepted_topics', [])),
            'current_day': week.get('current_day', 1),
            'status': week.get('status'),
            'total_active_days': stats.get('total', 0),
            'current_streak': stats.get('streak', 0),
        }

    # ==================== СТАТУС И ИНФОРМАЦИЯ ====================

    async def get_status(self) -> Dict:
        """Возвращает текущий статус Ленты"""
        intern = await self.get_intern()
        week = await self.get_current_week()
        stats = await get_activity_stats(self.chat_id)

        return {
            'feed_active': intern.get('feed_status') == FeedStatus.ACTIVE,
            'has_week': week is not None,
            'week_status': week.get('status') if week else None,
            'current_day': week.get('current_day', 0) if week else 0,
            'topics': week.get('accepted_topics', []) if week else [],
            'active_days': stats.get('total', 0),
            'streak': stats.get('streak', 0),
        }
