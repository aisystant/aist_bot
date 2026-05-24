"""
Стейт: Урок Марафона.

Вход: из common.mode_select (выбор "Марафон") или после завершения задания
Выход: workshop.marathon.question (после показа урока)
"""

import asyncio
from typing import Optional

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from states.base import BaseState
from i18n import t
from helpers.message_split import prepare_html_parts
from db.queries import get_intern, update_intern
from db.queries.marathon import get_marathon_content, mark_content_delivered, save_marathon_content
from db.queries.users import moscow_today, get_topics_today
from core.knowledge import get_topic, get_topic_title, get_total_topics
from core.topics import get_marathon_day as canonical_get_marathon_day
from clients import claude
from config import get_logger, MARATHON_DAYS, MAX_TOPICS_PER_DAY, CLAUDE_MODEL_HAIKU, CLAUDE_MODEL_SONNET

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
        """Получить текущий день марафона (canonical — через core.topics)."""
        intern = self._user_to_intern_dict(user)
        return canonical_get_marathon_day(intern)

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
            val = user.get('current_topic_index', 0)
        else:
            val = getattr(user, 'current_topic_index', 0)
        try:
            return int(val) if val is not None and val != '' else 0
        except (ValueError, TypeError):
            return 0

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
            # Обновляем статус если ещё не completed (guard — повторный вход)
            if chat_id and user.get('marathon_status') != 'completed':
                from db.queries.users import derive_mode
                from config import MarathonStatus
                feed_status = user.get('feed_status', 'not_started')
                await update_intern(chat_id,
                    marathon_status=MarathonStatus.COMPLETED,
                    mode=derive_mode(MarathonStatus.COMPLETED, feed_status),
                )

                # WP-151 Ф3: marathon_completed
                from db.queries.events import log_event
                await log_event(chat_id, 'marathon_completed', {
                    'total_topics': total_topics,
                    'completed_topics': len(completed),
                    'path': 'lesson_state',
                })

            await self.send(user, t('marathon.completed', lang))
            return "marathon_complete"

        # Проверка: дневной лимит (с учётом last_topic_date)
        if isinstance(user, dict):
            topics_today = get_topics_today(user)
        else:
            topics_today = getattr(user, 'topics_today', 0)

        if topics_today >= MAX_TOPICS_PER_DAY:
            await self.send(user, t('marathon.daily_limit', lang))
            return "daily_limit"

        # Проверяем тип текущей темы
        topic = get_topic(topic_index)
        if topic and topic.get('type', 'theory') != 'theory':
            # Текущая тема — practice → маршрутизируем в task state
            logger.info(f"Current topic {topic_index} is practice, routing to task state")
            return "already_completed"  # → workshop.marathon.task

        if not topic:
            back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"← {t('buttons.back_to_menu', lang)}",
                    callback_data="marathon_back_menu"
                )]
            ])
            await self.send(user, t('marathon.no_topics_available', lang), reply_markup=back_keyboard)
            return "come_back"

        # Гарантируем marathon_start_date: если нет или в будущем — ставим сегодня
        if isinstance(user, dict):
            start_date = user.get('marathon_start_date')
        else:
            start_date = getattr(user, 'marathon_start_date', None)

        today = moscow_today()
        needs_start_date_fix = False
        if not start_date:
            needs_start_date_fix = True
        else:
            from datetime import datetime as _dt
            sd = start_date.date() if isinstance(start_date, _dt) else start_date
            if sd > today:
                needs_start_date_fix = True

        if needs_start_date_fix:
            await update_intern(chat_id, marathon_start_date=today)
            if isinstance(user, dict):
                user['marathon_start_date'] = today
            logger.info(f"Set marathon_start_date={today} for user {chat_id}")

        # Проверка: тема не опережает календарный день марафона
        calendar_day = self._get_calendar_marathon_day(user)
        topic_day = topic.get('day', 1)
        if topic_day > calendar_day:
            await self.send(user, f"✅ {t('marathon.come_back_tomorrow', lang)}")
            return "come_back"

        topic_day = topic.get('day', marathon_day)

        # ─── Попытка загрузить пре-генерированный контент из БД ───
        pre_generated = await get_marathon_content(chat_id, topic_index)

        if pre_generated and pre_generated.get('lesson_content') and len(pre_generated['lesson_content']) > 200:
            # Контент готов — показываем мгновенно
            content = pre_generated['lesson_content']
            await mark_content_delivered(chat_id, topic_index)
            logger.info(f"Loaded pre-generated lesson for user {chat_id}, topic {topic_index}")
        else:
            # Fallback: генерация на лету
            await self.send(user, f"⏳ {t('marathon.generating_material', lang)}")

            try:
                intern = self._user_to_intern_dict(user)
                logger.info(f"Generating content on-the-fly for topic {topic_index}, day {topic_day}, user {chat_id}")
                try:
                    # Rule 10.20: Haiku for on-the-fly (3-5s vs 15-19s Sonnet)
                    content = await asyncio.wait_for(
                        claude.generate_content(
                            topic=topic,
                            intern=intern,
                            model=CLAUDE_MODEL_HAIKU,
                        ),
                        timeout=CONTENT_GENERATION_TIMEOUT
                    )
                    if not content:
                        logger.warning(f"Haiku returned None for user {chat_id}, topic {topic_index} — trying Sonnet fallback")
                        content = await asyncio.wait_for(
                            claude.generate_content(
                                topic=topic,
                                intern=intern,
                                model=CLAUDE_MODEL_SONNET,
                            ),
                            timeout=CONTENT_GENERATION_TIMEOUT
                        )
                    if not content:
                        logger.error(f"Content generation returned None for user {chat_id}, topic {topic_index}")
                        retry_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(
                                text=f"🔄 {t('buttons.try_again', lang)}",
                                callback_data="marathon_retry_lesson"
                            )],
                            [InlineKeyboardButton(
                                text=f"← {t('buttons.back_to_menu', lang)}",
                                callback_data="marathon_back_menu"
                            )],
                        ])
                        await self.send(
                            user,
                            f"⚠️ {t('errors.content_generation_failed', lang)}\n\n"
                            f"_{t('errors.try_again_later', lang)}_",
                            reply_markup=retry_keyboard,
                            parse_mode="Markdown"
                        )
                        return
                    # Сохраняем в БД для повторного использования
                    await save_marathon_content(chat_id, topic_index, lesson_content=content)
                    await mark_content_delivered(chat_id, topic_index)
                    logger.info(f"Cached on-the-fly lesson for user {chat_id}, topic {topic_index}")
                except asyncio.TimeoutError:
                    logger.error(f"Content generation timeout ({CONTENT_GENERATION_TIMEOUT}s) for user {chat_id}")
                    retry_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(
                            text=f"🔄 {t('buttons.try_again', lang)}",
                            callback_data="marathon_retry_lesson"
                        )],
                        [InlineKeyboardButton(
                            text=f"← {t('buttons.back_to_menu', lang)}",
                            callback_data="marathon_back_menu"
                        )],
                    ])
                    await self.send(
                        user,
                        f"⚠️ {t('errors.content_generation_failed', lang)}\n\n"
                        f"_{t('errors.try_again_later', lang)}_",
                        reply_markup=retry_keyboard,
                        parse_mode="Markdown"
                    )
                    return

            except Exception as e:
                logger.error(f"Error generating content for user {chat_id}: {e}", exc_info=True)
                retry_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text=f"🔄 {t('buttons.try_again', lang)}",
                        callback_data="marathon_retry_lesson"
                    )],
                    [InlineKeyboardButton(
                        text=f"← {t('buttons.back_to_menu', lang)}",
                        callback_data="marathon_back_menu"
                    )],
                ])
                await self.send(
                    user,
                    f"⚠️ {t('errors.content_generation_failed', lang)}\n\n"
                    f"_{t('errors.try_again_later', lang)}_",
                    reply_markup=retry_keyboard,
                    parse_mode="Markdown"
                )
                return

        # ─── Показываем урок ───
        topic_title = get_topic_title(topic, lang)
        study_duration = self._get_study_duration(user)

        header = (
            f"📚 *{t('marathon.day_theory', lang, day=topic_day)}*\n"
            f"*{topic_title}*\n"
            f"⏱ {t('marathon.minutes', lang, minutes=study_duration)}\n\n"
        )

        # Кнопка «Далее → Вопрос» — прикрепляем к последнему сообщению урока
        next_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"💭 {t('buttons.next_question', lang)}",
                callback_data="marathon_next_question"
            )]
        ])

        full = header + content
        parts = prepare_html_parts(full)
        for i, part in enumerate(parts):
            is_last = (i == len(parts) - 1)
            kb = next_keyboard if is_last else None
            await self.send(user, part, parse_mode="HTML", reply_markup=kb)

        logger.info(f"Content sent to user {chat_id}, length: {len(content)}")

        # WP-253 Блок 2: lesson_completed event for stage_evaluator activity coverage
        asyncio.create_task(_log_lesson_completed(chat_id, topic_index, topic_day))

        # Rule 10.19: Look-ahead — pre-gen next topic in background
        intern_dict = self._user_to_intern_dict(user)
        asyncio.create_task(
            _pregen_next_topic_bg(chat_id, intern_dict, topic_index)
        )

        return None  # ждём клик

    async def handle(self, user, message: Message) -> Optional[str]:
        """
        Обрабатываем ввод пользователя.

        В этом стейте пользователь обычно просто читает материал.
        Любое сообщение переводит к вопросу.
        """
        text = (message.text or "").strip()
        lang = self._get_lang(user)

        # Проверка: текущая тема — practice → маршрутизируем в task
        topic_index = self._get_current_topic_index(user)
        topic = get_topic(topic_index)
        if topic and topic.get('type', 'theory') != 'theory':
            return "already_completed"  # → workshop.marathon.task

        # Проверка: если следующая тема впереди по дням — блокируем
        if topic:
            calendar_day = self._get_calendar_marathon_day(user)
            topic_day = topic.get('day', 1)
            if topic_day > calendar_day:
                await self.send(user, f"✅ {t('marathon.come_back_tomorrow', lang)}")
                return "come_back"

        # Вопрос к ИИ
        if text.startswith('?'):
            # Обрабатываем вопрос, но остаёмся в стейте
            await self.send(user, t('marathon.question_processed', lang))
            return None

        # Текстовое сообщение тоже переводит к вопросу
        return "lesson_shown"

    async def handle_callback(self, user, callback) -> Optional[str]:
        """Обработка inline-кнопок."""
        await callback.answer()

        if callback.data == "marathon_next_question":
            return "lesson_shown"

        if callback.data == "marathon_retry_lesson":
            await self.enter(user)
            return None

        if callback.data == "marathon_back_menu":
            return "come_back"

        return None

    async def exit(self, user) -> dict:
        """Передаём контекст следующему стейту."""
        return {
            "topic_index": self._get_current_topic_index(user),
            "marathon_day": self._get_marathon_day(user)
        }


async def _log_lesson_completed(chat_id: int, topic_index: int, topic_day: int) -> None:
    """Fire-and-forget: emit lesson_completed event for stage_evaluator coverage (WP-253 Блок 2)."""
    try:
        from db.queries.events import log_event
        await log_event(chat_id, 'lesson_completed', {
            'topic_index': topic_index,
            'topic_day': topic_day,
        })
    except Exception as exc:
        logger.warning("[lesson_completed] event emit failed for %s: %s", chat_id, exc)


async def _pregen_next_topic_bg(chat_id: int, intern: dict, current_topic_index: int):
    """Background task: look-ahead pre-gen для следующей темы (Rule 10.19)."""
    try:
        from core.scheduler import pregen_next_for_user
        await pregen_next_for_user(chat_id, intern, current_topic_index)
    except Exception as e:
        logger.warning(f"[LookAhead] Background pre-gen failed for {chat_id}: {e}")
