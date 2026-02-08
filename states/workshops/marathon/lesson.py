"""
Стейт: Урок Марафона.

Вход: из common.mode_select (выбор "Марафон") или после завершения задания
Выход: workshop.marathon.question (после показа урока)
"""

import asyncio
from typing import Optional

from aiogram.types import Message

from states.base import BaseState
from i18n import t
from db.queries import get_intern, update_intern
from db.queries.users import moscow_today, get_topics_today
from core.knowledge import get_topic, get_topic_title, get_total_topics
from clients import claude, mcp_guides, mcp_knowledge
from config import get_logger, MARATHON_DAYS, MAX_TOPICS_PER_DAY

logger = get_logger(__name__)

# Таймаут на генерацию контента (секунды)
CONTENT_GENERATION_TIMEOUT = 90


class MarathonLessonState(BaseState):
    """
    Стейт показа урока Марафона.

    Показывает теоретический материал, сгенерированный через Claude API,
    и переходит к вопросу.
    """

    name = "workshop.marathon.lesson"
    display_name = {"ru": "Урок Марафона", "en": "Marathon Lesson", "es": "Lección del Maratón", "fr": "Leçon du Marathon"}
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

    def _get_marathon_day(self, user) -> int:
        """Получить текущий день марафона (по прогрессу)."""
        if isinstance(user, dict):
            completed = user.get('completed_topics', [])
        else:
            completed = getattr(user, 'completed_topics', [])
        return len(completed) // 2 + 1

    def _get_calendar_marathon_day(self, user) -> int:
        """Получить день марафона по календарю (от marathon_start_date)."""
        if isinstance(user, dict):
            start_date = user.get('marathon_start_date')
        else:
            start_date = getattr(user, 'marathon_start_date', None)

        if not start_date:
            # Нет даты старта — fallback на прогресс
            return self._get_marathon_day(user)

        from datetime import datetime
        today = moscow_today()
        if isinstance(start_date, datetime):
            start_date = start_date.date()
        days_passed = (today - start_date).days
        return min(days_passed + 1, MARATHON_DAYS)

    def _get_current_topic_index(self, user) -> int:
        """Получить индекс текущей темы."""
        if isinstance(user, dict):
            return user.get('current_topic_index', 0)
        return getattr(user, 'current_topic_index', 0)

    def _get_completed_topics(self, user) -> list:
        """Получить список завершённых тем."""
        if isinstance(user, dict):
            return user.get('completed_topics', [])
        return getattr(user, 'completed_topics', [])

    def _get_study_duration(self, user) -> int:
        """Получить длительность обучения."""
        if isinstance(user, dict):
            return user.get('study_duration', 15)
        return getattr(user, 'study_duration', 15)

    def _get_bloom_level(self, user) -> int:
        """Получить уровень сложности (Блум)."""
        if isinstance(user, dict):
            return user.get('bloom_level', 1)
        return getattr(user, 'bloom_level', 1)

    def _user_to_intern_dict(self, user) -> dict:
        """Конвертировать user в dict для совместимости с Claude клиентом."""
        if isinstance(user, dict):
            return user
        return {
            'chat_id': getattr(user, 'chat_id', None),
            'language': getattr(user, 'language', 'ru'),
            'study_duration': getattr(user, 'study_duration', 15),
            'bloom_level': getattr(user, 'bloom_level', 1),
            'occupation': getattr(user, 'occupation', ''),
            'interests': getattr(user, 'interests', ''),
            'values': getattr(user, 'values', ''),
            'goals': getattr(user, 'goals', ''),
            'completed_topics': getattr(user, 'completed_topics', []),
            'current_topic_index': getattr(user, 'current_topic_index', 0),
        }

    async def enter(self, user, context: dict = None) -> Optional[str]:
        """
        Показываем урок текущего дня.

        Проверяем:
        - Марафон завершён?
        - Есть доступные темы?
        - Лимит тем на сегодня не превышен?

        Генерируем контент через Claude API.

        Returns:
            "lesson_shown" для автоперехода к вопросу, или None
        """
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        completed = self._get_completed_topics(user)
        marathon_day = self._get_marathon_day(user)
        topic_index = self._get_current_topic_index(user)

        total_topics = get_total_topics()

        # Проверка: марафон завершён
        if len(completed) >= total_topics or len(completed) >= 28:
            await self.send(user, t('marathon.completed', lang))
            return  # Событие marathon_complete обработает StateMachine

        # Проверка: дневной лимит (с учётом last_topic_date)
        if isinstance(user, dict):
            topics_today = get_topics_today(user)
        else:
            topics_today = getattr(user, 'topics_today', 0)

        if topics_today >= MAX_TOPICS_PER_DAY:
            await self.send(user, t('marathon.daily_limit', lang))
            return

        # Получаем тему (пропускаем practice, ищем следующую theory)
        topic = get_topic(topic_index)
        while topic and topic.get('type', 'theory') != 'theory':
            # Пропускаем practice темы — они обрабатываются через task.py
            logger.info(f"Skipping practice topic {topic_index}, looking for next theory")
            topic_index += 1
            topic = get_topic(topic_index)
            # Обновляем индекс в БД
            if chat_id and topic:
                await update_intern(chat_id, current_topic_index=topic_index)

        if not topic:
            await self.send(user, t('marathon.no_topics_available', lang))
            return

        # Проверка: тема не опережает календарный день марафона
        calendar_day = self._get_calendar_marathon_day(user)
        topic_day = topic.get('day', 1)
        if topic_day > calendar_day:
            await self.send(user, f"✅ {t('marathon.come_back_tomorrow', lang)}")
            return

        # Показываем сообщение о загрузке
        await self.send(user, f"⏳ {t('marathon.generating_material', lang)}")

        try:
            # Получаем intern dict для Claude
            intern = self._user_to_intern_dict(user)
            topic_day = topic.get('day', marathon_day)

            # Генерируем контент через Claude API с таймаутом
            logger.info(f"Generating content for topic {topic_index}, day {topic_day}, user {chat_id}")
            try:
                content = await asyncio.wait_for(
                    claude.generate_content(
                        topic=topic,
                        intern=intern,
                        mcp_client=mcp_guides,
                        knowledge_client=mcp_knowledge
                    ),
                    timeout=CONTENT_GENERATION_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.error(f"Content generation timeout ({CONTENT_GENERATION_TIMEOUT}s) for user {chat_id}")
                await self.send(
                    user,
                    f"⚠️ {t('errors.content_generation_failed', lang)}\n\n"
                    f"_{t('errors.try_again_later', lang)}_",
                    parse_mode="Markdown"
                )
                return

            # Формируем заголовок
            topic_title = get_topic_title(topic, lang)
            study_duration = self._get_study_duration(user)

            header = (
                f"📚 *{t('marathon.day_theory', lang, day=topic_day)}*\n"
                f"*{topic_title}*\n"
                f"⏱ {t('marathon.minutes', lang, minutes=study_duration)}\n\n"
            )

            # Отправляем контент
            full = header + content
            if len(full) > 4000:
                await self.send(user, header, parse_mode="Markdown")
                # Разбиваем контент на части
                for i in range(0, len(content), 4000):
                    await self.send(user, content[i:i+4000])
            else:
                await self.send(user, full, parse_mode="Markdown")

            logger.info(f"Content sent to user {chat_id}, length: {len(content)}")

            # Автоматический переход к вопросу
            return "lesson_shown"

        except Exception as e:
            logger.error(f"Error generating content for user {chat_id}: {e}")
            await self.send(
                user,
                f"⚠️ {t('errors.content_generation_failed', lang)}\n\n"
                f"_{t('errors.try_again_later', lang)}_",
                parse_mode="Markdown"
            )
            return None

    async def handle(self, user, message: Message) -> Optional[str]:
        """
        Обрабатываем ввод пользователя.

        В этом стейте пользователь обычно просто читает материал.
        Любое сообщение переводит к вопросу.
        """
        text = (message.text or "").strip()
        lang = self._get_lang(user)

        # Вопрос к ИИ
        if text.startswith('?'):
            # Обрабатываем вопрос, но остаёмся в стейте
            await self.send(user, t('marathon.question_processed', lang))
            return None

        # Готов к вопросу
        return "lesson_shown"

    async def exit(self, user) -> dict:
        """Передаём контекст следующему стейту."""
        return {
            "topic_index": self._get_current_topic_index(user),
            "marathon_day": self._get_marathon_day(user)
        }
