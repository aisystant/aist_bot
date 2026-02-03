"""
Стейт: Вопрос урока Марафона.

Вход: после показа урока (workshop.marathon.lesson)
Выход:
  - workshop.marathon.bonus (если bloom_level >= 2)
  - workshop.marathon.task (если bloom_level == 1 или пропуск)
"""

from typing import Optional

from aiogram.types import Message

from states.base import BaseState
from i18n import t
from db.queries import update_intern, save_answer
from core.knowledge import get_topic
from clients import claude
from config import get_logger

logger = get_logger(__name__)


# Автоповышение уровня после N тем
BLOOM_AUTO_UPGRADE_AFTER = 7


class MarathonQuestionState(BaseState):
    """
    Стейт вопроса на понимание урока.

    Показывает вопрос, принимает ответ, обновляет прогресс.
    """

    name = "workshop.marathon.question"
    display_name = {"ru": "Вопрос урока", "en": "Lesson Question", "es": "Pregunta de lección", "fr": "Question de leçon"}
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

    def _get_bloom_level(self, user) -> int:
        """Получить уровень сложности."""
        if isinstance(user, dict):
            return user.get('complexity_level', 1) or user.get('bloom_level', 1) or 1
        return getattr(user, 'complexity_level', 1) or getattr(user, 'bloom_level', 1) or 1

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

    def _get_topics_at_bloom(self, user) -> int:
        """Получить количество тем на текущем уровне."""
        if isinstance(user, dict):
            return user.get('topics_at_current_complexity', 0) or user.get('topics_at_current_bloom', 0) or 0
        return getattr(user, 'topics_at_current_complexity', 0) or getattr(user, 'topics_at_current_bloom', 0) or 0

    def _user_to_intern_dict(self, user) -> dict:
        """Конвертировать user в dict для совместимости с Claude клиентом."""
        if isinstance(user, dict):
            return user
        return {
            'chat_id': getattr(user, 'chat_id', None),
            'language': getattr(user, 'language', 'ru'),
            'study_duration': getattr(user, 'study_duration', 15),
            'bloom_level': getattr(user, 'bloom_level', 1),
            'complexity_level': getattr(user, 'complexity_level', 1),
            'occupation': getattr(user, 'occupation', ''),
            'interests': getattr(user, 'interests', ''),
            'values': getattr(user, 'values', ''),
            'goals': getattr(user, 'goals', ''),
            'completed_topics': getattr(user, 'completed_topics', []),
            'current_topic_index': getattr(user, 'current_topic_index', 0),
        }

    async def enter(self, user, context: dict = None) -> None:
        """
        Показываем вопрос на понимание урока.

        Context может содержать:
        - topic_index: индекс темы
        - marathon_day: день марафона
        """
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        bloom_level = self._get_bloom_level(user)
        topic_index = self._get_current_topic_index(user)

        # Получаем тему
        topic = get_topic(topic_index)
        if not topic:
            await self.send(user, t('marathon.no_topics_available', lang))
            return

        # Показываем сообщение о генерации
        await self.send(user, f"⏳ {t('marathon.generating_question', lang)}")

        try:
            # Получаем intern dict для Claude
            intern = self._user_to_intern_dict(user)

            # Генерируем вопрос через Claude API
            logger.info(f"Generating question for topic {topic_index}, bloom {bloom_level}, user {chat_id}")
            question = await claude.generate_question(
                topic=topic,
                intern=intern,
                bloom_level=bloom_level
            )

            # Формируем сообщение с вопросом
            header = f"💭 *{t('marathon.reflection_question', lang)}* ({t(f'bloom.level_{bloom_level}_short', lang)})\n\n"
            footer = (
                f"\n\n_{t('marathon.answer_hint', lang)}_\n\n"
                f"💬 *{t('marathon.waiting_for', lang)}:* {t('marathon.answer_expected', lang)}"
            )

            await self.send(user, header + question + footer, parse_mode="Markdown")
            logger.info(f"Question sent to user {chat_id}, length: {len(question)}")

        except Exception as e:
            logger.error(f"Error generating question for user {chat_id}: {e}")
            await self.send(
                user,
                f"⚠️ {t('errors.question_generation_failed', lang)}\n\n"
                f"_{t('errors.try_again_later', lang)}_",
                parse_mode="Markdown"
            )

    async def handle(self, user, message: Message) -> Optional[str]:
        """
        Обрабатываем ответ на вопрос.

        Возвращает:
        - "correct" → bonus (уровни 2-3)
        - "correct_level_1" → task (уровень 1)
        - "skip" → task
        - None → остаёмся (короткий ответ или вопрос к ИИ)
        """
        text = (message.text or "").strip()
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        # Вопрос к ИИ (начинается с ?)
        if text.startswith('?'):
            question_text = text[1:].strip()
            if question_text:
                # TODO: Обработка вопроса через handle_question
                await self.send(
                    user,
                    f"_Ответ на ваш вопрос..._\n\n"
                    f"💬 *{t('marathon.waiting_for', lang)}:* {t('marathon.answer_expected', lang)}",
                    parse_mode="Markdown"
                )
            return None  # Остаёмся в стейте

        # Пропуск темы
        if "пропустить" in text.lower() or "skip" in text.lower():
            await self.send(user, t('marathon.topic_skipped', lang))
            return "skip"

        # Настройки — переход в настройки
        if text in self.SETTINGS_BUTTONS or "настройки" in text.lower() or "settings" in text.lower():
            return "settings"

        # Слишком короткий ответ
        if len(text) < 20:
            await self.send(
                user,
                f"{t('marathon.waiting_for', lang)}: {t('marathon.answer_expected', lang)}"
            )
            return None

        # Сохраняем ответ
        topic_index = self._get_current_topic_index(user)
        if chat_id:
            await save_answer(
                chat_id=chat_id,
                topic_index=topic_index,
                answer=text,
                answer_type="theory_answer"
            )

        # Обновляем прогресс
        completed = self._get_completed_topics(user) + [topic_index]
        topics_at_bloom = self._get_topics_at_bloom(user) + 1
        # Сохраняем ИСХОДНЫЙ уровень для решения о бонусе
        original_bloom_level = self._get_bloom_level(user)
        bloom_level = original_bloom_level

        # Автоповышение уровня
        if topics_at_bloom >= BLOOM_AUTO_UPGRADE_AFTER and bloom_level < 3:
            bloom_level += 1
            topics_at_bloom = 0
            await self.send(
                user,
                f"🎉 *{t('marathon.level_up', lang)}* *{t(f'bloom.level_{bloom_level}_short', lang)}*!",
                parse_mode="Markdown"
            )

        if chat_id:
            await update_intern(
                chat_id,
                completed_topics=completed,
                current_topic_index=topic_index + 1,
                complexity_level=bloom_level,
                topics_at_current_complexity=topics_at_bloom
            )

        # Подтверждение
        await self.send(user, f"✅ *{t('marathon.topic_completed', lang)}*", parse_mode="Markdown")

        # Решаем: бонус или сразу задание
        # Бонус предлагается на основе ИСХОДНОГО уровня (до автоповышения)
        # Уровень 1 → сразу задание, уровни 2-3 → предложить бонус
        if original_bloom_level >= 2:
            return "correct"  # → bonus
        else:
            return "correct_level_1"  # → task

    async def exit(self, user) -> dict:
        """Передаём контекст следующему стейту."""
        return {
            "topic_index": self._get_current_topic_index(user),
            "bloom_level": self._get_bloom_level(user),
            "from_question": True
        }
