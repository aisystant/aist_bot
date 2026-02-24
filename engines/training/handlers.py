"""
Обработчики Telegram для режима Тренировка (WP-55).

Содержит:
- Команда /train — вход в режим
- Первоначальная настройка (когнитивный уровень + принципы)
- Дашборд с прогрессом
- Задание → ответ → оценка
- Настройки
"""

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import (
    get_logger,
    ZP_PRINCIPLES,
    TRAINING_COGNITIVE_LEVELS,
    TRAINING_MIN_ANSWER_LENGTH,
    TRAINING_MAX_DEPTH,
)
from i18n import t
from db.queries.users import get_intern
from db.queries.training import get_principle_depth
from .engine import TrainingEngine

logger = get_logger(__name__)

# Создаём роутер для Тренировки
training_router = Router(name="training")


class TrainingStates(StatesGroup):
    """FSM-состояния режима Тренировка."""
    setup_cognitive = State()
    setup_mode = State()       # Выбор режима: вперемешку / по порядку / конкретный ZP / конкретный FPF
    setup_principles = State()  # Выбор конкретного принципа (если выбран ZP/FPF)
    dashboard = State()
    viewing_assignment = State()
    waiting_answer = State()
    settings = State()
    settings_cognitive = State()
    settings_principles = State()


# ============= PROGRESS BAR =============

def _progress_bar(current: int, total: int) -> str:
    """Визуальный прогресс-бар: [████░] 4/5"""
    filled = '█' * current
    empty = '░' * (total - current)
    return f"[{filled}{empty}] {current}/{total}"


# ============= ENTRY POINT =============

@training_router.message(Command("train"))
async def cmd_train(message: Message, state: FSMContext):
    """Команда /train — вход в тренировку."""
    chat_id = message.chat.id
    engine = TrainingEngine(chat_id)

    if not await engine.get_intern():
        lang = 'ru'
        await message.answer(t('profile.not_found', lang))
        return

    if not await engine.is_setup_complete():
        await show_cognitive_selection(message, state)
        return

    await show_dashboard(message, engine, state)


# ============= FIRST-TIME SETUP =============

