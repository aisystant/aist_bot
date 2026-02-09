"""
Стейт: Дайджест Ленты.

Вход: из feed.topics (после выбора тем)
Выход: остаёмся в этом стейте (циклический режим) или common.mode_select
"""

import asyncio
from datetime import datetime, date
from typing import Optional, Dict

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ReplyKeyboardRemove

from states.base import BaseState
from i18n import t
from db.queries.users import get_intern, update_intern
from db.queries.feed import (
    get_current_feed_week,
    update_feed_week,
    create_feed_session,
    get_feed_session,
    get_feed_session_by_id,
    update_feed_session,
    get_incomplete_feed_session,
)
from db.queries.activity import record_active_day, get_activity_stats
from engines.feed.planner import generate_multi_topic_digest
from engines.shared import handle_question
from config import get_logger, FeedWeekStatus, FEED_SESSION_DURATION_MAX, FEED_SESSION_DURATION_MIN

logger = get_logger(__name__)

# Таймаут на генерацию контента (секунды)
CONTENT_GENERATION_TIMEOUT = 90


class FeedDigestState(BaseState):
    """
    Стейт показа дайджеста и приёма фиксации.

    Объединяет функционал показа дайджеста и ожидания фиксации.
    Пользователь может:
    - Читать дайджест
    - Задавать вопросы
    - Писать фиксацию
    - Менять темы
    """

    name = "feed.digest"
    display_name = {
        "ru": "Дайджест Ленты",
        "en": "Feed Digest",
        "es": "Resumen del Feed",
        "fr": "Digest du Flux"
    }
    allow_global = ["consultation", "notes"]

    # Состояние пользователя: chat_id -> {'session_id': int, 'waiting_fixation': bool}
    _user_data: Dict[int, Dict] = {}

    def _get_lang(self, user) -> str:
        """Получить язык пользователя."""
        if isinstance(user, dict):
            return user.get('language', 'ru') or 'ru'
        return getattr(user, 'language', 'ru') or 'ru'

    def _get_chat_id(self, user) -> int:
        """Получить chat_id пользователя."""
        if isinstance(user, dict):
            return user.get('chat_id')
        return getattr(user, 'chat_id', None)

    def _user_to_intern_dict(self, user) -> dict:
        """Конвертировать user в dict для совместимости."""
        if isinstance(user, dict):
            return user
        return {
            'chat_id': getattr(user, 'chat_id', None),
            'language': getattr(user, 'language', 'ru'),
            'name': getattr(user, 'name', ''),
            'occupation': getattr(user, 'occupation', ''),
            'feed_duration': getattr(user, 'feed_duration', FEED_SESSION_DURATION_MAX),
        }

    async def enter(self, user, context: dict = None) -> Optional[str]:
        """
        Показываем дайджест на сегодня.

        1. Проверяем активную неделю
        2. Проверяем/создаём сессию на сегодня
        3. Генерируем контент если нужно
        4. Показываем дайджест

        Context:
            show_topics_menu: если True, показываем меню тем вместо дайджеста

        Returns:
            "digest_shown" или None
        """
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        intern = self._user_to_intern_dict(user)
        context = context or {}

        # Удаляем Reply Keyboard (если остался от Marathon)
        await self.send(user, f"📚 {t('feed.menu_title', lang)}", reply_markup=ReplyKeyboardRemove())

        # Получаем текущую неделю
        week = await get_current_feed_week(chat_id)

        # Если запрошено меню тем — показываем его
        if context.get('show_topics_menu') and week:
            await self._show_topics_menu_standalone(user, week)
            return None

        if not week:
            await self.send(user, t('feed.no_active_week', lang))
            return "done"

        if week.get('status') != FeedWeekStatus.ACTIVE:
            if week.get('status') == FeedWeekStatus.PLANNING:
                await self.send(user, t('feed.select_topics_first', lang))
                return "change_topics"
            await self.send(user, t('feed.week_completed', lang))
            return "done"

        # Проверяем сессию на сегодня
        today = date.today()
        existing = await get_feed_session(week['id'], today)

        if existing:
            if existing.get('status') == 'completed':
                await self.send(user, f"✅ {t('feed.digest_completed_today', lang)}")
                await self._show_menu(user, week, digest_completed_today=True)
                return None

            # Показываем существующую сессию
            await self._show_digest(user, existing, week)
            return None

        # Проверяем незавершённую сессию за прошлые дни
        incomplete = await get_incomplete_feed_session(week['id'])
        if incomplete:
            await self.send(user, t('feed.incomplete_digest', lang))
            await self._show_digest(user, incomplete, week)
            return None

        # Генерируем новый дайджест
        await self.send(user, f"⏳ {t('loading.generating_content', lang)}")

        try:
            topics = week.get('accepted_topics', [])
            depth_level = week.get('current_day', 1)

            if not topics:
                await self.send(user, t('feed.no_topics_selected', lang))
                return "change_topics"

            # Длительность из профиля
            duration = intern.get('feed_duration', FEED_SESSION_DURATION_MAX)
            if not duration or duration < FEED_SESSION_DURATION_MIN:
                duration = (FEED_SESSION_DURATION_MIN + FEED_SESSION_DURATION_MAX) // 2

            # Генерируем контент
            content = await asyncio.wait_for(
                generate_multi_topic_digest(
                    topics=topics,
                    intern=intern,
                    duration=duration,
                    depth_level=depth_level,
                ),
                timeout=CONTENT_GENERATION_TIMEOUT
            )

            # Создаём сессию
            topics_title = ", ".join(topics)
            session = await create_feed_session(
                week_id=week['id'],
                day_number=depth_level,
                topic_title=topics_title,
                content=content,
                session_date=today,
            )

            # Показываем дайджест
            await self._show_digest(user, session, week)
            return None

        except asyncio.TimeoutError:
            logger.error(f"Digest generation timeout for user {chat_id}")
            await self.send(user, t('errors.generation_timeout', lang))
            return None
        except Exception as e:
            logger.error(f"Error generating digest for user {chat_id}: {e}")
            await self.send(user, t('errors.try_again', lang))
            return None

    async def _show_digest(self, user, session: dict, week: dict) -> None:
        """Показывает дайджест."""
        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)

        content = session.get('content', {})
        topics_list = content.get('topics_list', [])
        depth_level = content.get('depth_level', session.get('day_number', 1))

        # Формируем заголовок
        if topics_list:
            topics_str = ", ".join(topics_list)
            text = t('feed.digest_header', lang, topics=topics_str) + "\n"
        else:
            topic = session.get('topic_title', t('feed.topics_of_day', lang))
            text = t('feed.digest_header', lang, topics=topic) + "\n"

        # Показываем уровень глубины
        if depth_level > 1:
            text += f"_{t('feed.deepening', lang, level=depth_level)}_\n"

        text += "\n"

        if content.get('intro'):
            text += f"_{content['intro']}_\n\n"

        text += content.get('main_content', t('feed.content_unavailable', lang))

        if content.get('reflection_prompt'):
            text += f"\n\n💭 *{content['reflection_prompt']}*"

        # Подсказка о возможности задать вопрос
        # Кнопки (по сценарию: Фиксация, Вопрос, Темы)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"✍️ {t('buttons.write_fixation', lang)}",
                callback_data="feed_fixation"
            )],
            [InlineKeyboardButton(
                text=f"❓ {t('feed.ask_details', lang)}",
                callback_data="feed_ask_question"
            )],
            [InlineKeyboardButton(
                text=f"📋 {t('buttons.topics_menu', lang)}",
                callback_data="feed_whats_next"
            )]
        ])

        # Сохраняем состояние
        self._user_data[chat_id] = {
            'session_id': session['id'],
            'waiting_fixation': False,
            'week_id': week['id'],
        }

        # Отправляем (разбиваем длинные сообщения)
        if len(text) > 4000:
            parts = [text[i:i+4000] for i in range(0, len(text), 4000)]
            for i, part in enumerate(parts):
                if i == len(parts) - 1:
                    await self.send(user, part, reply_markup=keyboard, parse_mode="Markdown")
                else:
                    await self.send(user, part, parse_mode="Markdown")
        else:
            await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")

    async def _show_menu(self, user, week: dict, digest_completed_today: bool = False) -> None:
        """Показывает меню Ленты.

        Args:
            user: Пользователь
            week: Данные недели
            digest_completed_today: Если True, показывает "Мой прогресс" вместо "Получить дайджест"
        """
        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)

        topics = week.get('accepted_topics', [])

        text = f"📚 *{t('feed.menu_title', lang)}*\n\n"

        if topics:
            text += f"{t('feed.your_topics_label', lang)}\n"
            for i, topic in enumerate(topics, 1):
                text += f"{i}. {topic}\n"
        else:
            text += f"{t('feed.no_topics', lang)}\n"

        # Первая кнопка зависит от статуса дайджеста
        if digest_completed_today:
            first_button = InlineKeyboardButton(
                text=f"📊 {t('buttons.progress', lang)}",
                callback_data="feed_my_progress"
            )
        else:
            first_button = InlineKeyboardButton(
                text=f"📖 {t('buttons.get_digest', lang)}",
                callback_data="feed_get_digest"
            )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [first_button],
            [InlineKeyboardButton(
                text=f"📋 {t('buttons.topics_menu', lang)}",
                callback_data="feed_topics_menu"
            )]
        ])

        await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")

    async def handle(self, user, message: Message) -> Optional[str]:
        """
        Обрабатываем сообщения пользователя.

        - Фиксация (если ожидаем)
        - Вопрос к материалу
        """
        text = (message.text or "").strip()
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        if text.startswith('/'):
            return None

        data = self._user_data.get(chat_id, {})

        # Ожидаем фиксацию?
        if data.get('waiting_fixation'):
            return await self._handle_fixation(user, text)

        # Иначе — это вопрос к материалу
        if len(text) >= 3:
            await self._handle_question(user, text)

        return None

    async def _handle_fixation(self, user, text: str) -> Optional[str]:
        """Обрабатывает фиксацию."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        if len(text) < 10:
            await self.send(user, t('feed.fixation_too_short', lang))
            return None

        data = self._user_data.get(chat_id, {})
        session_id = data.get('session_id')

        if not session_id:
            await self.send(user, t('feed.start_digest_first', lang))
            return None

        try:
            # Получаем информацию о сессии для сохранения
            session = await get_feed_session_by_id(session_id)

            # Сохраняем фиксацию в feed_sessions
            await update_feed_session(session_id, {
                'fixation_text': text,
                'status': 'completed',
                'completed_at': datetime.utcnow(),
            })

            # Сохраняем фиксацию в answers для статистики
            from db.queries.answers import save_answer
            await save_answer(
                chat_id=chat_id,
                topic_index=session.get('day_number', 0) if session else 0,
                answer=text,
                mode='feed',
                answer_type='fixation',
                topic_id=session.get('topic_title') if session else None,
                feed_session_id=session_id,
            )

            # Записываем активность
            await record_active_day(
                chat_id=chat_id,
                activity_type='feed_fixation',
                mode='feed',
                reference_id=session_id,
            )

            # Увеличиваем уровень глубины
            week_id = data.get('week_id')
            if week_id:
                week = await get_current_feed_week(chat_id)
                if week:
                    new_depth = week.get('current_day', 1) + 1
                    await update_feed_week(week_id, {'current_day': new_depth})

            # Показываем статистику
            stats = await get_activity_stats(chat_id)

            stat_text = (
                f"✅ {t('feed.fixation_saved', lang)}\n\n"
                f"📊 *{t('progress.statistics', lang)}*\n"
                f"• {t('feed.active_days_label', lang)}: {stats.get('total', 0)}\n"
                f"• {t('feed.current_streak', lang)}: {stats.get('streak', 0)} {t('progress.days', lang)}"
            )
            await self.send(user, stat_text, parse_mode="Markdown")

            # Сбрасываем ожидание фиксации
            self._user_data[chat_id]['waiting_fixation'] = False

            # Показываем меню (дайджест завершён, т.к. фиксация сохранена)
            week = await get_current_feed_week(chat_id)
            if week:
                await self._show_menu(user, week, digest_completed_today=True)

            return "fixation_saved"

        except Exception as e:
            logger.error(f"Error saving fixation: {e}")
            await self.send(user, t('errors.try_again', lang))
            return None

    async def _handle_question(self, user, question: str) -> None:
        """Обрабатывает вопрос пользователя."""
        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)

        # Получаем контекст (темы недели)
        week = await get_current_feed_week(chat_id)
        context_topics = None
        if week:
            topics = week.get('accepted_topics', [])
            if topics:
                context_topics = ", ".join(topics)

        # Получаем профиль
        intern = await get_intern(chat_id)

        await self.send(user, t('shared.thinking', lang))

        try:
            answer, sources = await handle_question(
                question=question,
                intern=intern,
                context_topic=context_topics
            )

            response = answer
            if sources:
                response += "\n\n📚 _Источники: " + ", ".join(sources[:2]) + "_"

            await self.send(user, response, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Error handling question: {e}")
            await self.send(user, t('shared.question_error', lang))

    async def handle_callback(self, user, callback: CallbackQuery) -> Optional[str]:
        """Обрабатываем нажатия кнопок."""
        data = callback.data
        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)

        if data == "feed_fixation":
            # Начинаем ожидание фиксации
            self._user_data.setdefault(chat_id, {})['waiting_fixation'] = True

            await callback.message.answer(
                f"✍️ *{t('feed.fixation_title', lang)}*\n\n"
                f"{t('feed.fixation_instruction', lang)}\n\n"
                f"_{t('feed.fixation_hint', lang)}_",
                parse_mode="Markdown"
            )
            await callback.answer()
            return None

        elif data == "feed_ask_question":
            # Подсказка о вопросах
            await callback.message.answer(
                f"❓ *{t('feed.ask_details', lang)}*\n\n"
                f"_{t('marathon.question_hint', lang)}_",
                parse_mode="Markdown"
            )
            await callback.answer()
            return None

        elif data == "feed_whats_next":
            # Показываем темы
            week = await get_current_feed_week(chat_id)
            if not week:
                await callback.answer(t('errors.try_again', lang), show_alert=True)
                return None

            topics = week.get('accepted_topics', [])

            text = f"📋 *{t('feed.topics_menu_title', lang)}*\n\n"
            if topics:
                text += f"{t('feed.your_topics_label', lang)}\n"
                for i, topic in enumerate(topics, 1):
                    text += f"{i}. {topic}\n"
                text += f"\n{t('feed.topics_deepen_daily', lang)}"
            else:
                text += f"{t('feed.no_topics', lang)}"

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"✏️ {t('buttons.topics_menu', lang)}",
                    callback_data="feed_topics_menu"
                )]
            ])

            await callback.message.answer(text, reply_markup=keyboard, parse_mode="Markdown")
            await callback.answer()
            return None

        elif data == "feed_topics_menu":
            # Переход к редактированию тем
            await callback.answer()
            return "change_topics"

        elif data == "feed_get_digest":
            # Показать дайджест
            await callback.answer()
            week = await get_current_feed_week(chat_id)
            if week:
                intern = await get_intern(chat_id)
                await self.enter(intern, {})
            return None

        elif data == "feed_reset_topics":
            # Сброс тем — переходим к выбору новых тем
            # Сброс разрешён всегда, но новый дайджест не выдаётся
            # если сегодня уже был (проверка в enter())
            week = await get_current_feed_week(chat_id)
            if week:
                await update_feed_week(week['id'], {
                    'status': FeedWeekStatus.PLANNING,
                    'accepted_topics': [],
                    'suggested_topics': []  # Очищаем для перегенерации
                })
            await callback.answer()
            return "change_topics"

        elif data == "feed_my_progress":
            # Показываем прогресс пользователя
            await callback.answer()

            week = await get_current_feed_week(chat_id)
            stats = await get_activity_stats(chat_id)

            topics = week.get('accepted_topics', []) if week else []
            current_day = week.get('current_day', 1) if week else 1

            text = f"📊 *{t('buttons.progress', lang)}*\n\n"

            if topics:
                text += f"*{t('feed.your_topics_label', lang)}*\n"
                for i, topic in enumerate(topics, 1):
                    text += f"{i}. {topic}\n"
                text += "\n"

            # Проверяем, завершён ли дайджест сегодня
            today_session = await get_feed_session(week['id'], date.today()) if week else None
            digest_done_today = today_session and today_session.get('status') == 'completed'

            text += (
                f"📅 *{t('marathon.your_statistics', lang)}*\n"
                f"• {t('modes.day_label', lang).capitalize()}: {current_day}/7\n"
                f"• {t('feed.active_days_label', lang)}: {stats.get('total', 0)}\n"
                f"• {t('feed.current_streak', lang)}: {stats.get('streak', 0)} {t('progress.days', lang)}"
            )

            if digest_done_today:
                text += f"\n\n_✅ {t('feed.digest_completed_today', lang)}_"

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"📋 {t('buttons.topics_menu', lang)}",
                    callback_data="feed_topics_menu"
                )]
            ])

            await callback.message.answer(text, reply_markup=keyboard, parse_mode="Markdown")
            return None

        return None

    async def _show_topics_menu_standalone(self, user, week: dict) -> None:
        """
        Показывает меню тем как отдельное сообщение (не редактирование).

        Используется при входе с контекстом show_topics_menu=True.
        """
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        topics = week.get('accepted_topics', [])

        # Проверяем, завершён ли сегодняшний дайджест
        today = date.today()
        existing = await get_feed_session(week['id'], today)
        digest_completed_today = existing and existing.get('status') == 'completed'

        text = f"📋 *{t('feed.topics_menu_title', lang)}*\n\n"
        if topics:
            text += f"{t('feed.your_topics_label', lang)}\n"
            for i, topic in enumerate(topics, 1):
                text += f"{i}. {topic}\n"
            text += f"\n{t('feed.topics_deepen_daily', lang)}"
        else:
            text += f"{t('feed.no_topics', lang)}"

        # Первая кнопка зависит от статуса дайджеста
        if digest_completed_today:
            first_button = InlineKeyboardButton(
                text=f"📊 {t('buttons.progress', lang)}",
                callback_data="feed_my_progress"
            )
        else:
            first_button = InlineKeyboardButton(
                text=f"📖 {t('buttons.get_digest', lang)}",
                callback_data="feed_get_digest"
            )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [first_button],
            [InlineKeyboardButton(
                text=f"🔄 {t('buttons.reset_topics', lang)}",
                callback_data="feed_reset_topics"
            )]
        ])

        await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")

    async def exit(self, user) -> dict:
        """Очищаем временные данные."""
        chat_id = self._get_chat_id(user)
        self._user_data.pop(chat_id, None)
        return {}
