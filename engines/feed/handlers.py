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
from .engine import FeedEngine
from db.queries.users import get_intern
from engines.shared import handle_question

logger = get_logger(__name__)

# Создаём роутер для Ленты
feed_router = Router(name="feed")


class FeedStates(StatesGroup):
    """FSM состояния для режима Лента"""
    choosing_topics = State()      # Выбор тем на неделю
    reading_content = State()      # Читает контент сессии
    waiting_fixation = State()     # Ожидание фиксации


# ==================== КОМАНДЫ ====================

@feed_router.message(Command("feed"))
async def cmd_feed(message: Message, state: FSMContext):
    """Команда /feed - вход в режим Лента"""
    try:
        chat_id = message.chat.id
        logger.info(f"cmd_feed вызван для {chat_id}")
        engine = FeedEngine(chat_id)

        # Получаем статус
        logger.info(f"Получаем статус для {chat_id}")
        status = await engine.get_status()
        logger.info(f"Статус Ленты для {chat_id}: {status}")

        if not status['has_week'] or status['week_status'] == 'completed':
            # Нужно предложить новые темы
            logger.info(f"Запускаем feed для {chat_id}")
            success, msg = await engine.start_feed()
            if not success:
                await message.answer(msg)
                return

            logger.info(f"Генерируем темы для {chat_id}")
            topics, msg = await engine.suggest_topics()
            if not topics:
                await message.answer(msg)
                return

            # Показываем темы для выбора
            await show_topic_selection(message, topics, state)

        elif status['week_status'] == 'planning':
            # Показываем уже предложенные темы (не создаём новую неделю!)
            logger.info(f"Показываем выбор тем (planning) для {chat_id}")
            week = await engine.get_current_week()
            if week and week.get('suggested_topics'):
                # Преобразуем названия тем в формат для отображения
                topics = [{'title': t, 'description': '', 'why': ''} for t in week['suggested_topics']]
                await show_topic_selection(message, topics, state)
            else:
                # Если тем нет, генерируем новые
                topics, msg = await engine.suggest_topics()
                await show_topic_selection(message, topics, state)

        else:
            # Есть активная неделя - показываем сессию
            logger.info(f"Показываем сессию для {chat_id}")
            await show_today_session(message, engine, state)

    except Exception as e:
        import traceback
        logger.error(f"Ошибка в cmd_feed: {e}\n{traceback.format_exc()}")
        await message.answer("Произошла ошибка при загрузке Ленты. Попробуйте позже.")


async def show_topic_selection(message: Message, topics: list, state: FSMContext):
    """Показывает интерфейс выбора тем"""
    try:
        logger.info(f"show_topic_selection: получено {len(topics)} тем")
        # Сохраняем темы в state
        await state.update_data(suggested_topics=topics, selected_indices=set())
        await state.set_state(FeedStates.choosing_topics)

        text = "📚 *Темы на эту неделю*\n\n"
        text += "Выберите интересующие темы (нажмите для выбора/отмены):\n\n"

        for i, topic in enumerate(topics):
            text += f"*{i+1}. {topic['title']}*\n"
            text += f"_{topic.get('description', '')}_ \n"
            text += f"💡 {topic.get('why', '')}\n\n"

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
            InlineKeyboardButton(text="✅ Подтвердить выбор", callback_data="feed_confirm")
        ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")
        logger.info("show_topic_selection: сообщение отправлено")

    except Exception as e:
        import traceback
        logger.error(f"Ошибка в show_topic_selection: {e}\n{traceback.format_exc()}")
        await message.answer("Произошла ошибка при отображении тем. Попробуйте позже.")


