"""
Стейт: Практическое задание Марафона.

Вход: после вопроса урока (или бонусного вопроса)
Выход:
  - submitted → common.mode_select (день завершён)
  - day_complete → common.mode_select (день завершён)
  - marathon_complete → common.mode_select (марафон завершён)
"""

import asyncio
from typing import Optional

from aiogram.types import Message, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

from states.base import BaseState
from i18n import t
from helpers.message_split import prepare_html_parts
from db.queries import update_intern, save_answer, moscow_today
from db.queries.marathon import get_marathon_content, save_marathon_content
from core.knowledge import get_topic, get_topic_title, get_total_topics
from core.topics import get_marathon_day
from config import CLAUDE_MODEL_HAIKU
from clients import claude
from config import get_logger, DAILY_TOPICS_LIMIT
from config.settings import EVALUATION_ENABLED, WP_VALIDATION_ENABLED

logger = get_logger(__name__)


class MarathonTaskState(BaseState):
    """
    Стейт практического задания Марафона.

    Показывает задание, принимает рабочий продукт, завершает день.
    """

    name = "workshop.marathon.task"
    display_name = {"ru": "Задание", "en": "Task", "es": "Tarea", "fr": "Tâche"}
    allow_global = ["consultation", "notes"]
    keyboard_type = "reply"

    # Тексты кнопок для навигации
    SETTINGS_BUTTONS = ["⚙️ Настройки", "⚙️ Settings", "⚙️ Ajustes", "⚙️ Paramètres"]

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

    def _get_current_topic_index(self, user) -> int:
        """Получить индекс текущей темы."""
        if isinstance(user, dict):
            return user.get('current_topic_index', 0)
        return getattr(user, 'current_topic_index', 0)

    def _get_bloom_level(self, user) -> int:
        """Получить уровень сложности."""
        if isinstance(user, dict):
            return user.get('complexity_level', 1) or user.get('bloom_level', 1) or 1
        return getattr(user, 'complexity_level', 1) or getattr(user, 'bloom_level', 1) or 1

    def _get_completed_topics(self, user) -> list:
        """Получить список завершённых тем."""
        if isinstance(user, dict):
            return user.get('completed_topics', [])
        return getattr(user, 'completed_topics', [])

    def _get_marathon_day(self, user) -> int:
        """Получить текущий день марафона (canonical — через core.topics)."""
        intern = self._user_to_intern_dict(user)
        return get_marathon_day(intern)

    def _get_topics_today(self, user) -> int:
        """Получить количество тем за сегодня."""
        if isinstance(user, dict):
            return user.get('topics_today', 0)
        return getattr(user, 'topics_today', 0)

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

    async def enter(self, user, context: dict = None) -> None:
        """
        Показываем практическое задание.

        Context может содержать:
        - topic_index: индекс темы
        - from_bonus: пришли из бонусного вопроса
        - from_question: пришли из вопроса урока
        """
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        marathon_day = self._get_marathon_day(user)
        topic_index = self._get_current_topic_index(user)

        # Получаем тему
        topic = get_topic(topic_index)
        if not topic:
            await self.send(user, t('marathon.no_topics_available', lang))
            return

        # ─── Попытка загрузить пре-генерированную практику из БД ───
        pre_generated = await get_marathon_content(chat_id, topic_index)
        practice_data = pre_generated.get('practice_content') if pre_generated else None

        if practice_data and isinstance(practice_data, dict):
            logger.info(f"Loaded pre-generated practice for user {chat_id}, topic {topic_index}")
        else:
            # Fallback: генерация на лету
            await self.send(user, f"⏳ {t('marathon.preparing_practice', lang)}")

            try:
                intern = self._user_to_intern_dict(user)
                logger.info(f"Generating practice on-the-fly for topic {topic_index}, user {chat_id}, lang {lang}")
                # Rule 10.20: Haiku for on-the-fly (3-5s vs 14s Sonnet)
                practice_data = await claude.generate_practice_intro(
                    topic=topic,
                    intern=intern,
                    model=CLAUDE_MODEL_HAIKU,
                )
                # Сохраняем в БД для повторного использования
                await save_marathon_content(chat_id, topic_index, practice_content=practice_data)
                logger.info(f"Cached on-the-fly practice for user {chat_id}, topic {topic_index}")
            except Exception as e:
                logger.error(f"Error generating practice intro for user {chat_id}: {e}")
                # Fallback: показываем задание без введения
                task_text = topic.get('task', t('marathon.task_default', lang))
                work_product = topic.get('work_product', t('marathon.work_product_default', lang))

                skip_btn = t('buttons.skip_practice', lang)
                keyboard = ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(text=skip_btn)]],
                    resize_keyboard=True,
                    one_time_keyboard=True
                )

                await self.send(
                    user,
                    f"✏️ *{t('marathon.day_practice', lang, day=marathon_day)}*\n\n"
                    f"📋 *{t('marathon.task', lang)}:*\n"
                    f"{task_text}\n\n"
                    f"🎯 *{t('marathon.work_product', lang)}:* {work_product}\n\n"
                    f"📝 *{t('marathon.when_complete', lang)}:*\n"
                    f"{t('marathon.write_wp_name', lang)}\n\n"
                    f"💬 *{t('marathon.waiting_for', lang)}:* {t('marathon.work_product_name', lang)}",
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
                return

        # ─── Показываем практическое задание ───
        topic_title = get_topic_title(topic, lang)
        intro = practice_data.get('intro', '')
        task_text = practice_data.get('task', '') or topic.get('task', t('marathon.task_default', lang))
        work_product = practice_data.get('work_product', '') or topic.get('work_product', t('marathon.work_product_default', lang))
        examples = practice_data.get('examples', '')

        message = (
            f"✏️ *{t('marathon.day_practice', lang, day=marathon_day)}*\n"
            f"*{topic_title}*\n\n"
        )

        if intro:
            message += f"{intro}\n\n"

        message += f"📋 *{t('marathon.task', lang)}:*\n{task_text}\n\n"
        message += f"🎯 *{t('marathon.work_product', lang)}:* {work_product}\n"

        bloom_level = self._get_bloom_level(user)
        if examples and bloom_level >= 2:
            message += f"{t('marathon.wp_examples', lang)}:\n{examples}\n\n"
        else:
            message += "\n"

        message += (
            f"📝 *{t('marathon.when_complete', lang)}:*\n"
            f"{t('marathon.write_wp_name', lang)}\n\n"
            f"💬 *{t('marathon.waiting_for', lang)}:* {t('marathon.work_product_name', lang)}"
        )

        skip_btn = t('buttons.skip_practice', lang)
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=skip_btn)]],
            resize_keyboard=True,
            one_time_keyboard=True
        )

        parts = prepare_html_parts(message)
        for i, part in enumerate(parts):
            is_last = (i == len(parts) - 1)
            kb = keyboard if is_last else None
            await self.send(user, part, parse_mode="HTML", reply_markup=kb)
        logger.info(f"Practice task sent to user {chat_id}, lang {lang}, parts: {len(parts)}")

        # Rule 10.19: Look-ahead — pre-gen next topic in background
        intern_dict = self._user_to_intern_dict(user)
        asyncio.create_task(
            _pregen_next_topic_bg(chat_id, intern_dict, topic_index)
        )

    async def handle(self, user, message: Message) -> Optional[str]:
        """
        Обрабатываем ответ с рабочим продуктом.

        Возвращает:
        - "marathon_complete" → марафон полностью завершён
        - "submitted" / "day_complete" → день завершён, ждём следующий день
        - None → остаёмся в стейте (короткий ответ или вопрос)
        """
        text = (message.text or "").strip()
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        # Примечание: вопросы с ? обрабатываются глобально через State Machine
        # (allow_global: [consultation] → common.consultation)

        # Пропуск практики (кнопка или текст)
        skip_btn = t('buttons.skip_practice', lang)
        if text == skip_btn or "пропустить" in text.lower() or "skip" in text.lower():
            await self.send_remove_keyboard(user, t('marathon.practice_skipped', lang))
            return "day_complete"

        # Настройки — переход в настройки
        if text in self.SETTINGS_BUTTONS or "настройки" in text.lower() or "settings" in text.lower():
            return "settings"

        # Слишком короткий ответ
        if len(text) < 3:
            await self.send(
                user,
                f"{t('marathon.waiting_for', lang)}: {t('marathon.work_product_name', lang)}"
            )
            return None

        # ─── Валидация формулировки РП (DS-evaluator-agent) ───
        topic_index = self._get_current_topic_index(user)
        bloom_level = self._get_bloom_level(user)

        if WP_VALIDATION_ENABLED and lang == 'ru':
            try:
                from core.wp_validator import validate_formulation, get_wp_hint
                validation = await validate_formulation(text)
                if not validation["valid"]:
                    hint = get_wp_hint(
                        bloom_level=bloom_level,
                        text=text,
                        suggestion=validation.get("suggestion", ""),
                        lang=lang,
                    )
                    try:
                        await self.send(user, hint, parse_mode="Markdown")
                    except Exception:
                        await self.send(user, hint)
                    # не блокируем — принимаем ответ, hint как совет
            except Exception as e:
                logger.warning(f"WP validation error for user {chat_id}: {e}")

        # Сохраняем рабочий продукт
        if chat_id:
            await save_answer(
                chat_id=chat_id,
                topic_index=topic_index,
                answer=f"[РП] {text}",
                answer_type="work_product",
                complexity_level=bloom_level
            )

            # ЦД: событие marathon_task (WP-85, WP-151 Ф3: расширенный payload)
            from db.queries.events import log_event
            from core.topics import get_topic
            topic_data = get_topic(topic_index)
            await log_event(chat_id, 'marathon_task', {
                'topic_index': topic_index,
                'topic_id': topic_data.get('id') if topic_data else None,
                'topic_title': topic_data.get('title') if topic_data else None,
                'topic_type': topic_data.get('type') if topic_data else None,
                'complexity_level': bloom_level,
                'answer_type': 'work_product',
                'answer_length': len(text),
            }, confidence=0.9)

        # Сразу подтверждаем — ДО evaluator, чтобы feedback воспринимался как совет
        await self.send(user, f"✅ *{t('marathon.practice_accepted', lang)}*", parse_mode="Markdown")

        # ─── Оценка рабочего продукта + фиксация (DS-evaluator-agent) ───
        if EVALUATION_ENABLED:
            topic = get_topic(topic_index)
            if topic:
                try:
                    from core.evaluator import evaluate_and_fixate
                    intern = self._user_to_intern_dict(user)
                    evaluation = await evaluate_and_fixate(
                        answer_text=text,
                        topic=topic,
                        bloom_level=bloom_level,
                        intern=intern,
                        telegram_user_id=chat_id,
                    )
                    if evaluation and evaluation.get("feedback"):
                        await self.send(
                            user,
                            evaluation["feedback"],
                            parse_mode="Markdown",
                        )
                except Exception as e:
                    logger.warning(f"Evaluation failed for user {chat_id}: {e}")

        # Обновляем прогресс + гарантируем marathon_status=ACTIVE и корректный mode
        completed = self._get_completed_topics(user) + [topic_index]
        topics_today = self._get_topics_today(user) + 1
        today = moscow_today()

        if chat_id:
            from db.queries.users import derive_mode
            from config.settings import MarathonStatus
            feed_status = user.get('feed_status', 'not_started')
            await update_intern(
                chat_id,
                completed_topics=completed,
                current_topic_index=topic_index + 1,
                topics_today=topics_today,
                last_topic_date=today,
                marathon_status=MarathonStatus.ACTIVE,
                mode=derive_mode(MarathonStatus.ACTIVE, feed_status),
            )

        # Проверяем статус завершения
        total_topics = get_total_topics()
        marathon_completed = len(completed) >= total_topics or len(completed) >= 28

        if marathon_completed:
            # Марафон полностью завершён — обновляем статус + C1 конверсия в программы
            if chat_id:
                feed_status = user.get('feed_status', 'not_started')
                await update_intern(chat_id,
                    marathon_status=MarathonStatus.COMPLETED,
                    mode=derive_mode(MarathonStatus.COMPLETED, feed_status),
                )

            from config.settings import PLATFORM_URLS

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=t('marathon.btn_program_lr', lang),
                    url=PLATFORM_URLS['lr'],
                )],
                [InlineKeyboardButton(
                    text=t('marathon.btn_continue_feed', lang),
                    callback_data="mode_feed",
                )],
            ])

            await self.send(
                user,
                f"🎉 *{t('marathon.completed', lang)}*\n\n"
                f"{t('marathon.completed_next_step', lang)}\n\n"
                f"_{t('marathon.completed_hint', lang)}_",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            return "marathon_complete"

        # День завершён (практика = последняя тема дня)
        # Проверяем: была ли это тема с прошлого дня (catch-up)?
        topic = get_topic(topic_index)
        marathon_day = self._get_marathon_day(user)
        topic_day = topic.get('day', marathon_day) if topic else marathon_day
        is_catchup = topic_day < marathon_day

        if is_catchup:
            # Проверяем: есть ли темы на сегодня (marathon_day)?
            from core.topics import TOPICS
            today_uncompleted = [
                i for i, t_topic in enumerate(TOPICS)
                if t_topic.get('day') == marathon_day and i not in set(completed)
            ]

            if today_uncompleted and topics_today < 4:
                # Предлагаем сегодняшний урок
                catchup_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text=f"📚 {t('reminders.marathon_catchup_btn', lang)}",
                        callback_data="marathon_catchup_today"
                    )],
                    [InlineKeyboardButton(
                        text=f"✋ {t('reminders.marathon_catchup_skip', lang)}",
                        callback_data="marathon_catchup_no"
                    )],
                ])
                await self.send(
                    user,
                    f"✅ {t('marathon.day_complete', lang)}\n\n"
                    f"{t('reminders.marathon_catchup_offer', lang)}",
                    parse_mode="Markdown",
                    reply_markup=catchup_keyboard
                )
                return "submitted"  # → common.mode_select (buttons stay)

        await self.send(
            user,
            f"✅ {t('marathon.day_complete', lang)}\n\n"
            f"_{t('marathon.come_back_tomorrow', lang)}_",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
        return "submitted"  # → common.mode_select

    async def exit(self, user) -> dict:
        """Передаём контекст следующему стейту."""
        return {
            "day_completed": True,
            "topics_completed": len(self._get_completed_topics(user))
        }


async def _pregen_next_topic_bg(chat_id: int, intern: dict, current_topic_index: int):
    """Background task: look-ahead pre-gen для следующей темы (Rule 10.19)."""
    try:
        from core.scheduler import pregen_next_for_user
        await pregen_next_for_user(chat_id, intern, current_topic_index)
    except Exception as e:
        logger.warning(f"[LookAhead] Background pre-gen failed for {chat_id}: {e}")
