"""
/diagnose — Диагностика ученика R28 (WP-318 Ф6).

# see DP.SC.132, DP.ROLE.042, PD.FORM.089 §6.1 v5.0 (WP-370: cp.iwe informational, derive cp.skl из cp.rhy)

Алгоритм CAT: ≤5 вопросов, старт со ст. 3.
Фаза 1 (2-3 якорных вопроса) → Фаза 2 (1-2 drill-down по bottleneck).
Структурированные ответы 1-5 (chip/inline-keyboard).
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from db.queries.cp_assessment import (
    MANDATORY_SLOTS,
    get_latest_cp_assessment,
    save_cp_assessment,
    compute_cp_stage,
)

logger = logging.getLogger(__name__)
diagnose_router = Router(name="diagnose")

CB_PREFIX = "diag"
CB_FORCE_RESTART = "diag-force:restart"

# Cooldown между диагностиками. WP-318 Ф6 ставил 30 дней без явного rationale.
# 2026-05-30 (post WP-353 closure): снижено до 7 дней — Аттестатор обновляет
# степень мастерства часто, рестарт ≤30 дней блокировал валидацию пилота.
# Принудительный рестарт доступен через кнопку «🔄 Повторить сейчас» в cooldown-сообщении.
DIAGNOSE_COOLDOWN_DAYS = 7


# ── FSM States ────────────────────────────────────────────────────────────────

class DiagnoseStates(StatesGroup):
    q1 = State()
    q2 = State()
    q3 = State()
    q4 = State()
    q5 = State()  # Phase 1 cp.iwe (informational, last anchor)
    q6 = State()  # WP-370: Phase 2 drill-down по mandatory bottleneck


# ── Вопросы FORM.089 §6.1 ─────────────────────────────────────────────────────

# Фаза 1: якорные вопросы (всегда задаются)
# WP-370: 5 вопросов = 4 mandatory якорных (cp.rhy/wld/int/agt) + 1 informational (cp.iwe).
# cp.skl derive из cp.rhy в _finish_diagnose (§6.1: «ритм и мастерство коррелируют»).
PHASE1_QUESTIONS = [
    {
        "slot": "cp.rhy",
        "text": (
            "📍 <b>Вопрос 1 из 5</b>\n\n"
            "Как вы ведёте учёт времени на саморазвитие и насколько регулярный ритм?\n"
            "Сколько примерно часов в неделю?"
        ),
        "labels": {
            1: "Не выделяю, как пойдёт",
            2: "Стараюсь, но без ритма (1-2 ч/нед)",
            3: "Еженедельно явно (3-4 ч/нед)",
            4: "Ежедневная практика + трекер (5-10 ч/нед)",
            5: "Автоматизировано + артефакты (10+ ч/нед)",
        },
    },
    {
        "slot": "cp.wld",
        "text": (
            "📍 <b>Вопрос 2 из 5</b>\n\n"
            "Как вы принимаете важные решения?\n"
            "Через интуицию, ценности или системный анализ?"
        ),
        "labels": {
            1: "В основном интуитивно",
            2: "Пробую разные подходы, не сложилось",
            3: "Через сформулированные принципы / мировоззрение",
            4: "Системно: цели, ограничения, альтернативы",
            5: "Передаю свои методы и принципы другим",
        },
    },
    {
        "slot": "cp.int",
        "text": (
            "📍 <b>Вопрос 3 из 5</b>\n\n"
            "Применяете ли вы системное мышление — видите ли роли, границы, "
            "интерфейсы, надсистемы в реальных задачах?"
        ),
        "labels": {
            1: "Нет опыта",
            2: "Слышал(а), но не применяю",
            3: "Базовые различения (роль/функция/граница)",
            4: "Системный разбор в работе",
            5: "Формализую модели, учу других",
        },
    },
    {
        "slot": "cp.agt",
        "text": (
            "📍 <b>Вопрос 4 из 5</b>\n\n"
            "Какая доля задач за последний месяц инициирована вами лично — "
            "не «спустили», а вы сами увидели и взяли?"
        ),
        "labels": {
            1: "Почти всё спущено сверху",
            2: "Иногда сам(а), редко",
            3: "Около половины — мои",
            4: "Большинство задач — моя инициатива",
            5: "Задаю повестку для других",
        },
    },
    {
        "slot": "cp.iwe",  # informational — не входит в mandatory, не блокирует ступень
        "text": (
            "📍 <b>Вопрос 5 из 5</b>\n\n"
            "Насколько у вас настроена среда работы со знаниями — "
            "заметки, база знаний, инструменты (VS Code + Pack + ИИ, или альтернативы)?"
        ),
        "labels": {
            1: "Среды нет",
            2: "Простейшее (заметки в телефоне, папка в облаке)",
            3: "Базово настроено, пользуюсь регулярно",
            4: "Несколько сервисов, структура, связи, поиск",
            5: "Развиваю как систему — Pack/протоколы/агенты",
        },
    },
]

# Фаза 2: уточняющие вопросы по bottleneck (задаётся один)
PHASE2_QUESTIONS = {
    "cp.skl": {
        "slot": "cp.rhy",
        "text": (
            "🔍 <b>Уточняющий вопрос</b>\n\n"
            "Как часто вы занимаетесь саморазвитием в последние 3 месяца?\n"
            "(учебные сессии, чтение, практика)"
        ),
        "labels": {
            1: "Реже раза в месяц",
            2: "1-2 раза в месяц",
            3: "Еженедельно",
            4: "3-5 раз в неделю",
            5: "Ежедневно",
        },
    },
    "cp.iwe": {
        "slot": "cp.int",
        "text": (
            "🔍 <b>Уточняющий вопрос</b>\n\n"
            "Системное мышление — это видеть связи между частями целого, "
            "понимать надсистему и подсистему. Как вы с ним?"
        ),
        "labels": {
            1: "Не знаю что это",
            2: "Слышал(а), но не применяю",
            3: "Понимаю концепцию",
            4: "Применяю осознанно",
            5: "Формализую и передаю другим",
        },
    },
    "cp.wld": {
        "slot": "cp.wld",  # уточнение того же слота
        "text": (
            "🔍 <b>Уточняющий вопрос</b>\n\n"
            "Можете назвать 2-3 принципа, которые направляют ваши решения?\n"
            "Насколько они сформулированы явно?"
        ),
        "labels": {
            1: "Принципы не сформулированы",
            2: "Есть смутное ощущение",
            3: "Могу назвать 1-2",
            4: "Явные принципы, записаны",
            5: "Принципы = работающая система",
        },
    },
    "cp.int": {
        "slot": "cp.int",
        "text": (
            "🔍 <b>Уточняющий вопрос</b>\n\n"
            "Пробовали ли вы разбирать ситуацию через надсистему и подсистему?\n"
            "Выделять роли, функции, ограничения?"
        ),
        "labels": {
            1: "Нет, это мне пока незнакомо",
            2: "Слышал(а), не применял(а)",
            3: "Применяю интуитивно",
            4: "Применяю осознанно",
            5: "Учу других делать это",
        },
    },
    "cp.rhy": {
        "slot": "cp.rhy",
        "text": (
            "🔍 <b>Уточняющий вопрос</b>\n\n"
            "Есть ли у вас «ритуалы» начала/завершения рабочей недели?\n"
            "Или регулярные точки рефлексии?"
        ),
        "labels": {
            1: "Нет ничего регулярного",
            2: "Иногда делаю итоги",
            3: "Есть еженедельный ритуал",
            4: "Структурированные ритуалы",
            5: "Полная ОРЗ-практика",
        },
    },
    "cp.agt": {
        "slot": "cp.agt",
        "text": (
            "🔍 <b>Уточняющий вопрос</b>\n\n"
            "Берёте ли вы на себя ответственность за результат,\n"
            "даже если обстоятельства были неблагоприятные?"
        ),
        "labels": {
            1: "Чаще объясняю обстоятельствами",
            2: "Иногда беру ответственность",
            3: "Обычно беру, но бывает срыв",
            4: "Всегда беру ответственность",
            5: "Задаю стандарты для других",
        },
    },
}

# WP-370: defaults только для mandatory (5). Применяется ТОЛЬКО при partial-завершении (q_count<MIN_ANCHORS).
# При full-завершении все mandatory заполнены через PHASE1 (4 явных) + derive cp.skl (из cp.rhy).
_DEFAULT_CP = {s: 2 for s in MANDATORY_SLOTS}

# WP-370: минимум якорных вопросов для валидного stage. Если меньше — показываем «диагностика не завершена».
MIN_ANCHORS = 4  # все 4 mandatory якорных в PHASE1 (cp.rhy, cp.wld, cp.int, cp.agt); cp.iwe — informational

# Человеко-читаемые имена ступеней
STAGE_NAMES = {
    1: "Случайный",
    2: "Практикующий",
    3: "Систематический",
    4: "Дисциплинированный",
    5: "Проактивный",
}

STREAM_LABELS = {
    "S1": "Фундамент — начать с основ",
    "S2": "Систематизация — собрать себя",
    "S3": "Масштаб — выйти наружу",
    "S4": "Передача — делиться и создавать",
    # WP-371: ст. 5 Проактивный → программа Рабочего развития, переход к следующим ролям.
    "РР": "Рабочее развитие — переход к ролям Интеллектуала и Профессионала",
}

# WP-371: следующие роли траектории Ученик → Интеллектуал → Профессионал → Исследователь → Просветитель.
# Показывается только на ст. 5 (Проактивный), когда ЛР завершена и следующая программа — РР.
NEXT_ROLES_HINT = "Интеллектуал → Профессионал"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _truncate_label(label: str, max_len: int = 25) -> str:
    """Усечение лейбла на границе слова с многоточием. Короткие отдаются как есть.

    iPhone Telegram режет inline-button text около 30-35 символов; max_len=25
    оставляет запас на префикс «N — » и эллипсис, влезая на iPhone SE.
    Полная формулировка варианта дублируется в тексте сообщения через _format_question().
    """
    if len(label) <= max_len:
        return label
    cut = label[:max_len]
    if " " in cut:
        words = cut.rstrip().split(" ")
        if len(words[-1]) <= 3 and len(words) > 1:
            words = words[:-1]
        cut = " ".join(words)
    return cut.rstrip(",.;:") + "…"


def _make_scale_keyboard(slot: str, q_idx: int, labels: dict[int, str]) -> InlineKeyboardMarkup:
    """Inline-клавиатура 1-5: 5 рядов по 1 кнопке. На каждой — цифра + усечённый лейбл.

    Полный лейбл — в тексте сообщения (см. _format_question). Кнопка во всю ширину
    обеспечивает max tap-target (Apple HIG ≥44pt) — критично для диагностики, где
    разница между уровнями определяет ступень мастерства.
    """
    buttons = []
    for val in range(1, 6):
        label = labels.get(val, str(val))
        short = _truncate_label(label)
        cb = f"{CB_PREFIX}:{slot}:{val}:{q_idx}"
        buttons.append([InlineKeyboardButton(text=f"{val} — {short}", callback_data=cb[:64])])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _format_question(q: dict) -> str:
    """Текст вопроса + полные тексты всех 5 вариантов под ним.

    Источник истины для рубрик FORM.089 §6.1 — q["labels"] (дословно).
    Пользователь видит формулировку без обрезки и нажимает кнопку по цифре.
    """
    lines = [q["text"], "", "<b>Варианты ответа:</b>"]
    for val in range(1, 6):
        lines.append(f"<b>{val}</b> — {html.escape(q['labels'][val])}")
    return "\n".join(lines)


async def send_diagnostic_question(target, q: dict, q_idx: int, *, prefix: str = "") -> None:
    """Единая точка отправки вопроса диагностики.

    target — Message или callback.message (поддерживает .answer()).
    prefix — опциональный preamble (используется для первого вопроса с шапкой).
    """
    text = prefix + _format_question(q) if prefix else _format_question(q)
    await target.answer(
        text,
        parse_mode="HTML",
        reply_markup=_make_scale_keyboard(q["slot"], q_idx, q["labels"]),
    )


def _determine_phase2_slot(scores: dict) -> str | None:
    """Слот с наименьшим значением среди MANDATORY (не informational), для drill-down.

    WP-370: informational cp.iwe не триггерит Phase 2 даже при низком значении.
    Phase 2 нужна только если есть пробел в mandatory (≤2).
    """
    mandatory_asked = {k: v for k, v in scores.items() if k in MANDATORY_SLOTS}
    if not mandatory_asked:
        return None
    bottleneck = min(mandatory_asked, key=mandatory_asked.get)
    return bottleneck if mandatory_asked[bottleneck] < 3 else None


def _format_result(profile: dict, valid_until_iso: str | None) -> str:
    stage = profile["stage"]
    bottleneck = profile["bottleneck_slot"]
    stream = profile["recommended_stream"]

    stage_name = STAGE_NAMES.get(stage, f"Ступень {stage}")
    stream_label = STREAM_LABELS.get(stream, stream)

    # WP-370: bottleneck = None или 'none' при stage ≥ 4 → нет узких мест.
    if bottleneck is None or bottleneck == 'none':
        priority_line = "Узких мест нет — поддерживайте темп и берите следующие ступени."
    else:
        bottleneck_human = {
            "cp.rhy": "регулярность и ритм занятий",
            "cp.wld": "мировоззрение и системный взгляд",
            "cp.skl": "учёт времени и собранность",
            "cp.iwe": "рабочая среда и инструменты",
            "cp.int": "системное мышление",
            "cp.agt": "агентность и инициатива",
        }.get(bottleneck, bottleneck)
        priority_line = f"Приоритет роста: {bottleneck_human}"

    valid_str = ""
    if valid_until_iso:
        try:
            dt = datetime.fromisoformat(valid_until_iso)
            valid_str = f"\nАктуально до: {dt.strftime('%B %Y')}"
        except Exception:
            pass

    # WP-371: на ст. 5 (Проактивный) показываем программу РР + след. роли вместо «руководства».
    if stream == "РР":
        program_line = (
            f"Следующая программа: <b>Рабочее развитие (РР)</b>\n"
            f"{stream_label}\n\n"
            f"Следующие роли: <b>{NEXT_ROLES_HINT}</b>"
        )
    else:
        program_line = f"Рекомендованное руководство: <b>{stream}</b> — {stream_label}"

    return (
        f"📊 <b>Результаты диагностики</b>\n\n"
        f"Ступень: <b>{stage_name} ({stage} из 5)</b>\n\n"
        f"{priority_line}\n\n"
        f"{program_line}"
        f"{valid_str}"
    )


# ── Entry point ───────────────────────────────────────────────────────────────

@diagnose_router.message(Command("diagnose"))
async def cmd_diagnose(message: Message, state: FSMContext) -> None:
    chat_id = message.chat.id

    from helpers.dual_write import resolve_ory_id_from_chat
    from db.queries import get_intern
    account_id = await resolve_ory_id_from_chat(chat_id)
    if not account_id:
        intern = await get_intern(chat_id)
        if intern:
            account_id = intern.get('dt_user_id')
    # account_id may still be None — proceed without saving (_finish_diagnose handles this)

    # Проверить свежий cp-срез (≤DIAGNOSE_COOLDOWN_DAYS)
    existing = await get_latest_cp_assessment(account_id) if account_id else None
    if existing:
        assessed_at = existing.get("assessed_at", "")
        try:
            dt = datetime.fromisoformat(assessed_at)
            age_days = (datetime.now(timezone.utc) - dt).days
            if age_days < DIAGNOSE_COOLDOWN_DAYS:
                stage = existing["stage"]
                stream = existing["recommended_stream"]
                stage_name = STAGE_NAMES.get(stage, f"Ступень {stage}")
                force_kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🔄 Повторить сейчас", callback_data=CB_FORCE_RESTART)
                ]])
                await message.answer(
                    f"У вас уже есть свежая диагностика ({age_days} дн. назад).\n\n"
                    f"Ступень: <b>{stage_name} ({stage} из 5)</b>\n"
                    f"Рекомендованное руководство: <b>{stream}</b>\n\n"
                    f"Следующая диагностика доступна через {DIAGNOSE_COOLDOWN_DAYS - age_days} дн.\n"
                    f"Если изменилось что-то существенное — запустите досрочно:",
                    parse_mode="HTML",
                    reply_markup=force_kb,
                )
                return
        except Exception:
            pass

    await _start_phase1(message, state, account_id)


async def _start_phase1(target, state: FSMContext, account_id: str | None) -> None:
    """Старт фазы 1: подготовка state + первый вопрос. target = Message (для cmd) или callback.message (для force-restart)."""
    await state.update_data(scores={}, account_id=account_id, q_count=0)
    q = PHASE1_QUESTIONS[0]
    await state.set_state(DiagnoseStates.q1)
    preamble = (
        "🔬 <b>Диагностика ступени мастерства</b>\n\n"
        "До 5 вопросов с вариантами ответа. Занимает ~3 минуты.\n"
        "Выбирайте то, что ближе всего к вашей текущей практике.\n\n"
    )
    await send_diagnostic_question(target, q, 0, prefix=preamble)


@diagnose_router.callback_query(F.data == CB_FORCE_RESTART)
async def cb_force_restart(callback: CallbackQuery, state: FSMContext) -> None:
    """Принудительный рестарт диагностики через кнопку в cooldown-сообщении."""
    await callback.answer("Запускаю диагностику заново…")

    chat_id = callback.from_user.id
    from helpers.dual_write import resolve_ory_id_from_chat
    from db.queries import get_intern
    account_id = await resolve_ory_id_from_chat(chat_id)
    if not account_id:
        intern = await get_intern(chat_id)
        if intern:
            account_id = intern.get('dt_user_id')

    await _start_phase1(callback.message, state, account_id)


# ── Callback handlers ─────────────────────────────────────────────────────────

async def _process_answer(callback: CallbackQuery, state: FSMContext, q_idx: int) -> None:
    """Общий обработчик ответа на вопрос q_idx (0-based Фаза 1)."""
    parts = callback.data.split(":")
    if len(parts) < 4:
        await callback.answer("Некорректный ответ")
        return

    slot, val_str = parts[1], parts[2]
    try:
        val = int(val_str)
    except ValueError:
        await callback.answer()
        return

    await callback.answer()
    data = await state.get_data()
    scores = data.get("scores", {})
    scores[slot] = val
    q_count = data.get("q_count", 0) + 1
    await state.update_data(scores=scores, q_count=q_count)

    # Редактируем сообщение: подтверждаем ответ
    label = PHASE1_QUESTIONS[q_idx]["labels"].get(val, str(val))
    try:
        await callback.message.edit_text(
            callback.message.text + f"\n\n✅ Ваш ответ: {val} — {label}",
            parse_mode="HTML",
        )
    except Exception:
        pass

    next_idx = q_idx + 1
    # WP-370: 5 вопросов Phase 1 (4 mandatory + cp.iwe informational), states q1..q5.
    states_map = [
        DiagnoseStates.q1, DiagnoseStates.q2, DiagnoseStates.q3,
        DiagnoseStates.q4, DiagnoseStates.q5,
    ]
    if next_idx < len(PHASE1_QUESTIONS):
        nq = PHASE1_QUESTIONS[next_idx]
        await state.set_state(states_map[next_idx])
        await send_diagnostic_question(callback.message, nq, next_idx)
    else:
        # Фаза 1 завершена. Phase 2 drill-down: только по mandatory bottleneck (cp.iwe — informational, не триггерит).
        # MANDATORY_SLOTS = 5 (cp.rhy, cp.wld, cp.skl, cp.int, cp.agt) — cp.skl derive из cp.rhy в _finish_diagnose.
        bottleneck_slot = _determine_phase2_slot(scores)
        if bottleneck_slot and bottleneck_slot in PHASE2_QUESTIONS and bottleneck_slot != "cp.iwe":
            p2q = PHASE2_QUESTIONS[bottleneck_slot]
            await state.set_state(DiagnoseStates.q6)  # WP-370: Phase 2 drill-down (q5 занят Phase 1 cp.iwe)
            await send_diagnostic_question(callback.message, p2q, 99)
        else:
            await _finish_diagnose(callback.message, state)


@diagnose_router.callback_query(DiagnoseStates.q1, F.data.startswith(CB_PREFIX + ":"))
async def answer_q1(callback: CallbackQuery, state: FSMContext) -> None:
    await _process_answer(callback, state, 0)


@diagnose_router.callback_query(DiagnoseStates.q2, F.data.startswith(CB_PREFIX + ":"))
async def answer_q2(callback: CallbackQuery, state: FSMContext) -> None:
    await _process_answer(callback, state, 1)


@diagnose_router.callback_query(DiagnoseStates.q3, F.data.startswith(CB_PREFIX + ":"))
async def answer_q3(callback: CallbackQuery, state: FSMContext) -> None:
    await _process_answer(callback, state, 2)


@diagnose_router.callback_query(DiagnoseStates.q4, F.data.startswith(CB_PREFIX + ":"))
async def answer_q4(callback: CallbackQuery, state: FSMContext) -> None:
    await _process_answer(callback, state, 3)


@diagnose_router.callback_query(DiagnoseStates.q5, F.data.startswith(CB_PREFIX + ":"))
async def answer_q5(callback: CallbackQuery, state: FSMContext) -> None:
    """Phase 1, last anchor — cp.iwe informational. После него возможен Phase 2 drill-down."""
    await _process_answer(callback, state, 4)


@diagnose_router.callback_query(DiagnoseStates.q6, F.data.startswith(CB_PREFIX + ":"))
async def answer_q6(callback: CallbackQuery, state: FSMContext) -> None:
    """WP-370: Phase 2 drill-down handler (бывший answer_q5 на DiagnoseStates.q5)."""
    parts = callback.data.split(":")
    if len(parts) < 4:
        await callback.answer()
        return
    slot, val_str = parts[1], parts[2]
    try:
        val = int(val_str)
    except ValueError:
        await callback.answer()
        return

    await callback.answer()
    data = await state.get_data()
    scores = data.get("scores", {})
    scores[slot] = val
    await state.update_data(scores=scores)

    label = ""
    for pq in PHASE2_QUESTIONS.values():
        if pq["slot"] == slot:
            label = pq["labels"].get(val, str(val))
            break

    try:
        await callback.message.edit_text(
            callback.message.text + f"\n\n✅ Ваш ответ: {val} — {label}",
            parse_mode="HTML",
        )
    except Exception:
        pass

    await _finish_diagnose(callback.message, state)


async def _finish_diagnose(message: Message, state: FSMContext) -> None:
    """Вычислить профиль, сохранить, показать результат.

    WP-370: §6.1 v5.0 compliance.
    - q_count < MIN_ANCHORS (4 mandatory) → partial: показать «не завершена», не вычислять stage.
    - cp.skl derive из cp.rhy (§6.1 line 144 «cp.skl определяется из ответа на cp.rhy»).
    - all mandatory ≥ 4 → bottleneck = None (показывается как «нет узких мест»).
    """
    data = await state.get_data()
    await state.clear()

    account_id = data.get("account_id")
    scores_raw = data.get("scores", {})
    q_count = data.get("q_count", 0)

    # WP-370 partial-flag: меньше 4 mandatory ответов — диагностика недостоверна.
    if q_count < MIN_ANCHORS:
        await message.answer(
            "⚠️ <b>Диагностика не завершена</b>\n\n"
            f"Получено ответов: {q_count} из {MIN_ANCHORS} обязательных.\n"
            "Для корректной оценки ступени нужно пройти все обязательные вопросы.\n\n"
            "Запустите заново: /diagnose",
            parse_mode="HTML",
        )
        return

    # Default consevativeно подстраховывает редкие partial-кейсы (после MIN_ANCHORS check — обычно не сработает).
    scores = dict(_DEFAULT_CP)
    scores.update(scores_raw)

    # WP-370 derive: cp.skl выводится из cp.rhy (spec §6.1).
    # setdefault — если cp.skl уже задан явно (будущий код), не перезаписываем.
    scores.setdefault("cp.skl", scores.get("cp.rhy", 2))
    if "cp.skl" not in scores_raw and "cp.rhy" in scores_raw:
        scores["cp.skl"] = scores_raw["cp.rhy"]  # explicit override default

    scores = await _apply_bh_proxy(account_id, scores)

    profile = compute_cp_stage(scores)

    if not account_id:
        logger.warning("[diagnose] no account_id — skip save")
        await message.answer(
            _format_result(profile, None),
            parse_mode="HTML",
        )
        return

    try:
        row_id = await save_cp_assessment(
            account_id=account_id,
            cp_scores=scores,
            source="dialogue",
            interface="tg",
            questions_count=q_count,
        )
        from datetime import timedelta
        from datetime import datetime as _dt
        valid_until = (_dt.now(timezone.utc) + timedelta(days=180)).isoformat()
        await message.answer(
            _format_result(profile, valid_until),
            parse_mode="HTML",
        )
        logger.info("[diagnose] saved id=%s stage=%s", row_id, profile["stage"])
    except Exception as e:
        logger.error("[diagnose] save failed: %s", e)
        await message.answer(
            _format_result(profile, None) + "\n\n⚠️ Результат сохранить не удалось. "
            "Попробуйте снова позже.",
            parse_mode="HTML",
        )


async def _apply_bh_proxy(account_id: str | None, scores: dict) -> dict:
    """Фаза 3 (bh-прокси): повысить cp-слоты на основе bh-индексов (FORM.089 §6.1).

    bh.inv ≥ 4 → cp.skl baseline ≥ 2
    bh.sys ≥ 3 → cp.rhy baseline ≥ 2
    bh.awr ≥ 2 → cp.wld baseline ≥ 2

    Правило: cp_final = max(cp_dialogue, cp_bh_proxy) — только повышаем.
    Если bh недоступен — возвращаем scores без изменений.
    """
    if not account_id:
        return scores
    try:
        from db.connection import get_learning_pool
        pool = await get_learning_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT payload FROM learning.domain_event
                   WHERE account_id = $1::uuid
                     AND event_type = 'bh.snapshot'
                   ORDER BY occurred_at DESC
                   LIMIT 1""",
                account_id,
            )
        if row is None:
            return scores

        import json as _json
        bh = _json.loads(row["payload"]) if isinstance(row["payload"], str) else (row["payload"] or {})

        inv = float(bh.get("bh.inv", bh.get("inv", 0)) or 0)
        sys_ = float(bh.get("bh.sys", bh.get("sys", 0)) or 0)
        awr = float(bh.get("bh.awr", bh.get("awr", 0)) or 0)

        if inv >= 4:
            scores["cp.skl"] = max(scores.get("cp.skl", 1), 2)
        if sys_ >= 3:
            scores["cp.rhy"] = max(scores.get("cp.rhy", 1), 2)
        if awr >= 2:
            scores["cp.wld"] = max(scores.get("cp.wld", 1), 2)
    except Exception as e:
        logger.debug("[diagnose] bh_proxy skip: %s", e)
    return scores
