"""
Обработчики Telegram для режима Лента.

Содержит:
- Команда /feed - вход в режим
- Выбор тем на неделю
- Ежедневные сессии
- Приём фиксаций
"""

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import get_logger
from i18n import t
from helpers.message_split import prepare_html_parts
from helpers.typing_indicator import keep_typing
from .engine import FeedEngine
from db.queries.users import get_intern
from engines.shared import handle_question

logger = get_logger(__name__)


async def get_user_lang(chat_id: int) -> str:
    """Получает язык пользователя из профиля"""
    intern = await get_intern(chat_id)
    if intern:
        return intern.get('language', 'ru') or 'ru'
    return 'ru'

# Создаём роутер для Ленты
feed_router = Router(name="feed")


async def show_feed_menu(message: Message, engine: FeedEngine, state: FSMContext):
    """Показывает главное меню режима Лента с двумя кнопками"""
    try:
        chat_id = message.chat.id
        lang = await get_user_lang(chat_id)

        week = await engine.get_current_week()
        if not week:
            await message.answer(t('feed.use_command', lang))
            return

        topics = week.get('accepted_topics', [])

        # Формируем текст меню
        text = f"📚 *{t('feed.menu_title', lang)}*\n\n"

        if topics:
            text += f"{t('feed.your_topics_label', lang)}\n"
            for i, topic in enumerate(topics, 1):
                text += f"{i}. {topic}\n"
        else:
            text += f"{t('feed.no_topics', lang)}\n"

        # Кнопки
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"📖 {t('buttons.get_digest', lang)}",
                callback_data="feed_get_digest"
            )],
            [InlineKeyboardButton(
                text=f"📋 {t('buttons.topics_menu', lang)}",
                callback_data="feed_topics_menu"
            )]
        ])

        await state.clear()
        await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

    except Exception as e:
        import traceback
        logger.error(f"Ошибка в show_feed_menu: {e}\n{traceback.format_exc()}")
        await message.answer(t('errors.try_again', await get_user_lang(message.chat.id)))


class FeedStates(StatesGroup):
    """FSM состояния для режима Лента"""
    choosing_topics = State()       # Выбор тем на неделю
    reading_content = State()       # Читает контент сессии
    waiting_fixation = State()      # Ожидание фиксации
    choosing_tomorrow = State()     # Выбор/изменение темы на завтра
    editing_topic = State()         # Редактирование конкретной темы


# ==================== КОМАНДЫ ====================

@feed_router.message(Command("feed"))
async def cmd_feed(message: Message, state: FSMContext):
    """Команда /feed - вход в режим Лента"""
    try:
        chat_id = message.chat.id
        logger.info(f"cmd_feed вызван для {chat_id}")

        # Получаем язык пользователя
        intern = await get_intern(chat_id)
        lang = intern.get('language', 'ru') if intern else 'ru'

        engine = FeedEngine(chat_id)

        # Получаем статус
        logger.info(f"Получаем статус для {chat_id}")
        status = await engine.get_status()
        logger.info(f"Статус Ленты для {chat_id}: {status}")

        if status['week_status'] == 'active':
            # Есть активная неделя — показываем меню Ленты (дайджест)
            logger.info(f"Показываем меню Ленты для {chat_id}")
            await show_feed_menu(message, engine, state)

        elif status['week_status'] == 'planning':
            # Неделя в планировании — перегенерируем темы из каталога
            logger.info(f"Перегенерируем темы (planning) для {chat_id}")
            loading_msg = await message.answer(t('loading.generating_topics', lang))
            async with keep_typing(message):
                topics, msg = await engine.suggest_topics()
            await loading_msg.delete()
            if not topics:
                await message.answer(msg)
                return
            await show_topic_selection(message, topics, state)

        else:
            # Нет недели или завершена — запускаем новую
            loading_msg = await message.answer(t('loading.generating_topics', lang))

            async with keep_typing(message):
                logger.info(f"Запускаем feed для {chat_id}")
                success, msg = await engine.start_feed()
                if not success:
                    await loading_msg.delete()
                    await message.answer(msg)
                    return

                logger.info(f"Генерируем темы для {chat_id}")
                topics, msg = await engine.suggest_topics()

            await loading_msg.delete()
            if not topics:
                await message.answer(msg)
                return

            await show_topic_selection(message, topics, state)

    except Exception as e:
        import traceback
        logger.error(f"Ошибка в cmd_feed: {e}\n{traceback.format_exc()}")
        lang = await get_user_lang(message.chat.id)
        await message.answer(t('feed.load_error', lang))


