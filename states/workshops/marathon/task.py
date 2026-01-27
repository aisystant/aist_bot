"""
Стейт: Практическое задание Марафона.

Вход: после вопроса урока (или бонусного вопроса)
Выход: workshop.marathon.lesson (следующий урок)
"""

from typing import Optional

from aiogram.types import Message

from states.base import BaseState
from i18n import t
from db.queries import update_intern, save_answer, moscow_today


class MarathonTaskState(BaseState):
    """
    Стейт практического задания Марафона.

    Показывает задание, принимает рабочий продукт, завершает день.
    """

    name = "workshop.marathon.task"
    display_name = {"ru": "Задание", "en": "Task"}
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
        """Получить текущий день марафона."""
        completed = self._get_completed_topics(user)
        return len(completed) // 2 + 1

    def _get_topics_today(self, user) -> int:
        """Получить количество тем за сегодня."""
        if isinstance(user, dict):
            return user.get('topics_today', 0)
        return getattr(user, 'topics_today', 0)

    async def enter(self, user, context: dict = None) -> None:
        """
        Показываем практическое задание.

        Context может содержать:
        - topic_index: индекс темы
        - from_bonus: пришли из бонусного вопроса
        - from_question: пришли из вопроса урока
        """
        lang = self._get_lang(user)
        marathon_day = self._get_marathon_day(user)

        # Показываем сообщение о загрузке
        await self.send(user, f"⏳ {t('marathon.preparing_practice', lang)}")

        # Генерация задания делегируется LLM клиенту
        # TODO: Интеграция с claude.generate_practice_intro()

        await self.send(
            user,
            f"✏️ *{t('marathon.day_practice', lang, day=marathon_day)}*\n\n"
            f"📋 *{t('marathon.task', lang)}:*\n"
            f"_Задание будет сгенерировано..._\n\n"
            f"🎯 *{t('marathon.work_product', lang)}:* Рабочий продукт\n\n"
            f"📝 *{t('marathon.when_complete', lang)}:*\n"
            f"{t('marathon.write_wp_name', lang)}\n\n"
            f"💬 *{t('marathon.waiting_for', lang)}:* {t('marathon.work_product_name', lang)}",
            parse_mode="Markdown"
        )

    async def handle(self, user, message: Message) -> Optional[str]:
        """
        Обрабатываем ответ с рабочим продуктом.

        Возвращает:
        - "submitted" → lesson (следующий урок)
        - "day_complete" → lesson (день завершён)
        - None → остаёмся (короткий ответ)
        """
        text = (message.text or "").strip()
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        # Вопрос к ИИ
        if text.startswith('?'):
            question_text = text[1:].strip()
            if question_text:
                # TODO: Обработка вопроса
                await self.send(
                    user,
                    f"_Ответ на ваш вопрос..._\n\n"
                    f"💬 *{t('marathon.waiting_for', lang)}:* {t('marathon.work_product_name', lang)}",
                    parse_mode="Markdown"
                )
            return None

        # Пропуск практики
        if "пропустить" in text.lower() or "skip" in text.lower():
            await self.send(user, t('marathon.practice_skipped', lang))
            return "day_complete"

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

        # Подтверждение
        await self.send(
            user,
            f"✅ *{t('marathon.practice_accepted', lang)}*\n\n"
            f"✅ {t('marathon.day_complete', lang)}",
            parse_mode="Markdown"
        )

        # Проверяем, есть ли ещё темы
        if len(completed) >= 28:
            return "day_complete"  # Марафон завершён

        return "submitted"  # Следующий день

    async def exit(self, user) -> dict:
        """Передаём контекст следующему стейту."""
        return {
            "day_completed": True,
            "topics_completed": len(self._get_completed_topics(user))
        }