async def show_cognitive_selection(message: Message, state: FSMContext):
    """Показать выбор когнитивного уровня."""
    buttons = []
    for key, label in TRAINING_COGNITIVE_LEVELS.items():
        buttons.append([InlineKeyboardButton(
            text=label,
            callback_data=f"train_cognitive_{key}"
        )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(
        "🧠 *Тренировка принципов мышления*\n\n"
        "Выберите когнитивный уровень тренируемого:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await state.set_state(TrainingStates.setup_cognitive)


@training_router.callback_query(F.data.startswith("train_cognitive_"))
async def handle_cognitive_choice(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора когнитивного уровня."""
    current_state = await state.get_state()
    level = callback.data.replace("train_cognitive_", "")

    if level not in TRAINING_COGNITIVE_LEVELS:
        await callback.answer("Неизвестный уровень", show_alert=True)
        return

    await state.update_data(cognitive_level=level)

    # Если это setup — показать выбор режима тренировки
    if current_state == TrainingStates.setup_cognitive.state:
        await show_training_mode_selection(callback.message, state)
    else:
        # Из настроек — обновить и вернуться
        engine = TrainingEngine(callback.message.chat.id)
        await engine.update_cognitive_level(level)
        await callback.answer(f"Уровень: {TRAINING_COGNITIVE_LEVELS[level]}")
        await show_dashboard(callback.message, engine, state, edit=True)

    await callback.answer()


# ============= TRAINING MODE SELECTION =============

async def show_training_mode_selection(message: Message, state: FSMContext):
    """Показать 4 режима тренировки после выбора когнитивного уровня."""
    buttons = [
        [InlineKeyboardButton(
            text="🔀 Все вперемешку",
            callback_data="train_mode_shuffle"
        )],
        [InlineKeyboardButton(
            text="📶 По порядку из непройденных",
            callback_data="train_mode_sequential"
        )],
        [InlineKeyboardButton(
            text="🔹 Выбрать нулевой принцип (ZP)",
            callback_data="train_mode_pick_zp"
        )],
        [InlineKeyboardButton(
            text="🔸 Выбрать первый принцип (FPF)",
            callback_data="train_mode_pick_fpf"
        )],
    ]

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    text = (
        "📋 *Как тренироваться?*\n\n"
        "🔀 *Все вперемешку* — случайный принцип из непройденных\n"
        "📶 *По порядку* — от первого непройденного к последнему\n"
        "🔹 *Нулевой принцип* — выбрать конкретный ZP\n"
        "🔸 *Первый принцип* — выбрать конкретный FPF (скоро)"
    )

    try:
        await message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception:
        await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

    await state.set_state(TrainingStates.setup_mode)


@training_router.callback_query(F.data == "train_mode_shuffle")
async def handle_mode_shuffle(callback: CallbackQuery, state: FSMContext):
    """Режим: все вперемешку — включить все ZP принципы."""
    await state.update_data(training_mode='shuffle', selected_principles=list(ZP_PRINCIPLES))
    await _finalize_setup(callback, state)


@training_router.callback_query(F.data == "train_mode_sequential")
async def handle_mode_sequential(callback: CallbackQuery, state: FSMContext):
    """Режим: по порядку — включить все ZP принципы."""
    await state.update_data(training_mode='sequential', selected_principles=list(ZP_PRINCIPLES))
    await _finalize_setup(callback, state)


@training_router.callback_query(F.data == "train_mode_pick_zp")
async def handle_mode_pick_zp(callback: CallbackQuery, state: FSMContext):
    """Режим: выбрать конкретный нулевой принцип."""
    from .planner import get_principle_name

    buttons = []
    for pid in ZP_PRINCIPLES:
        name = get_principle_name(pid)
        buttons.append([InlineKeyboardButton(
            text=f"{pid} {name}",
            callback_data=f"train_pick_{pid}"
        )])
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="train_mode_back")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(
        "🔹 *Выберите нулевой принцип:*",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()


@training_router.callback_query(F.data == "train_mode_pick_fpf")
async def handle_mode_pick_fpf(callback: CallbackQuery, state: FSMContext):
    """Режим: первые принципы (FPF) — пока не реализовано."""
    await callback.answer("Первые принципы (FPF) будут добавлены позже.", show_alert=True)


@training_router.callback_query(F.data.startswith("train_pick_"))
async def handle_pick_principle(callback: CallbackQuery, state: FSMContext):
    """Выбран конкретный принцип."""
    principle_id = callback.data.replace("train_pick_", "")
    if principle_id in ZP_PRINCIPLES:
        await state.update_data(training_mode='single', selected_principles=[principle_id])
        await _finalize_setup(callback, state)
    else:
        await callback.answer("Неизвестный принцип", show_alert=True)


@training_router.callback_query(F.data == "train_mode_back")
async def handle_mode_back(callback: CallbackQuery, state: FSMContext):
    """Назад к выбору режима тренировки."""
    await show_training_mode_selection(callback.message, state)
    await callback.answer()


async def _finalize_setup(callback: CallbackQuery, state: FSMContext):
    """Финализировать настройку и показать дашборд."""
    data = await state.get_data()
    cognitive_level = data.get('cognitive_level', 'postformal')
    selected = data.get('selected_principles', list(ZP_PRINCIPLES))

    engine = TrainingEngine(callback.message.chat.id)
    success = await engine.setup(cognitive_level, selected)

    if not success:
        await callback.answer("Ошибка сохранения настроек", show_alert=True)
        return

    await callback.answer("Настройки сохранены!")
    await show_dashboard(callback.message, engine, state, edit=True)


async def show_principle_selection(message: Message, state: FSMContext):
    """Показать выбор принципов (для настроек)."""
    from .planner import get_principle_name

    data = await state.get_data()
    selected = data.get('selected_principles', list(ZP_PRINCIPLES))

    buttons = []
    for pid in ZP_PRINCIPLES:
        name = get_principle_name(pid)
        check = '✅' if pid in selected else '⬜'
        buttons.append([InlineKeyboardButton(
            text=f"{check} {pid} {name}",
            callback_data=f"train_toggle_{pid}"
        )])

    # Кнопка "Все"
    all_selected = len(selected) == len(ZP_PRINCIPLES)
    buttons.append([InlineKeyboardButton(
        text="☑️ Все" if all_selected else "⬜ Все",
        callback_data="train_toggle_all"
    )])

    # Кнопка подтверждения
    buttons.append([InlineKeyboardButton(
        text="✅ Подтвердить",
        callback_data="train_confirm_principles"
    )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    text = "Выберите принципы для тренировки:"
    try:
        await message.edit_text(text, reply_markup=keyboard)
    except Exception:
        await message.answer(text, reply_markup=keyboard)

    await state.set_state(TrainingStates.setup_principles)


@training_router.callback_query(F.data.startswith("train_toggle_"))
async def handle_principle_toggle(callback: CallbackQuery, state: FSMContext):
    """Переключение принципа вкл/выкл при настройке."""
    data = await state.get_data()
    selected = data.get('selected_principles', list(ZP_PRINCIPLES))
    target = callback.data.replace("train_toggle_", "")

    if target == "all":
        if len(selected) == len(ZP_PRINCIPLES):
            selected = [ZP_PRINCIPLES[0]]  # Нельзя убрать все — оставить хотя бы один
        else:
            selected = list(ZP_PRINCIPLES)
    elif target in ZP_PRINCIPLES:
        if target in selected:
            if len(selected) > 1:
                selected.remove(target)
            else:
                await callback.answer("Нельзя отключить все принципы", show_alert=True)
                return
        else:
            selected.append(target)

    await state.update_data(selected_principles=selected)
    await show_principle_selection(callback.message, state)
    await callback.answer()


@training_router.callback_query(F.data == "train_confirm_principles")
async def handle_confirm_principles(callback: CallbackQuery, state: FSMContext):
    """Подтвердить изменение принципов из настроек."""
    data = await state.get_data()
    selected = data.get('selected_principles', list(ZP_PRINCIPLES))

    from db.queries.training import update_training_settings
    await update_training_settings(callback.message.chat.id, enabled_principles=selected)
    await callback.answer("Принципы обновлены!")

    engine = TrainingEngine(callback.message.chat.id)
    await show_dashboard(callback.message, engine, state, edit=True)


# ============= DASHBOARD =============

async def show_dashboard(
    message: Message,
    engine: TrainingEngine,
    state: FSMContext,
    edit: bool = False,
):
    """Показать дашборд с прогрессом."""
    data = await engine.get_dashboard_data()
    if not data:
        await message.answer("Ошибка загрузки дашборда.")
        return

    principles = data.get('principles', [])
    stats = data.get('stats', {})
    cognitive_label = data.get('cognitive_label', 'Взрослый')

    # Собрать текст дашборда
    lines = [
        "🧠 *Тренировка принципов*",
        f"Уровень: {cognitive_label}",
        "━━━━━━━━━━━━━━━━━━",
    ]

    buttons = []
    for p in principles:
        if not p['enabled']:
            continue
        bar = _progress_bar(p['current_depth'], p['max_depth'])
        status = "✅" if p['completed'] else ""
        lines.append(f"{p['id']} {p['name']}  {bar} {status}")

        if not p['completed']:
            buttons.append([InlineKeyboardButton(
                text=f"📝 {p['id']} {p['name']}",
                callback_data=f"train_start_{p['id']}"
            )])

    lines.append("━━━━━━━━━━━━━━━━━━")

    total = stats.get('total_attempts', 0)
    passed = stats.get('total_passed', 0)
    if total > 0:
        lines.append(f"Попыток: {total} | Пройдено: {passed}")

    # Кнопка настроек
    buttons.append([InlineKeyboardButton(
        text="⚙️ Настройки",
        callback_data="train_settings"
    )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = '\n'.join(lines)

    try:
        if edit:
            await message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        else:
            await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception:
        await message.answer(text, reply_markup=keyboard, parse_mode=None)

    await state.set_state(TrainingStates.dashboard)


# ============= ASSIGNMENT =============

@training_router.callback_query(F.data.startswith("train_start_"))
async def handle_start_assignment(callback: CallbackQuery, state: FSMContext):
    """Начать задание по выбранному принципу."""
    principle_id = callback.data.replace("train_start_", "")
    engine = TrainingEngine(callback.message.chat.id)

    await callback.answer("Генерирую задание...")

    try:
        assignment = await engine.generate_assignment(principle_id)
    except Exception as e:
        logger.error(f"[Training] generate_assignment error: {e}")
        assignment = None

    if not assignment:
        from .planner import get_principle_name
        p_name = get_principle_name(principle_id)
        depth = await get_principle_depth(callback.message.chat.id, principle_id)
        if depth >= TRAINING_MAX_DEPTH:
            error_text = f"✅ {p_name} — пройден полностью ({depth}/{TRAINING_MAX_DEPTH})."
        else:
            error_text = f"⚠️ Не удалось загрузить задание для {p_name}. Попробуйте позже."
        await callback.message.edit_text(
            error_text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="← Назад", callback_data="train_back")
            ]])
        )
        return

    # Сохранить данные задания в FSM
    await state.update_data(
        current_principle=principle_id,
        current_depth=assignment['depth'],
        assignment_text=assignment['assignment_text'],
    )

    # Собрать текст
    bloom_label = assignment.get('bloom_level', '')
    lines = [
        f"📝 *{assignment['principle_name']}* — Глубина {assignment['depth']} ({bloom_label})",
    ]

    if assignment.get('bridge_text'):
        lines.append(f"\n💡 {assignment['bridge_text']}")

    lines.append(f"\n{assignment['assignment_text']}")
    lines.append("\n✏️ Напишите ваш ответ:")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="← Назад", callback_data="train_back")
    ]])

    text = '\n'.join(lines)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=None)

    await state.set_state(TrainingStates.waiting_answer)


# ============= ANSWER EVALUATION =============

@training_router.message(TrainingStates.waiting_answer, F.text)
async def receive_answer(message: Message, state: FSMContext):
    """Принять ответ и оценить."""
    answer_text = message.text.strip()

    if len(answer_text) < TRAINING_MIN_ANSWER_LENGTH:
        await message.answer(
            f"Ответ слишком короткий. Напишите развёрнутый ответ (минимум {TRAINING_MIN_ANSWER_LENGTH} символов)."
        )
        return

    data = await state.get_data()
    principle_id = data.get('current_principle')
    depth = data.get('current_depth')
    assignment_text = data.get('assignment_text', '')

    if not principle_id or not depth:
        await message.answer("Ошибка: потеряны данные задания. Используйте /train заново.")
        await state.clear()
        return

    # Показать "думаю..."
    thinking_msg = await message.answer("🔍 Оцениваю ответ...")

    engine = TrainingEngine(message.chat.id)
    result = await engine.evaluate_answer(
        principle_id, depth, assignment_text, answer_text
    )

    # Удалить "думаю..."
    try:
        await thinking_msg.delete()
    except Exception:
        pass

    # Показать результат
    if result.get('passed'):
        new_depth = result.get('new_depth', depth)
        if new_depth >= TRAINING_MAX_DEPTH:
            text = f"🎉 *Принцип пройден полностью!*\n\nГлубина {new_depth}/{TRAINING_MAX_DEPTH}"
        else:
            text = f"✅ *Принято!* Глубина {new_depth}/{TRAINING_MAX_DEPTH}"
        if result.get('feedback'):
            text += f"\n\n{result['feedback']}"
    elif result.get('partial'):
        text = f"🔶 *Частично верно*\n\n{result.get('feedback', '')}"
    else:
        text = f"❌ *Не совсем*\n\n{result.get('feedback', '')}"

    # Кнопки
    buttons = []
    if not result.get('passed'):
        buttons.append([InlineKeyboardButton(
            text="🔄 Попробовать ещё",
            callback_data=f"train_start_{principle_id}"
        )])
    buttons.append([InlineKeyboardButton(
        text="← К дашборду",
        callback_data="train_back"
    )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    try:
        await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception:
        await message.answer(text, reply_markup=keyboard, parse_mode=None)

    await state.set_state(TrainingStates.dashboard)


# ============= NAVIGATION =============

@training_router.callback_query(F.data == "train_back")
async def handle_back(callback: CallbackQuery, state: FSMContext):
    """Вернуться к дашборду."""
    engine = TrainingEngine(callback.message.chat.id)
    await show_dashboard(callback.message, engine, state, edit=True)
    await callback.answer()


# ============= SETTINGS =============

@training_router.callback_query(F.data == "train_settings")
async def handle_settings(callback: CallbackQuery, state: FSMContext):
    """Показать меню настроек."""
    engine = TrainingEngine(callback.message.chat.id)
    data = await engine.get_dashboard_data()

    cognitive_label = data.get('cognitive_label', 'Взрослый')
    principles = data.get('principles', [])
    enabled_count = sum(1 for p in principles if p['enabled'])

    text = (
        "⚙️ *Настройки тренировки*\n\n"
        f"Когнитивный уровень: {cognitive_label}\n"
        f"Включено принципов: {enabled_count}/{len(ZP_PRINCIPLES)}"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🧠 Изменить уровень",
            callback_data="train_settings_cognitive"
        )],
        [InlineKeyboardButton(
            text="📋 Изменить принципы",
            callback_data="train_settings_principles"
        )],
        [InlineKeyboardButton(
            text="← Назад",
            callback_data="train_back"
        )],
    ])

    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=None)
    await callback.answer()


@training_router.callback_query(F.data == "train_settings_cognitive")
async def handle_settings_cognitive(callback: CallbackQuery, state: FSMContext):
    """Показать выбор когнитивного уровня из настроек."""
    buttons = []
    for key, label in TRAINING_COGNITIVE_LEVELS.items():
        buttons.append([InlineKeyboardButton(
            text=label,
            callback_data=f"train_cognitive_{key}"
        )])
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="train_settings")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(
        "Выберите когнитивный уровень:",
        reply_markup=keyboard
    )
    await state.set_state(TrainingStates.settings_cognitive)
    await callback.answer()


@training_router.callback_query(F.data == "train_settings_principles")
async def handle_settings_principles(callback: CallbackQuery, state: FSMContext):
    """Показать переключение принципов из настроек."""
    engine = TrainingEngine(callback.message.chat.id)
    settings = await engine.get_settings()
    enabled = settings.get('enabled_principles', []) if settings else []

    await state.update_data(selected_principles=enabled)
    await show_principle_selection(callback.message, state)
    await callback.answer()
