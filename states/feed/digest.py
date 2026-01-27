"""
Стейт: Показ дайджеста Ленты.

Вход: после выбора тем (feed.topics)
Выход:
  - feed.topics (смена тем или новая неделя)
  - common.mode_select (выход из Ленты)
"""

from typing import Optional

from aiogram.types import Message

from states.base import BaseState
from i18n import t
from db.queries import update_intern, moscow_today


class FeedDigestState(BaseState):
    """
    Стейт показа дайджеста.

    Показывает дайджест по выбранным темам, ждёт фиксацию.
    """

    name = "feed.digest"
    display_name = {"ru": "Дайджест", "en": "Digest"}
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

    def _get_depth_level(self, user) -> int:
        """Получить текущий уровень глубины."""
        if isinstance(user, dict):
            return user.get('feed_depth_level', 1)
        return getattr(user, 'feed_depth_level', 1)

    def _get_accepted_topics(self, user) -> list:
        """Получить выбранные темы недели."""
        if isinstance(user, dict):
            return user.get('feed_accepted_topics', [])
        return getattr(user, 'feed_accepted_topics', [])

    async def enter(self, user, context: dict = None) -> None:
        """
        Показываем дайджест.

        Context может содержать:
        - from_topics: пришли после выбора тем
        - depth_level: текущий уровень глубины
        """
        lang = self._get_lang(user)
        context = context or {}

        depth = context.get('depth_level', self._get_depth_level(user))
        topics = self._get_accepted_topics(user)
        topics_str = ", ".join(topics) if topics else "Общие темы"

        # Показываем индикатор генерации
        await self.send(user, f"⏳ {t('feed.generating_digest', lang)}")

        # Генерация дайджеста делегируется LLM клиенту
        # TODO: Интеграция с engines/feed/engine.py

        # Название уровня глубины
        depth_names = {
            1: t('feed.depth_basic', lang),
            2: t('feed.depth_practical', lang),
            3: t('feed.depth_integration', lang),
        }
        depth_name = depth_names.get(depth, t('feed.depth_deep', lang))

        await self.send(
            user,
            f"📖 *{t('feed.digest_title', lang)}*\n"
            f"_{topics_str}_\n\n"
            f"📊 *{t('feed.depth_level', lang)}:* {depth} — {depth_name}\n\n"
            f"_Контент дайджеста будет сгенерирован..._\n\n"
            f"━━━━━━━━━━━━━━━━\n\n"
            f"💭 *{t('feed.reflection_question', lang)}*\n"
            f"_Как эти идеи связаны с вашей работой?_\n\n"
            f"💬 *{t('feed.waiting_for', lang)}:* {t('feed.fixation', lang)}",
            parse_mode="Markdown"
        )

    async def handle(self, user, message: Message) -> Optional[str]:
        """
        Обрабатываем ответ пользователя.

        Returns:
        - "fixation_saved" → _same (следующий дайджест)
        - "change_topics" → topics (смена тем)
        - "done" → mode_select (выход)
        - None → остаёмся (короткий ответ или вопрос)
        """
        text = (message.text or "").strip()
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        # Вопрос к ИИ
        if text.startswith('?'):
            question = text[1:].strip()
            if question:
                await self.send(
                    user,
                    f"_Ответ на ваш вопрос..._\n\n"
                    f"💬 *{t('feed.waiting_for', lang)}:* {t('feed.fixation', lang)}",
                    parse_mode="Markdown"
                )
            return None

        # Команды
        if text.lower() in ["новая неделя", "new week", "темы", "topics"]:
            await self.send(user, t('feed.changing_topics', lang))
            return "change_topics"

        if text.lower() in ["выход", "exit", "готово", "done"]:
            await self.send(user, t('feed.exit', lang))
            return "done"

        # Слишком короткая фиксация
        if len(text) < 10:
            await self.send(
                user,
                f"{t('feed.fixation_too_short', lang)}\n\n"
                f"💬 *{t('feed.waiting_for', lang)}:* {t('feed.fixation', lang)}",
                parse_mode="Markdown"
            )
            return None

        # Сохраняем фиксацию
        # TODO: Сохранить в feed_sessions
        # await save_fixation(chat_id, text)

        depth = self._get_depth_level(user)
        today = moscow_today()

        # Обновляем прогресс
        if chat_id:
            await update_intern(
                chat_id,
                feed_depth_level=depth + 1,
                last_feed_date=today
            )

        # Подтверждение
        await self.send(
            user,
            f"✅ *{t('feed.fixation_saved', lang)}*\n\n"
            f"📊 *{t('feed.stats', lang)}:*\n"
            f"🎯 {t('feed.depth_reached', lang)}: {depth}\n"
            f"📅 {t('feed.next_digest', lang)}",
            parse_mode="Markdown"
        )

        return "fixation_saved"

    async def exit(self, user) -> dict:
        """Передаём контекст."""
        return {
            "digest_completed": True,
            "depth_level": self._get_depth_level(user)
        }