def escape_markdown(text: str) -> str:
    """Экранирует специальные символы Markdown"""
    if not text:
        return ''
    # Экранируем символы, которые могут сломать Markdown
    for char in ['_', '*', '[', ']', '`']:
        text = text.replace(char, '\\' + char)
    return text


async def show_topic_selection(message: Message, topics: list, state: FSMContext):
    """Показывает интерфейс выбора тем"""
    try:
        logger.info(f"show_topic_selection: получено {len(topics)} тем")
        chat_id = message.chat.id
        lang = await get_user_lang(chat_id)

        # Сохраняем темы в state (используем list вместо set для JSON-сериализации)
        await state.update_data(suggested_topics=topics, selected_indices=[])
        await state.set_state(FeedStates.choosing_topics)

        text = f"📚 *{t('feed.suggested_topics', lang)}*\n\n"

        for i, topic in enumerate(topics):
            title = escape_markdown(topic.get('title', ''))
            why = escape_markdown(topic.get('why', ''))
            text += f"*{i+1}. {title}*\n"
            text += f"   _{why}_\n\n"

        text += "—\n"
        text += f"{t('feed.select_up_to_3', lang)}\n"
        text += f"{t('feed.select_hint', lang)}\n"
        text += f"_{t('feed.select_example', lang)}_"

        # Создаём кнопки
        buttons = []
        for i, topic in enumerate(topics):
            buttons.append([
                InlineKeyboardButton(
                    text=f"☐ {topic['title'][:30]}",
                    callback_data=f"feed_topic_{i}"
                )
            ])

        buttons.append([
            InlineKeyboardButton(text=f"✅ {t('buttons.yes', lang)}", callback_data="feed_confirm")
        ])
        buttons.append([
            InlineKeyboardButton(text=f"🔄 {t('buttons.other_topics', lang)}", callback_data="feed_reset_topics")
        ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")
        logger.info("show_topic_selection: сообщение отправлено")

    except Exception as e:
        import traceback
        logger.error(f"Ошибка в show_topic_selection: {e}\n{traceback.format_exc()}")
        await message.answer(t('errors.try_again', await get_user_lang(message.chat.id)))


async def show_topic_selection_direct(bot, chat_id: int, topics: list, state: FSMContext, lang: str = 'ru'):
    """Показывает интерфейс выбора тем напрямую через бота (без Message объекта)"""
    try:
        logger.info(f"show_topic_selection_direct: получено {len(topics)} тем для {chat_id}")

        # Сохраняем темы в state (используем list вместо set для JSON-сериализации)
        await state.update_data(suggested_topics=topics, selected_indices=[])
        await state.set_state(FeedStates.choosing_topics)

        text = f"📚 *{t('feed.suggested_topics', lang)}*\n\n"

        for i, topic in enumerate(topics):
            text += f"*{i+1}. {topic['title']}*\n"
            text += f"   _{topic.get('why', '')}_\n\n"

        text += "—\n"
        text += f"{t('feed.select_up_to_3', lang)}\n"
        text += f"{t('feed.select_hint', lang)}\n"
        text += f"_{t('feed.select_example', lang)}_"

        # Создаём кнопки
        buttons = []
        for i, topic in enumerate(topics):
            buttons.append([
                InlineKeyboardButton(
                    text=f"☐ {topic['title'][:30]}",
                    callback_data=f"feed_topic_{i}"
                )
            ])

        buttons.append([
            InlineKeyboardButton(text=f"✅ {t('buttons.yes', lang)}", callback_data="feed_confirm")
        ])
        buttons.append([
            InlineKeyboardButton(text=f"🔄 {t('buttons.other_topics', lang)}", callback_data="feed_reset_topics")
        ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="Markdown")
        logger.info("show_topic_selection_direct: сообщение отправлено")

    except Exception as e:
        import traceback
        logger.error(f"Ошибка в show_topic_selection_direct: {e}\n{traceback.format_exc()}")
        await bot.send_message(chat_id, t('errors.try_again', lang))


@feed_router.callback_query(F.data.startswith("feed_topic_"))
async def toggle_topic(callback: CallbackQuery, state: FSMContext):
    """Переключает выбор темы (максимум 3)"""
    data = await state.get_data()
    topics = data.get('suggested_topics', [])
    selected = list(data.get('selected_indices', []))  # Конвертируем в list для надёжности

    # Получаем индекс темы
    index = int(callback.data.replace("feed_topic_", ""))

    # Переключаем выбор
    if index in selected:
        selected.remove(index)
    else:
        # Проверяем лимит 3 темы
        if len(selected) >= 3:
            lang = await get_user_lang(callback.message.chat.id)
            await callback.answer(t('feed.max_3_topics', lang), show_alert=True)
            return
        selected.append(index)

    await state.update_data(selected_indices=selected)

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

    lang = await get_user_lang(callback.message.chat.id)
    buttons.append([
        InlineKeyboardButton(text=t('buttons.confirm_selection', lang), callback_data="feed_confirm")
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    try:
        await callback.message.edit_reply_markup(reply_markup=keyboard)
    except Exception:
        pass

    await callback.answer()


def parse_topic_selection(text: str, topics_count: int) -> tuple[set, list]:
    """Парсит текстовый выбор тем пользователя.

    Примеры:
    - "1, 3, 5" → выбраны темы 1, 3, 5
    - "тема 2 и ещё хочу про собранность" → тема 2 + кастомная "собранность"
    - "2, 4 и добавь внимание" → темы 2, 4 + кастомная "внимание"

    Returns:
        (selected_indices, custom_topics)
    """
    import re

    selected_indices = set()
    custom_topics = []

    # Ищем номера тем (1-5)
    numbers = re.findall(r'\b([1-5])\b', text)
    for num in numbers:
        idx = int(num) - 1
        if 0 <= idx < topics_count:
            selected_indices.add(idx)

    # Ищем кастомные темы после ключевых слов
    custom_patterns = [
        r'(?:хочу|добавь|ещё|еще|также)\s+(?:про\s+)?([а-яА-ЯёЁa-zA-Z\s]+?)(?:[,.]|$|\s+и\s+|\s+тема)',
        r'(?:и\s+)?про\s+([а-яА-ЯёЁa-zA-Z\s]+?)(?:[,.]|$)',
    ]

    for pattern in custom_patterns:
        matches = re.findall(pattern, text.lower())
        for match in matches:
            topic = match.strip()
            # Фильтруем короткие и числовые
            if len(topic) >= 3 and not topic.isdigit():
                # Убираем слова-маркеры
                topic = re.sub(r'^(тему?|темы)\s+', '', topic)
                if topic and len(topic) >= 3:
                    custom_topics.append(topic.capitalize())

    return selected_indices, custom_topics


@feed_router.message(FeedStates.choosing_topics, F.text.func(lambda t: not t.startswith('/')))
async def handle_topic_text_selection(message: Message, state: FSMContext):
    """Обрабатывает текстовый выбор тем"""
    try:
        text = message.text.strip()
        data = await state.get_data()
        topics = data.get('suggested_topics', [])
        chat_id = message.chat.id
        lang = await get_user_lang(chat_id)

        if not topics:
            lang = await get_user_lang(message.chat.id)
            await message.answer(t('feed.use_feed_first', lang))
            return

        # Парсим текст
        selected_indices, custom_topics = parse_topic_selection(text, len(topics))

        if not selected_indices and not custom_topics:
            await message.answer(
                f"{t('feed.select_hint', lang)}\n"
                f"_{t('feed.select_example', lang)}_",
                parse_mode="Markdown"
            )
            return

        # Собираем выбранные темы
        selected_titles = [topics[i]['title'] for i in sorted(selected_indices)]
        selected_titles.extend(custom_topics)

        # Ограничиваем до 3 тем
        if len(selected_titles) > 3:
            selected_titles = selected_titles[:3]
            await message.answer(t('feed.limited_to_3', lang), parse_mode="Markdown")

        # Принимаем темы
        engine = FeedEngine(chat_id)
        success, msg = await engine.accept_topics(selected_titles)

        if not success:
            await message.answer(msg)
            return

        # Очищаем state выбора тем
        await state.clear()

        # Показываем подтверждение с кнопками
        confirm_text = f"✅ {t('feed.topics_selected', lang)}\n\n"
        confirm_text += f"{t('feed.selected_topics', lang)}\n"
        for i, title in enumerate(selected_titles, 1):
            mark = "📌" if title in custom_topics else "✓"
            confirm_text += f"{mark} {title}\n"

        if custom_topics:
            confirm_text += f"\n_📌 — {t('feed.your_topics', lang)}_"

        confirm_text += f"\n\n{t('feed.what_next', lang)}"

        # Кнопки "Начать сейчас" / "По расписанию"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t('buttons.start_now', lang), callback_data="feed_start_now")],
            [InlineKeyboardButton(text=t('buttons.start_scheduled', lang), callback_data="feed_start_scheduled")]
        ])

        await message.answer(confirm_text, reply_markup=keyboard, parse_mode="Markdown")

    except Exception as e:
        import traceback
        logger.error(f"Ошибка в handle_topic_text_selection: {e}\n{traceback.format_exc()}")
        await message.answer(t('errors.try_again', await get_user_lang(message.chat.id)))


@feed_router.callback_query(F.data == "feed_confirm")
async def confirm_topics(callback: CallbackQuery, state: FSMContext):
    """Подтверждает выбор тем"""
    data = await state.get_data()
    topics = data.get('suggested_topics', [])
    selected = list(data.get('selected_indices', []))  # Конвертируем в list для надёжности
    chat_id = callback.message.chat.id
    lang = await get_user_lang(chat_id)

    if not selected:
        await callback.answer(t('feed.select_hint', lang), show_alert=True)
        return

    # Получаем названия выбранных тем
    selected_titles = [topics[i]['title'] for i in sorted(selected)]

    # Принимаем темы
    engine = FeedEngine(chat_id)
    success, msg = await engine.accept_topics(selected_titles)

    if not success:
        await callback.answer(msg, show_alert=True)
        return

    # Очищаем state выбора тем
    await state.clear()

    # Показываем подтверждение с кнопками
    confirm_text = f"✅ {t('feed.topics_selected', lang)}\n\n"
    confirm_text += f"{t('feed.selected_topics', lang)}\n"
    confirm_text += "\n".join([f"✓ {title}" for title in selected_titles])
    confirm_text += f"\n\n{t('feed.what_next', lang)}"

    # Кнопки "Начать сейчас" / "По расписанию"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('buttons.start_now', lang), callback_data="feed_start_now")],
        [InlineKeyboardButton(text=t('buttons.start_scheduled', lang), callback_data="feed_start_scheduled")]
    ])

    await callback.message.edit_text(confirm_text, reply_markup=keyboard)
    await callback.answer()


@feed_router.callback_query(F.data == "feed_start_now")
async def feed_start_now(callback: CallbackQuery, state: FSMContext):
    """Начать сейчас — показать генерацию и контент"""
    chat_id = callback.message.chat.id
    lang = await get_user_lang(chat_id)

    # Показываем сообщение о генерации
    await callback.message.edit_text(t('loading.generating_content', lang))
    await callback.answer()

    # Генерируем и показываем контент
    engine = FeedEngine(chat_id)
    await show_today_session(callback.message, engine, state)


@feed_router.callback_query(F.data == "feed_start_scheduled")
async def feed_start_scheduled(callback: CallbackQuery, state: FSMContext):
    """По расписанию — просто подтвердить"""
    chat_id = callback.message.chat.id
    lang = await get_user_lang(chat_id)

    await callback.message.edit_text(
        f"✅ {t('feed.topics_selected', lang)}\n\n"
        f"_{t('help.schedule_note', lang)}_",
        parse_mode="Markdown"
    )
    await callback.answer(t('feed.topic_saved', lang))


# ==================== МЕНЮ ЛЕНТЫ ====================

@feed_router.callback_query(F.data == "feed_start_topics")
async def feed_start_topics(callback: CallbackQuery, state: FSMContext):
    """Начинает выбор тем на неделю (из сообщения активации режима)"""
    chat_id = callback.message.chat.id
    lang = await get_user_lang(chat_id)

    await callback.answer()

    # Показываем индикатор загрузки
    loading_msg = await callback.message.edit_text(t('loading.generating_topics', lang))

    # Генерируем темы
    engine = FeedEngine(chat_id)
    success, msg = await engine.start_feed()

    if not success:
        await callback.message.edit_text(msg)
        return

    topics, msg = await engine.suggest_topics()

    if not topics:
        await callback.message.edit_text(msg)
        return

    # Удаляем сообщение загрузки и показываем выбор тем
    try:
        await callback.message.delete()
    except Exception:
        pass

    # Отправляем новое сообщение напрямую через бота
    await show_topic_selection_direct(callback.bot, chat_id, topics, state, lang)


@feed_router.callback_query(F.data == "feed_get_digest")
async def feed_get_digest(callback: CallbackQuery, state: FSMContext):
    """Получить дайджест - показывает текущую сессию или создаёт новую"""
    chat_id = callback.message.chat.id
    lang = await get_user_lang(chat_id)

    await callback.answer()

    # Показываем индикатор загрузки
    await callback.message.edit_text(t('loading.generating_content', lang))

    # Генерируем и показываем контент
    engine = FeedEngine(chat_id)
    await show_today_session(callback.message, engine, state)


@feed_router.callback_query(F.data == "feed_topics_menu")
async def feed_topics_menu(callback: CallbackQuery, state: FSMContext):
    """Показывает меню тем с возможностью редактирования"""
    chat_id = callback.message.chat.id
    lang = await get_user_lang(chat_id)

    try:
        engine = FeedEngine(chat_id)
        week = await engine.get_current_week()

        if not week:
            await callback.answer(t('errors.try_again', lang), show_alert=True)
            return

        topics = week.get('accepted_topics', [])

        # Формируем текст
        text = f"📋 *{t('feed.topics_menu_title', lang)}*\n\n"

        if topics:
            text += f"{t('feed.your_topics_label', lang)}\n"
            for i, topic in enumerate(topics, 1):
                text += f"{i}. {topic}\n"
        else:
            text += f"{t('feed.no_topics', lang)}\n"

        text += "\n—\n"
        text += f"{t('feed.change_topics', lang)}\n"
        text += f"{t('feed.enter_format', lang)}\n\n"
        text += f"{t('feed.depth_explanation', lang)}"

        # Кнопки
        buttons = [
            [InlineKeyboardButton(
                text=f"🔄 {t('buttons.reset_topics', lang)}",
                callback_data="feed_reset_topics"
            )],
            [InlineKeyboardButton(
                text=t('buttons.back_to_menu', lang),
                callback_data="feed_back_to_menu"
            )]
        ]

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        # Устанавливаем состояние для приёма новых тем
        await state.set_state(FeedStates.editing_topic)

        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        await callback.answer()

    except Exception as e:
        import traceback
        logger.error(f"Ошибка в feed_topics_menu: {e}\n{traceback.format_exc()}")
        await callback.answer(t('errors.try_again', lang), show_alert=True)


@feed_router.callback_query(F.data == "feed_reset_topics")
async def feed_reset_topics(callback: CallbackQuery, state: FSMContext):
    """Начинает выбор тем заново — генерирует новые предложения"""
    chat_id = callback.message.chat.id
    lang = await get_user_lang(chat_id)

    await callback.answer()

    # Показываем индикатор загрузки
    await callback.message.edit_text(t('loading.generating_topics', lang))

    # Генерируем новые темы
    engine = FeedEngine(chat_id)
    topics, msg = await engine.suggest_topics()

    if not topics:
        await callback.message.edit_text(msg)
        return

    # Показываем выбор тем
    try:
        await callback.message.delete()
    except Exception:
        pass

    await show_topic_selection(callback.message, topics, state)


@feed_router.message(FeedStates.editing_topic, F.text.func(lambda t: not t.startswith('/')))
async def handle_topic_edit(message: Message, state: FSMContext):
    """Обрабатывает ввод новых тем.

    Форматы:
    - Числа (1, 2, 3) — ссылки на существующие темы
    - Текст — новые темы
    - Комбинация: "1, 2, Собранность" → темы 1, 2 + новая "Собранность"
    """
    try:
        chat_id = message.chat.id
        lang = await get_user_lang(chat_id)
        text = message.text.strip()

        if len(text) < 1:
            await message.answer(t('feed.enter_topics', lang))
            return

        # Получаем текущие темы для ссылок по номерам
        engine = FeedEngine(chat_id)
        week = await engine.get_current_week()
        current_topics = week.get('accepted_topics', []) if week else []

        # Парсим темы: разделители — запятая или новая строка
        import re
        raw_items = re.split(r'[,\n]+', text)

        new_topics = []
        for item in raw_items:
            item = item.strip()
            if not item:
                continue

            # Убираем нумерацию в начале (1., 1 -, 1) и т.п.)
            clean_item = re.sub(r'^\d+[\.\)\-\s]+', '', item).strip()

            # Проверяем, это число (ссылка на существующую тему)?
            if item.isdigit():
                idx = int(item) - 1
                if 0 <= idx < len(current_topics):
                    topic = current_topics[idx]
                    if topic not in new_topics:
                        new_topics.append(topic)
                continue

            # Это текст — новая тема
            if clean_item and len(clean_item) >= 2:
                topic = clean_item.capitalize()
                if topic not in new_topics:
                    new_topics.append(topic)

        if not new_topics:
            await message.answer(t('feed.topics_not_recognized', lang))
            return

        # Ограничение: максимум 3 темы
        if len(new_topics) > 3:
            new_topics = new_topics[:3]
            await message.answer(t('feed.limited_to_3', lang), parse_mode="Markdown")

        # Обновляем темы
        success = await engine.set_topics(new_topics)

        if success:
            # Показываем подтверждение
            confirm_text = f"{t('feed.topics_updated', lang)}\n\n"
            for i, topic in enumerate(new_topics, 1):
                confirm_text += f"{i}. {topic}\n"

            await message.answer(confirm_text, parse_mode="Markdown")
        else:
            await message.answer(t('errors.try_again', lang))

        await state.clear()

        # Показываем меню Ленты
        await show_feed_menu(message, engine, state)

    except Exception as e:
        import traceback
        logger.error(f"Ошибка в handle_topic_edit: {e}\n{traceback.format_exc()}")
        await message.answer(t('errors.try_again', await get_user_lang(message.chat.id)))
        await state.clear()


@feed_router.callback_query(F.data == "feed_back_to_menu")
async def feed_back_to_menu(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню Ленты"""
    chat_id = callback.message.chat.id

    await callback.answer()

    engine = FeedEngine(chat_id)

    # Удаляем старое сообщение и показываем меню
    try:
        await callback.message.delete()
    except Exception:
        pass

    await show_feed_menu(callback.message, engine, state)


async def show_today_session(message: Message, engine: FeedEngine, state: FSMContext):
    """Показывает сегодняшний дайджест.

    Новая модель:
    - Заголовок "Дайджест" без номера дня
    - Список тем в подзаголовке
    - Уровень глубины показывается как "(углубление X)"
    """
    try:
        logger.info("show_today_session: получаем дайджест")

        # Показываем индикатор "печатает..." пока генерируем контент
        await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")

        session, intro_msg = await engine.get_today_session()

        if not session:
            await message.answer(intro_msg)
            return

        if session.get('status') == 'completed':
            await message.answer(f"✅ {intro_msg}")
            return

        # Получаем контент сессии (content может быть None в JSONB → or {} защищает)
        content = session.get('content') or {}
        topics_list = content.get('topics_list', [])
        depth_level = content.get('depth_level', session.get('day_number', 1))

        # Формируем заголовок
        lang = await get_user_lang(message.chat.id)
        if topics_list:
            topics_str = ", ".join(topics_list)
            text = t('feed.digest_header', lang, topics=topics_str) + "\n"
        else:
            # Fallback для старых сессий
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

        # Добавляем подсказку о возможности задать вопрос
        text += f"\n\n—\n💡 _{t('feed.ask_details', lang)}_"

        # Кнопки: фиксация и "что дальше?"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✍️ {t('buttons.write_fixation', lang)}", callback_data="feed_fixation")],
            [InlineKeyboardButton(text=f"📋 {t('feed.whats_next', lang)}", callback_data="feed_whats_next")]
        ])

        await state.set_state(FeedStates.reading_content)
        await state.update_data(session_id=session['id'])

        # Разбиваем длинные сообщения (по абзацам, не mid-entity)
        parts = prepare_html_parts(text)
        for i, part in enumerate(parts):
            is_last = (i == len(parts) - 1)
            kb = keyboard if is_last else None
            await message.answer(part, reply_markup=kb, parse_mode="HTML")

        logger.info("show_today_session: дайджест показан")

    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        logger.error(f"Ошибка в show_today_session: {e}\n{error_trace}")
        # Показываем ошибку для отладки
        trace_lines = error_trace.strip().split('\n')
        short_trace = '\n'.join(trace_lines[-6:])
        lang = await get_user_lang(message.chat.id)
        await message.answer(f"{t('errors.digest_load_error', lang)}:\n```\n{short_trace[:500]}\n```", parse_mode="Markdown")


@feed_router.message(FeedStates.reading_content, F.text.func(lambda t: not t.startswith('/')))
async def handle_feed_question(message: Message, state: FSMContext):
    """Обрабатывает вопрос пользователя во время чтения дайджеста.

    Новая модель:
    - Контекстом являются ВСЕ выбранные темы (не одна)
    """
    try:
        chat_id = message.chat.id
        lang = await get_user_lang(chat_id)
        question = message.text.strip()

        if len(question) < 3:
            return

        logger.info(f"Feed: вопрос от {chat_id}: {question[:50]}...")

        # Получаем все темы как контекст
        engine = FeedEngine(chat_id)
        week = await engine.get_current_week()
        context_topics = None
        if week:
            topics = week.get('accepted_topics', [])
            if topics:
                # Все темы объединяем как контекст
                context_topics = ", ".join(topics)

        # Получаем профиль пользователя
        intern = await get_intern(chat_id)

        # Обрабатываем вопрос
        await message.answer(t('shared.thinking', lang))

        async with keep_typing(message):
            answer, sources = await handle_question(
                question=question,
                intern=intern,
                context_topic=context_topics
            )

        # Формируем ответ
        response = answer
        if sources:
            response += "\n\n📚 _Источники: " + ", ".join(sources[:2]) + "_"

        await message.answer(response, parse_mode="Markdown")

    except Exception as e:
        import traceback
        logger.error(f"Ошибка в handle_feed_question: {e}\n{traceback.format_exc()}")
        await message.answer(t('shared.question_error', lang))


@feed_router.callback_query(F.data == "feed_whats_next")
async def show_whats_next(callback: CallbackQuery, state: FSMContext):
    """Показывает текущие темы"""
    chat_id = callback.message.chat.id
    lang = await get_user_lang(chat_id)

    try:
        engine = FeedEngine(chat_id)
        week = await engine.get_current_week()

        if not week:
            await callback.answer(t('errors.try_again', lang), show_alert=True)
            return

        topics = week.get('accepted_topics', [])

        # Формируем список тем
        text = f"📋 *{t('feed.topics_menu_title', lang)}*\n\n"

        if topics:
            text += f"{t('feed.your_topics_label', lang)}\n"
            for i, topic in enumerate(topics, 1):
                text += f"{i}. {topic}\n"
            text += f"\n{t('feed.topics_deepen_daily', lang)}"
        else:
            text += f"{t('feed.no_topics', lang)}"

        # Кнопка для редактирования тем
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"✏️ {t('buttons.topics_menu', lang)}",
                callback_data="feed_topics_menu"
            )]
        ])

        await callback.message.answer(text, reply_markup=keyboard, parse_mode="Markdown")
        await callback.answer()

    except Exception as e:
        import traceback
        logger.error(f"Ошибка в show_whats_next: {e}\n{traceback.format_exc()}")
        await callback.answer(t('errors.try_again', lang), show_alert=True)


@feed_router.callback_query(F.data == "feed_fixation")
async def start_fixation(callback: CallbackQuery, state: FSMContext):
    """Начинает процесс фиксации"""
    lang = await get_user_lang(callback.message.chat.id)
    await state.set_state(FeedStates.waiting_fixation)

    await callback.message.answer(
        f"✍️ *{t('feed.fixation_title', lang)}*\n\n"
        f"{t('feed.fixation_instruction', lang)}\n\n"
        f"_{t('feed.fixation_hint', lang)}_",
        parse_mode="Markdown"
    )
    await callback.answer()


@feed_router.message(FeedStates.waiting_fixation, F.text.func(lambda t: not t.startswith('/')))
async def receive_fixation(message: Message, state: FSMContext):
    """Принимает фиксацию пользователя"""
    text = message.text.strip()
    lang = await get_user_lang(message.chat.id)

    if len(text) < 10:
        await message.answer(t('feed.fixation_too_short', lang))
        return

    chat_id = message.chat.id
    engine = FeedEngine(chat_id)

    success, msg = await engine.submit_fixation(text)

    if success:
        # Показываем статистику
        stats = await engine.get_week_summary()

        stat_text = (
            f"✅ {msg}\n\n"
            f"📊 *{t('progress.statistics', lang)}*\n"
            f"• {t('feed.active_days_label', lang)}: {stats.get('total_active_days', 0)}\n"
            f"• {t('feed.current_streak', lang)}: {stats.get('current_streak', 0)} {t('progress.days', lang)}"
        )
        await message.answer(stat_text, parse_mode="Markdown")

        await state.clear()

        # Показываем меню Ленты
        await show_feed_menu(message, engine, state)
    else:
        await message.answer(f"❌ {msg}")
        await state.clear()


async def show_tomorrow_topics(message: Message, engine: FeedEngine, state: FSMContext):
    """Показывает предложенные темы на завтра"""
    from .planner import suggest_weekly_topics

    try:
        chat_id = message.chat.id
        lang = await get_user_lang(chat_id)

        week = await engine.get_current_week()
        if not week or week.get('status') == 'completed':
            await state.clear()
            return

        topics = week.get('accepted_topics', [])
        current_day = week.get('current_day', 1)

        # Если все темы пройдены - не показываем
        if current_day > len(topics):
            await state.clear()
            return

        # Текущая тема на завтра (из плана)
        tomorrow_topic = topics[current_day - 1] if current_day <= len(topics) else None

        if not tomorrow_topic:
            await state.clear()
            return

        # Генерируем альтернативные предложения
        intern = await engine.get_intern()
        suggested = await suggest_weekly_topics(intern)

        # Сохраняем в state для обработки
        await state.update_data(
            tomorrow_day=current_day,
            current_tomorrow_topic=tomorrow_topic,
            suggested_topics=suggested
        )
        await state.set_state(FeedStates.choosing_tomorrow)

        # Формируем сообщение
        text = f"\n📅 *{t('feed.tomorrow_planned', lang)}*\n"
        text += f"➡️ {tomorrow_topic}\n\n"
        text += f"*{t('feed.alternative_topics', lang)}*\n"

        for i, topic in enumerate(suggested[:5], 1):
            text += f"{i}. {topic['title']}\n"
            text += f"   _{topic.get('why', '')}_\n"

        text += "\n—\n"
        text += t('feed.keep_or_change', lang)

        # Кнопки
        buttons = [
            [InlineKeyboardButton(text=f"✅ {t('buttons.keep_topic', lang)}", callback_data="feed_keep_tomorrow")]
        ]
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

    except Exception as e:
        import traceback
        logger.error(f"Ошибка в show_tomorrow_topics: {e}\n{traceback.format_exc()}")
        await state.clear()


@feed_router.callback_query(F.data == "feed_keep_tomorrow")
async def keep_tomorrow_topic(callback: CallbackQuery, state: FSMContext):
    """Оставляет текущую тему на завтра"""
    chat_id = callback.message.chat.id
    lang = await get_user_lang(chat_id)

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer(f"👍 {t('feed.topic_saved', lang)}")
    await state.clear()


@feed_router.message(FeedStates.choosing_tomorrow, F.text.func(lambda t: not t.startswith('/')))
async def handle_tomorrow_selection(message: Message, state: FSMContext):
    """Обрабатывает выбор/изменение темы на завтра"""
    import re

    try:
        text = message.text.strip()
        data = await state.get_data()
        suggested = data.get('suggested_topics', [])
        tomorrow_day = data.get('tomorrow_day', 1)

        chat_id = message.chat.id
        lang = await get_user_lang(chat_id)
        engine = FeedEngine(chat_id)

        new_topic = None

        # Проверяем, это номер темы?
        match = re.match(r'^([1-5])$', text)
        if match:
            idx = int(match.group(1)) - 1
            if 0 <= idx < len(suggested):
                new_topic = suggested[idx]['title']

        # Иначе — это кастомная тема
        if not new_topic and len(text) >= 3:
            # Капитализируем и обрезаем до 5 слов
            words = text.split()[:5]
            new_topic = ' '.join(words).capitalize()

        if not new_topic:
            await message.answer(
                t('feed.select_hint', lang),
                parse_mode="Markdown"
            )
            return

        # Обновляем тему на завтра
        success = await engine.update_tomorrow_topic(tomorrow_day, new_topic)

        if success:
            await message.answer(
                f"✅ {t('feed.topic_changed', lang)}\n➡️ *{new_topic}*",
                parse_mode="Markdown"
            )
        else:
            await message.answer(t('errors.try_again', lang))

        await state.clear()

    except Exception as e:
        import traceback
        logger.error(f"Ошибка в handle_tomorrow_selection: {e}\n{traceback.format_exc()}")
        await message.answer(t('errors.try_again', await get_user_lang(message.chat.id)))
        await state.clear()


# ==================== СТАТУС ====================

@feed_router.message(Command("feed_status"))
async def cmd_feed_status(message: Message):
    """Показывает статус Ленты.

    Новая модель:
    - current_day = глубина погружения (не номер темы)
    - Все темы в каждом дайджесте
    """
    try:
        chat_id = message.chat.id
        logger.info(f"cmd_feed_status вызван для {chat_id}")
        lang = await get_user_lang(chat_id)
        engine = FeedEngine(chat_id)

        status = await engine.get_status()
        logger.info(f"Статус для {chat_id}: {status}")

        if not status['feed_active']:
            await message.answer(
                f"📚 *{t('feed.mode_title', lang)}*\n\n"
                f"{t('feed.not_active', lang)}",
                parse_mode="Markdown"
            )
            return

        text = f"📚 *{t('feed.mode_title', lang)}*\n\n"

        if status['has_week']:
            text += f"📅 {t('feed.status_label', lang)}: {status['week_status']}\n"
            depth = status['current_day']
            text += f"📖 {t('feed.depth_level', lang)}: {depth}\n"

            if status['topics']:
                text += f"\n{t('feed.your_topics_count', lang, count=len(status['topics']))}\n"
                for i, topic in enumerate(status['topics'], 1):
                    text += f"• {topic}\n"
                text += f"\n{t('feed.topics_deepen_each_digest', lang)}\n"

        text += f"\n📊 *{t('progress.statistics', lang)}:*\n"
        text += f"• {t('feed.total_active_days', lang)}: {status['active_days']}\n"
        text += f"• {t('feed.current_streak', lang)}: {status['streak']} {t('progress.days', lang)}"

        await message.answer(text, parse_mode="Markdown")

    except Exception as e:
        import traceback
        logger.error(f"Ошибка в cmd_feed_status: {e}\n{traceback.format_exc()}")
        lang = await get_user_lang(message.chat.id)
        await message.answer(t('feed.status_load_error', lang))
