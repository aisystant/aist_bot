"""
Стейт: Бонусный вопрос Марафона.

Вход: после правильного ответа на вопрос урока (если bloom_level >= 2)
Выход: workshop.marathon.task (задание)

Логика:
- Уровень 1: бонус НЕ предлагается (сразу задание)
- Уровни 2 и 3: бонус предлагается (можно отказаться)
"""

import logging
from typing import Optional

from aiogram.types import Message, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton

from states.base import BaseState
from i18n import t
from helpers.message_split import prepare_html_parts
from db.queries import update_intern, save_answer
from clients.claude import claude
from core.knowledge import get_topic

logger = logging.getLogger(__name__)


class MarathonBonusState(BaseState):
    """
    Стейт бонусного вопроса Марафона.

    Предлагает ученику вопрос повышенной сложности.
    Необязательный — можно отказаться и сразу перейти к заданию.
    """

    name = "workshop.marathon.bonus"
    display_name = {"ru": "Бонусный вопрос", "en": "Bonus Question", "es": "Pregunta extra", "fr": "Question bonus"}
    keyboard_type = "reply"
    allow_global = ["consultation", "notes"]

    # Тексты кнопок
    YES_BUTTONS = ["🚀 Да, давай сложнее!", "🚀 Yes, harder!", "🚀 Sí, más difícil", "🚀 Oui, plus difficile!"]
    NO_BUTTONS = ["✅ Достаточно", "✅ Enough", "✅ Suficiente", "✅ Suffisant"]
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
        """Получить уровень сложности пользователя."""
        if isinstance(user, dict):
            return user.get('complexity_level', 1) or user.get('bloom_level', 1) or 1
        return getattr(user, 'complexity_level', 1) or getattr(user, 'bloom_level', 1) or 1

    def _get_current_topic_index(self, user) -> int:
        """Получить индекс текущей темы."""
        if isinstance(user, dict):
            return user.get('current_topic_index', 0)
        return getattr(user, 'current_topic_index', 0)

    def _user_to_intern_dict(self, user) -> dict:
        """Конвертировать user объект в dict для Claude."""
        if isinstance(user, dict):
            return user
        return {
            'chat_id': getattr(user, 'chat_id', None),
            'language': getattr(user, 'language', 'ru'),
            'bloom_level': getattr(user, 'bloom_level', 1),
            'complexity_level': getattr(user, 'complexity_level', 1),
            'occupation': getattr(user, 'occupation', ''),
            'interests': getattr(user, 'interests', ''),
            'goals': getattr(user, 'goals', ''),
        }

    async def enter(self, user, context: dict = None) -> None:
        """
        Предлагаем бонусный вопрос.

        Context может содержать:
        - topic_index: индекс текущей темы
        - topic_name: название темы
        - bloom_level: текущий уровень сложности
        """
        lang = self._get_lang(user)
        context = context or {}

        # Показываем кнопки выбора бонусного вопроса
        yes_btn = t('buttons.bonus_yes', lang)
        no_btn = t('buttons.bonus_no', lang)

        buttons = [[yes_btn], [no_btn]]
        await self.send_with_keyboard(
            user,
            t('marathon.want_harder', lang),
            buttons
        )

    async def handle(self, user, message: Message) -> Optional[str]:
        """
        Обрабатываем выбор/ответ пользователя.

        Возможные сценарии:
        1. Пользователь нажал "Да" — генерируем вопрос и ждём ответ
        2. Пользователь нажал "Нет" — переходим к заданию
        3. Пользователь отвечает на вопрос — проверяем и переходим к заданию
        """
        text = (message.text or "").strip()
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        # Пользователь хочет бонусный вопрос
        if self._is_yes_button(text, lang):
            # FIX: Удаляем Reply Keyboard при переходе к вопросу
            await self.send_remove_keyboard(user, f"⏳ {t('marathon.generating_harder', lang)}")

            try:
                # Получаем данные для генерации
                topic_index = self._get_current_topic_index(user)
                # Для бонуса берём предыдущую тему (theory), т.к. current уже указывает на practice
                theory_index = topic_index - 1 if topic_index > 0 else topic_index
                topic = get_topic(theory_index)

                if not topic:
                    logger.error(f"Topic not found for bonus question: index={theory_index}")
                    await self.send(user, t('errors.question_generation_failed', lang))
                    return "no"  # Переходим к заданию

                bloom_level = self._get_bloom_level(user)
                next_level = min(bloom_level + 1, 3)
                intern = self._user_to_intern_dict(user)

                logger.info(f"Generating bonus question: topic={theory_index}, level={next_level}, user={chat_id}")

                # Генерируем вопрос повышенной сложности
                question = await claude.generate_question(
                    topic=topic,
                    intern=intern,
                    bloom_level=next_level
                )

                if not question:
                    logger.warning(f"Bonus question generation returned None for user {chat_id}")
                    await self.send(
                        user,
                        f"⚠️ {t('errors.question_generation_failed', lang)}\n\n"
                        f"_{t('marathon.loading_practice', lang)}_",
                        parse_mode="Markdown"
                    )
                    return "no"  # Переходим к заданию

                # Показываем вопрос
                bonus_text = (
                    f"🚀 *{t('marathon.bonus_question', lang)}* ({t(f'bloom.level_{next_level}_short', lang)})\n\n"
                    f"{question}\n\n"
                    f"_{t('marathon.write_answer', lang)}_"
                )
                parts = prepare_html_parts(bonus_text)
                for part in parts:
                    await self.send(user, part, parse_mode="HTML")

                logger.info(f"Bonus question sent to user {chat_id}, parts: {len(parts)}, waiting for answer")
                return None  # Остаёмся в стейте, ждём ответ

            except Exception as e:
                logger.error(f"Error generating bonus question for user {chat_id}: {e}")
                await self.send(
                    user,
                    f"⚠️ {t('errors.question_generation_failed', lang)}\n\n"
                    f"_{t('marathon.loading_practice', lang)}_",
                    parse_mode="Markdown"
                )
                return "no"  # Переходим к заданию при ошибке

        # Пользователь отказался
        if self._is_no_button(text, lang):
            # Удаляем Reply Keyboard и показываем кнопку «Получить практику»
            practice_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"✏️ {t('buttons.get_practice', lang)}",
                    callback_data="marathon_get_practice"
                )]
            ])
            await self.send_remove_keyboard(user, t('marathon.loading_practice', lang))
            await self.send(user, "👇", reply_markup=practice_keyboard)
            return None  # ждём клик

        # Настройки — переход в настройки
        if text in self.SETTINGS_BUTTONS or "настройки" in text.lower() or "settings" in text.lower():
            return "settings"

        # Это ответ на бонусный вопрос (текст минимум 20 символов)
        if len(text) >= 20:
            # Получаем индекс темы для сохранения ответа
            topic_index = self._get_current_topic_index(user)
            theory_index = topic_index - 1 if topic_index > 0 else topic_index

            # Сохраняем ответ
            if chat_id:
                await save_answer(
                    chat_id=chat_id,
                    topic_index=theory_index,
                    answer=f"[BONUS] {text}",
                    answer_type="bonus_answer",
                    complexity_level=self._get_bloom_level(user)
                )

            # FIX: Удаляем Reply Keyboard при переходе к практике
            await self.send(
                user,
                f"🌟 *{t('marathon.bonus_completed', lang)}*\n\n"
                f"{t('marathon.training_skills', lang)} *{t(f'bloom.level_{self._get_bloom_level(user)}_short', lang)}* {t('marathon.and_higher', lang)}",
                parse_mode="Markdown",
                reply_markup=ReplyKeyboardRemove()
            )
            # Кнопка «Получить практику»
            practice_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"✏️ {t('buttons.get_practice', lang)}",
                    callback_data="marathon_get_practice"
                )]
            ])
            await self.send(user, "👇", reply_markup=practice_keyboard)
            return None  # ждём клик

        # Слишком короткий ответ — показываем ожидание
        await self.send(user, f"{t('marathon.waiting_for', lang)}: {t('marathon.answer_expected', lang)}")
        return None  # Остаёмся в стейте

    def _is_yes_button(self, text: str, lang: str) -> bool:
        """Проверяем, нажал ли пользователь кнопку 'Да'."""
        text_lower = text.lower()
        yes_btn = t('buttons.bonus_yes', lang).lower()

        if text_lower == yes_btn:
            return True
        if text_lower in [b.lower() for b in self.YES_BUTTONS]:
            return True
        # Проверка ключевых слов только для коротких сообщений (кнопки < 30 символов)
        # Иначе "создаёшь" и другие слова с "да" триггерят ложное срабатывание
        if len(text) < 30:
            if "да" in text_lower or "yes" in text_lower or "harder" in text_lower:
                return True

        return False

    def _is_no_button(self, text: str, lang: str) -> bool:
        """Проверяем, нажал ли пользователь кнопку 'Нет'."""
        text_lower = text.lower()
        no_btn = t('buttons.bonus_no', lang).lower()

        if text_lower == no_btn:
            return True
        if text_lower in [b.lower() for b in self.NO_BUTTONS]:
            return True
        # Проверка ключевых слов только для коротких сообщений (кнопки < 30 символов)
        if len(text) < 30:
            if "достаточно" in text_lower or "enough" in text_lower:
                return True

        return False

    async def handle_callback(self, user, callback) -> Optional[str]:
        """Обработка inline-кнопки «Получить практику»."""
        if callback.data == "marathon_get_practice":
            await callback.answer()
            return "answered"  # SM перейдёт в task
        return None

    async def exit(self, user) -> dict:
        """Передаём контекст следующему стейту (заданию)."""
        return {"from_bonus": True}