@feed_router.callback_query(F.data.startswith("feed_topic_"))
async def toggle_topic(callback: CallbackQuery, state: FSMContext):
    """Переключает выбор темы"""
    data = await state.get_data()
    topics = data.get('suggested_topics', [])
    selected = data.get('selected_indices', set())

    # Получаем индекс темы
    index = int(callback.data.replace("feed_topic_", ""))

    # Переключаем выбор
    if index in selected:
        selected.discard(index)
    else:
        selected.add(index)

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

    buttons.append([
        InlineKeyboardButton(text="✅ Подтвердить выбор", callback_data="feed_confirm")
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    try:
        await callback.message.edit_reply_markup(reply_markup=keyboard)
    except Exception:
        pass

    await callback.answer()


@feed_router.callback_query(F.data == "feed_confirm")
async def confirm_topics(callback: CallbackQuery, state: FSMContext):
    """Подтверждает выбор тем"""
    data = await state.get_data()
    topics = data.get('suggested_topics', [])
    selected = data.get('selected_indices', set())

    if not selected:
        await callback.answer("Выберите хотя бы одну тему!", show_alert=True)
        return

    # Получаем названия выбранных тем
    selected_titles = [topics[i]['title'] for i in sorted(selected)]

    # Принимаем темы
    chat_id = callback.message.chat.id
    engine = FeedEngine(chat_id)
    success, msg = await engine.accept_topics(selected_titles)

    if not success:
        await callback.answer(msg, show_alert=True)
        return

    # Очищаем state и показываем сегодняшнюю сессию
    await state.clear()

    await callback.message.edit_text(
        f"✅ {msg}\n\n"
        f"Выбрано тем: {len(selected_titles)}\n"
        + "\n".join([f"• {t}" for t in selected_titles])
    )

    # Показываем первую сессию
    await show_today_session(callback.message, engine, state)


async def show_today_session(message: Message, engine: FeedEngine, state: FSMContext):
    """Показывает сегодняшнюю сессию"""
    try:
        logger.info("show_today_session: получаем сессию")
        session, intro_msg = await engine.get_today_session()

        if not session:
            await message.answer(intro_msg)
            return

        if session.get('status') == 'completed':
            await message.answer(f"✅ {intro_msg}")
            return

        # Показываем контент сессии
        content = session.get('content', {})
        topic = session.get('topic_title', 'Тема дня')
        day = session.get('day_number', 1)

        text = f"📖 *День {day}: {topic}*\n\n"

        if content.get('intro'):
            text += f"_{content['intro']}_\n\n"

        text += content.get('main_content', 'Контент недоступен.')

        if content.get('reflection_prompt'):
            text += f"\n\n💭 *{content['reflection_prompt']}*"

        # Кнопка для фиксации
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Написать фиксацию", callback_data="feed_fixation")]
        ])

        await state.set_state(FeedStates.reading_content)
        await state.update_data(session_id=session['id'])

        # Разбиваем длинные сообщения
        if len(text) > 4000:
            # Отправляем частями
            parts = [text[i:i+4000] for i in range(0, len(text), 4000)]
            for i, part in enumerate(parts):
                if i == len(parts) - 1:
                    await message.answer(part, reply_markup=keyboard, parse_mode="Markdown")
                else:
                    await message.answer(part, parse_mode="Markdown")
        else:
            await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

        logger.info("show_today_session: сессия показана")

    except Exception as e:
        import traceback
        logger.error(f"Ошибка в show_today_session: {e}\n{traceback.format_exc()}")
        await message.answer("Произошла ошибка при загрузке сессии. Попробуйте позже.")


