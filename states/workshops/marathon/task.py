"""
Стейт: Практическое задание Марафона.

Вход: после вопроса урока (или бонусного вопроса)
Выход:
  - submitted → common.mode_select (день завершён)
  - day_complete → common.mode_select (день завершён)
  - marathon_complete → common.mode_select (марафон завершён)
"""

from typing import Optional

from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

from states.base import BaseState
from i18n import t
from db.queries import update_intern, save_answer, moscow_today
from core.knowledge import get_topic, get_topic_title, get_total_topics
from core.topics import get_marathon_day
from clients import claude
from config import get_logger, DAILY_TOPICS_LIMIT

logger = get_logger(__name__)


class MarathonTaskState(BaseState):
    """
    Стейт практического задания Марафона.

    Показывает задание, принимает рабочий продукт, завершает день.
    """

    name = "workshop.marathon.task"
    display_name = {"ru": "Задание", "en": "Task", "es": "Tarea", "fr": "Tâche"}
    allow_global = ["consultation", "notes"]

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

        # Показываем сообщение о загрузке
        await self.send(user, f"⏳ {t('marathon.preparing_practice', lang)}")

        try:
            # Получаем intern dict для Claude
            intern = self._user_to_intern_dict(user)

            # Генерируем полное описание задания через Claude API (включая перевод)
            logger.info(f"Generating practice content for topic {topic_index}, user {chat_id}, lang {lang}")
            practice_data = await claude.generate_practice_intro(
                topic=topic,
                intern=intern
            )

            # Получаем переведённые данные из ответа Claude
            topic_title = get_topic_title(topic, lang)
            intro = practice_data.get('intro', '')
            task_text = practice_data.get('task', '') or topic.get('task', t('marathon.task_default', lang))
            work_product = practice_data.get('work_product', '') or topic.get('work_product', t('marathon.work_product_default', lang))
            examples = practice_data.get('examples', '')

            # Формируем сообщение
            message = (
                f"✏️ *{t('marathon.day_practice', lang, day=marathon_day)}*\n"
                f"*{topic_title}*\n\n"
            )

            if intro:
                message += f"{intro}\n\n"

            message += f"📋 *{t('marathon.task', lang)}:*\n{task_text}\n\n"
            message += f"🎯 *{t('marathon.work_product', lang)}:* {work_product}\n"

            if examples:
                message += f"{t('marathon.wp_examples', lang)}:\n{examples}\n\n"
            else:
                message += "\n"

            message += (
                f"📝 *{t('marathon.when_complete', lang)}:*\n"
                f"{t('marathon.write_wp_name', lang)}\n\n"
                f"💬 *{t('marathon.waiting_for', lang)}:* {t('marathon.work_product_name', lang)}"
            )

            # Клавиатура с кнопкой пропуска
            skip_btn = t('buttons.skip_practice', lang)
            keyboard = ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text=skip_btn)]],
                resize_keyboard=True,
                one_time_keyboard=True
            )

            await self.send(user, message, parse_mode="Markdown", reply_markup=keyboard)
            logger.info(f"Practice task sent to user {chat_id}, lang {lang}")

        except Exception as e:
            logger.error(f"Error generating practice intro for user {chat_id}: {e}")
            # Fallback: показываем задание без введения
            task_text = topic.get('task', t('marathon.task_default', lang))
            work_product = topic.get('work_product', t('marathon.work_product_default', lang))

            # Клавиатура с кнопкой пропуска (fallback)
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

        # Сохраняем рабочий продукт
        topic_index = self._get_current_topic_index(user)
        if chat_id:
            await save_answer(
                chat_id=chat_id,
                topic_index=topic_index,
                answer=f"[РП] {text}",
                answer_type="work_product"
            )

        # Обновляем прогресс
        completed = self._get_completed_topics(user) + [topic_index]
        topics_today = self._get_topics_today(user) + 1
        today = moscow_today()

        if chat_id:
            await update_intern(
                chat_id,
                completed_topics=completed,
                current_topic_index=topic_index + 1,
                topics_today=topics_today,
                last_topic_date=today
            )

        # Проверяем статус завершения
        total_topics = get_total_topics()
        marathon_completed = len(completed) >= total_topics or len(completed) >= 28

        if marathon_completed:
            # Марафон полностью завершён
            await self.send(
                user,
                f"✅ *{t('marathon.practice_accepted', lang)}*\n\n"
                f"🎉 *{t('marathon.completed', lang)}*\n\n"
                f"_{t('marathon.completed_hint', lang)}_",
                parse_mode="Markdown"
            )
            return "marathon_complete"

        # День завершён (практика = последняя тема дня)
        await self.send(
            user,
            f"✅ *{t('marathon.practice_accepted', lang)}*\n\n"
            f"✅ {t('marathon.day_complete', lang)}\n\n"
            f"_{t('marathon.come_back_tomorrow', lang)}_",
            parse_mode="Markdown"
        )
        return "submitted"  # → common.mode_select

    async def exit(self, user) -> dict:
        """Передаём контекст следующему стейту."""
        return {
            "day_completed": True,
            "topics_completed": len(self._get_completed_topics(user))
        }
