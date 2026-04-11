"""
Стейт: Выбор тем для Ленты.

Вход: из common.mode_select (выбор "Лента") или после завершения недели
Выход: feed.digest (после выбора тем)
"""

import asyncio
import re
from typing import Optional, List, Dict

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ReplyKeyboardRemove

from states.base import BaseState
from i18n import t
from db.queries.users import get_intern, update_intern
from db.queries.feed import (
    create_feed_week,
    get_current_feed_week,
    update_feed_week,
    delete_feed_sessions,
)
from engines.feed.planner import suggest_weekly_topics
from config import get_logger, Mode, FeedStatus, FeedWeekStatus

logger = get_logger(__name__)

# Таймаут на генерацию тем (секунды)
TOPICS_GENERATION_TIMEOUT = 60


class FeedTopicsState(BaseState):
    """
    Стейт выбора тем на неделю.

    Генерирует персонализированные темы через Claude API
    и позволяет пользователю выбрать 1-3 темы.
    """

    name = "feed.topics"
    display_name = {
        "ru": "Выбор тем Ленты",
        "en": "Feed Topics Selection",
        "es": "Selección de temas",
        "fr": "Sélection des sujets"
    }
    allow_global = ["consultation", "notes"]

    def _get_lang(self, user) -> str:
        """Получить язык пользователя."""
        if isinstance(user, dict):
            return user.get('language', 'ru') or 'ru'
        return getattr(user, 'language', 'ru') or 'ru'

    def _user_to_intern_dict(self, user) -> dict:
        """Конвертировать user в dict для совместимости."""
        if isinstance(user, dict):
            return user
        return {
            'chat_id': getattr(user, 'chat_id', None),
            'language': getattr(user, 'language', 'ru'),
            'name': getattr(user, 'name', ''),
            'occupation': getattr(user, 'occupation', ''),
            'interests': getattr(user, 'interests', []),
            'goals': getattr(user, 'goals', ''),
            'motivation': getattr(user, 'motivation', ''),
        }

    def _escape_markdown(self, text: str) -> str:
        """Экранирует специальные символы Markdown."""
        if not text:
            return ''
        for char in ['_', '*', '[', ']', '`']:
            text = text.replace(char, '\\' + char)
        return text

    async def enter(self, user, context: dict = None) -> Optional[str]:
        """
        Показываем интерфейс выбора тем.

        1. Проверяем, есть ли активная неделя
        2. Если нет — генерируем темы
        3. Показываем интерфейс выбора

        Context:
            force_regenerate: если True — сбрасываем ACTIVE неделю и генерируем новые темы

        Returns:
            "topics_selected" если уже есть активная неделя, None иначе
        """
        lang = self._get_lang(user)
        chat_id = self._get_chat_id_from_user(user)
        intern = self._user_to_intern_dict(user)
        context = context or {}

        # Обновляем режим пользователя (derive_mode учитывает marathon_status)
        from db.queries.users import derive_mode
        marathon_status = intern.get('marathon_status', 'not_started')
        await update_intern(chat_id, mode=derive_mode(marathon_status, FeedStatus.ACTIVE), feed_status=FeedStatus.ACTIVE)

        # Проверяем текущую неделю
        week = await get_current_feed_week(chat_id)

        # force_regenerate: сбрасываем ACTIVE неделю для перегенерации тем
        if context.get('force_regenerate') and week and week.get('status') == FeedWeekStatus.ACTIVE:
            logger.info(f"force_regenerate: resetting week {week['id']} to PLANNING for chat_id={chat_id}")
            await update_feed_week(week['id'], {
                'status': FeedWeekStatus.PLANNING,
                'accepted_topics': [],
                'suggested_topics': [],
            })
            week['status'] = FeedWeekStatus.PLANNING
            week['accepted_topics'] = []
            week['suggested_topics'] = []

        if week and week.get('status') == FeedWeekStatus.ACTIVE:
            # Уже есть активная неделя — переходим к дайджесту
            return "topics_selected"

        # Генерируем новые темы (из каталога — мгновенно)
        await self.send(user, t('loading.generating_topics', lang), reply_markup=ReplyKeyboardRemove())

        try:
            topics = await asyncio.wait_for(
                suggest_weekly_topics(intern),
                timeout=TOPICS_GENERATION_TIMEOUT
            )

            if not topics:
                await self.send(user, t('feed.topics_generation_failed', lang))
                return "skip"

            # Сохраняем темы в неделю
            # Если есть существующая неделя в PLANNING — обновляем её,
            # иначе создаём новую
            if week and week.get('status') == FeedWeekStatus.PLANNING:
                await update_feed_week(week['id'], {
                    'suggested_topics': [topic['title'] for topic in topics],
                    'accepted_topics': [],
                })
            else:
                await create_feed_week(
                    chat_id=chat_id,
                    suggested_topics=[topic['title'] for topic in topics],
                    accepted_topics=[],
                )

            # Показываем выбор
            await self._show_topic_selection(user, topics)
            return None

        except asyncio.TimeoutError:
            logger.error(f"Topics generation timeout for user {chat_id}")
            await self.send(user, t('errors.generation_timeout', lang))
            return "skip"
        except Exception as e:
            logger.error(f"Error generating topics for user {chat_id}: {e}")
            await self.send(user, t('errors.try_again', lang))
            return "skip"

    async def _show_topic_selection(self, user, topics: List[Dict]) -> None:
        """Показывает интерфейс выбора тем."""
        lang = self._get_lang(user)

        # Сохраняем темы в БД (переживает Railway redeploy)
        await self.save_state(user, {
            'suggested_topics': topics,
            'selected_indices': []
        })

        # Формируем текст
        text = f"📚 *{t('feed.suggested_topics', lang)}*\n\n"

        for i, topic in enumerate(topics):
            title = self._escape_markdown(topic.get('title', ''))
            why = self._escape_markdown(topic.get('why', ''))
            text += f"*{i+1}. {title}*\n"
            if why:
                text += f"   _{why}_\n"
            text += "\n"

        text += "—\n"
        text += f"{t('feed.select_up_to_3', lang)}\n"
        text += f"{t('feed.select_hint', lang)}\n"
        text += f"_{t('feed.select_example', lang)}_"

        # Кнопки выбора тем
        buttons = []
        for i, topic in enumerate(topics):
            buttons.append([
                InlineKeyboardButton(
                    text=f"☐ {topic['title'][:30]}",
                    callback_data=f"feed_topic_{i}"
                )
            ])

        buttons.append([
            InlineKeyboardButton(
                text=t('buttons.confirm_selection', lang),
                callback_data="feed_confirm"
            )
        ])
        buttons.append([
            InlineKeyboardButton(
                text=f"🔄 {t('buttons.other_topics', lang)}",
                callback_data="feed_reset_topics"
            )
        ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")

    async def handle(self, user, message: Message) -> Optional[str]:
        """
        Обрабатываем текстовый выбор тем.

        Форматы:
        - "1, 3, 5" — выбор по номерам
        - "тема 2 и собранность" — номера + кастомные
        """
        text = (message.text or "").strip()
        lang = self._get_lang(user)
        chat_id = self._get_chat_id_from_user(user)

        if text.startswith('/'):
            # Команды обрабатываются отдельно
            return None

        # Получаем сохранённые темы
        data = await self.load_state(user)
        topics = data.get('suggested_topics', [])

        if not topics:
            await self.send(user, t('feed.use_feed_first', lang))
            return None

        # Парсим выбор
        selected_indices, custom_topics = self._parse_topic_selection(text, len(topics))

        if not selected_indices and not custom_topics:
            await self.send(
                user,
                f"{t('feed.select_hint', lang)}\n"
                f"_{t('feed.select_example', lang)}_",
                parse_mode="Markdown"
            )
            return None

        # Собираем выбранные темы
        selected_titles = [topics[i]['title'] for i in sorted(selected_indices)]
        selected_titles.extend(custom_topics)

        # Ограничиваем до 3 тем
        if len(selected_titles) > 3:
            selected_titles = selected_titles[:3]
            await self.send(user, t('feed.limited_to_3', lang))

        # Принимаем темы
        success = await self._accept_topics(chat_id, selected_titles, lang)

        if success:
            await self.clear_state(user)
            return "topics_selected"

        return None

    def _parse_topic_selection(self, text: str, topics_count: int) -> tuple:
        """Парсит текстовый выбор тем.

        Стратегия: извлекаем номера тем (1-5), а весь оставшийся текст
        после очистки интент-фраз и стоп-слов = одна кастомная тема.

        Примеры:
        - "Я хочу знать об организации знания" → "Организации знания"
        - "Хочу узнать про системное мышление" → "Системное мышление"
        - "1, 3 и ещё про коммуникации" → indices {0,2} + "Коммуникации"
        - "Расскажи о собранности" → "Собранности"
        """
        selected_indices = set()
        custom_topics = []

        # 1. Извлекаем номера тем (1-5)
        numbers = re.findall(r'\b([1-5])\b', text)
        for num in numbers:
            idx = int(num) - 1
            if 0 <= idx < topics_count:
                selected_indices.add(idx)

        # 2. Извлекаем кастомную тему из оставшегося текста
        remaining = text.lower()
        # Убираем номера
        remaining = re.sub(r'\b[1-5]\b', '', remaining)

        # Убираем интент-фразы целиком (длинные → короткие)
        intent_phrases = [
            r'\bя\s+(?:хочу|хотела?\s+бы)\s+(?:знать|узнать|изучить|изучать|понять|разобраться)\s+(?:о(?:б)?\s+|про\s+)?',
            r'\b(?:хочу|хотела?\s+бы)\s+(?:знать|узнать|изучить|изучать|понять|разобраться)\s+(?:о(?:б)?\s+|про\s+)?',
            r'\bя\s+(?:хочу|хотела?\s+бы)\s+(?:о(?:б)?\s+|про\s+)?',
            r'\b(?:хочу|хотела?\s+бы)\s+(?:о(?:б)?\s+|про\s+)?',
            r'\bмне\s+интересн[оа]\s+(?:тема\s+)?',
            r'\bинтересует\s+(?:тема\s+)?',
            r'\bрасскажи\s+(?:мне\s+)?(?:о(?:б)?\s+|про\s+)?',
        ]
        for pattern in intent_phrases:
            remaining = re.sub(pattern, '', remaining)

        # Убираем оставшиеся стоп-слова
        remaining = re.sub(r'\b(хочу|добавь|ещё|еще|также|тему|тема|темы)\b', '', remaining)
        # "про то, как/что/чтобы" → убираем обёртку, оставляем суть
        remaining = re.sub(r'\bпро\s+то\s*,?\s*(?:как|что|чтобы)\s+', '', remaining)
        # Убираем "про" отдельно стоящее
        remaining = re.sub(r'\bпро\s+', '', remaining)
        # Чистим пробелы и пунктуацию (перед проверкой краёв)
        remaining = re.sub(r'\s+', ' ', remaining).strip(' ,.')
        # Убираем ведущие предлоги "о", "об", "в" (после очистки интентов)
        remaining = re.sub(r'^(?:о(?:б)?|в)\s+', '', remaining)
        # Убираем союзы и "я" на краях
        remaining = re.sub(r'^(и|или|а|я)\s+', '', remaining)
        remaining = re.sub(r'\s+(и|или|а)$', '', remaining)
        # Финальная очистка
        remaining = remaining.strip(' ,.')

        if remaining and len(remaining) >= 3:
            custom_topics.append(remaining.capitalize())

        return selected_indices, custom_topics

    async def _accept_topics(self, chat_id: int, titles: List[str], lang: str) -> bool:
        """Принимает выбранные темы."""
        try:
            week = await get_current_feed_week(chat_id)
            if not week:
                return False

            # Удаляем незавершённые сессии (темы сменились → старые активные невалидны)
            # keep_completed=True: completed сессии сохраняются для дневного лимита
            await delete_feed_sessions(week['id'], keep_completed=True)

            # Обновляем неделю
            await update_feed_week(week['id'], {
                'accepted_topics': titles,
                'status': FeedWeekStatus.ACTIVE,
                'current_day': 1,
            })

            # Показываем подтверждение
            confirm_text = f"✅ {t('feed.topics_selected', lang)}\n\n"
            confirm_text += f"{t('feed.selected_topics', lang)}\n"
            confirm_text += "\n".join([f"✓ {title}" for title in titles])

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"📖 {t('buttons.get_digest', lang)}",
                    callback_data="feed_get_digest"
                )]
            ])

            await self.bot.send_message(chat_id, confirm_text, reply_markup=keyboard)
            return True

        except Exception as e:
            logger.error(f"Error accepting topics: {e}")
            return False

    async def handle_callback(self, user, callback: CallbackQuery) -> Optional[str]:
        """
        Обрабатываем нажатия кнопок.

        Вызывается из роутера callback-ов.
        """
        data = callback.data
        chat_id = self._get_chat_id_from_user(user)
        lang = self._get_lang(user)

        user_data = await self.load_state(user)
        topics = user_data.get('suggested_topics', [])
        selected = list(user_data.get('selected_indices', []))

        if data.startswith("feed_topic_"):
            # Переключение выбора темы
            index = int(data.replace("feed_topic_", ""))

            if index in selected:
                selected.remove(index)
            else:
                if len(selected) >= 3:
                    await callback.answer(t('feed.max_3_topics', lang), show_alert=True)
                    return None
                selected.append(index)

            # Обновляем данные в БД
            await self.save_state(user, {**user_data, 'selected_indices': selected})

            # Обновляем кнопки
            buttons = []
            for i, topic in enumerate(topics):
                mark = "☑" if i in selected else "☐"
                buttons.append([
                    InlineKeyboardButton(
                        text=f"{mark} {topic['title'][:30]}",
                        callback_data=f"feed_topic_{i}"
                    )
                ])

            buttons.append([
                InlineKeyboardButton(
                    text=t('buttons.confirm_selection', lang),
                    callback_data="feed_confirm"
                )
            ])

            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

            try:
                await callback.message.edit_reply_markup(reply_markup=keyboard)
            except Exception:
                pass

            await callback.answer()
            return None

        elif data == "feed_confirm":
            # Подтверждение выбора
            if not selected:
                await callback.answer(t('feed.select_hint', lang), show_alert=True)
                return None

            # Получаем названия выбранных тем
            selected_titles = [topics[i]['title'] for i in sorted(selected)]

            # Принимаем темы
            success = await self._accept_topics(chat_id, selected_titles, lang)

            if success:
                await self.clear_state(user)
                await callback.answer()
                return "topics_selected"

            await callback.answer(t('errors.try_again', lang), show_alert=True)
            return None

        elif data == "feed_get_digest":
            # Переход к дайджесту
            await callback.answer()
            return "topics_selected"

        return None

    async def exit(self, user) -> dict:
        """Передаём контекст следующему стейту."""
        await self.clear_state(user)
        return {}