@feed_router.message(FeedStates.reading_content)
async def handle_feed_question(message: Message, state: FSMContext):
    """Обрабатывает вопрос пользователя во время чтения контента"""
    try:
        chat_id = message.chat.id
        question = message.text.strip()

        if len(question) < 3:
            return

        logger.info(f"Feed: вопрос от {chat_id}: {question[:50]}...")

        # Получаем контекст из state
        data = await state.get_data()
        session_id = data.get('session_id')

        # Получаем текущую тему
        engine = FeedEngine(chat_id)
        week = await engine.get_current_week()
        current_topic = None
        if week:
            topics = week.get('accepted_topics', [])
            current_day = week.get('current_day', 1)
            if topics and current_day <= len(topics):
                current_topic = topics[current_day - 1]

        # Получаем профиль пользователя
        intern = await get_intern(chat_id)

        # Обрабатываем вопрос
        await message.answer("💭 Думаю над ответом...")

        answer, sources = await handle_question(
            question=question,
            intern=intern,
            context_topic=current_topic
        )

        # Формируем ответ
        response = answer
        if sources:
            response += "\n\n📚 _Источники: " + ", ".join(sources[:2]) + "_"

        await message.answer(response, parse_mode="Markdown")

    except Exception as e:
        import traceback
        logger.error(f"Ошибка в handle_feed_question: {e}\n{traceback.format_exc()}")
        await message.answer("Не удалось обработать вопрос. Попробуйте позже.")


@feed_router.callback_query(F.data == "feed_fixation")
async def start_fixation(callback: CallbackQuery, state: FSMContext):
    """Начинает процесс фиксации"""
    await state.set_state(FeedStates.waiting_fixation)

    await callback.message.answer(
        "✍️ *Фиксация дня*\n\n"
        "Напишите краткую фиксацию: что вы поняли, "
        "какие мысли возникли, как это связано с вашей жизнью.\n\n"
        "_Достаточно 2-3 предложения._",
        parse_mode="Markdown"
    )
    await callback.answer()


@feed_router.message(FeedStates.waiting_fixation)
async def receive_fixation(message: Message, state: FSMContext):
    """Принимает фиксацию пользователя"""
    text = message.text.strip()

    if len(text) < 10:
        await message.answer(
            "Напишите хотя бы пару предложений для фиксации.",
        )
        return

    chat_id = message.chat.id
    engine = FeedEngine(chat_id)

    success, msg = await engine.submit_fixation(text)

    if success:
        # Показываем статистику
        stats = await engine.get_week_summary()

        await message.answer(
            f"✅ {msg}\n\n"
            f"📊 *Статистика*\n"
            f"• Активных дней: {stats.get('total_active_days', 0)}\n"
            f"• Текущая серия: {stats.get('current_streak', 0)} дней",
            parse_mode="Markdown"
        )
    else:
        await message.answer(f"❌ {msg}")

    await state.clear()


# ==================== СТАТУС ====================

@feed_router.message(Command("feed_status"))
async def cmd_feed_status(message: Message):
    """Показывает статус Ленты"""
    try:
        chat_id = message.chat.id
        logger.info(f"cmd_feed_status вызван для {chat_id}")
        engine = FeedEngine(chat_id)

        status = await engine.get_status()
        logger.info(f"Статус для {chat_id}: {status}")

        if not status['feed_active']:
            await message.answer(
                "📚 *Режим Лента*\n\n"
                "Лента не активна. Используйте /feed для запуска.",
                parse_mode="Markdown"
            )
            return

        text = "📚 *Режим Лента*\n\n"

        if status['has_week']:
            text += f"📅 Статус недели: {status['week_status']}\n"
            text += f"📖 День: {status['current_day']} / {len(status['topics'])}\n"

            if status['topics']:
                text += "\n*Темы недели:*\n"
                for i, topic in enumerate(status['topics'], 1):
                    mark = "✅" if i < status['current_day'] else "📖" if i == status['current_day'] else "⏳"
                    text += f"{mark} {topic}\n"

        text += f"\n📊 *Статистика:*\n"
        text += f"• Всего активных дней: {status['active_days']}\n"
        text += f"• Текущая серия: {status['streak']} дней"

        await message.answer(text, parse_mode="Markdown")

    except Exception as e:
        import traceback
        logger.error(f"Ошибка в cmd_feed_status: {e}\n{traceback.format_exc()}")
        await message.answer("Произошла ошибка при загрузке статуса. Попробуйте позже.")
