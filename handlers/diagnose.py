"""
/diagnose — Диагностика ученика R28 (WP-318 Ф6).

# see DP.SC.132, DP.ROLE.042, PD.FORM.089 §6.1 (WP-370: cp.iwe informational; WP-385 Ф18: cp.skl asked separately, bh does not raise cp)

Алгоритм CAT: ≤5 вопросов, старт со ст. 3.
Фаза 1 (2-3 якорных вопроса) → Фаза 2 (1-2 drill-down по bottleneck).
Структурированные ответы 1-5 (chip/inline-keyboard).
"""
from __future__ import annotations

import asyncio
import html
import logging
import uuid
from datetime import datetime, timedelta, timezone

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
    CP_TTL_DAYS,
    PROGRAM_STAGE_LABELS,
    program_stage_for,
    STAGE_NAMES as CP_STAGE_NAMES,
    get_latest_cp_assessment,
    compute_cp_stage,
    describe_bottleneck,
    learner_role_has_no_gaps,
    qualification_programme,
)
from core.qualification import qualified_level
from helpers.typing_indicator import keep_typing

logger = logging.getLogger(__name__)
diagnose_router = Router(name="diagnose")

CB_PREFIX = "diag"
CB_FORCE_RESTART = "diag-force:restart"
CB_RETRY_SAVE = "diag-save:retry"

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
    q5 = State()
    q6 = State()  # Phase 1 cp.iwe (informational, last question)
    clarify = State()  # Phase 2: one clarifying question on the mandatory bottleneck


# ── Вопросы FORM.089 §6.1 ─────────────────────────────────────────────────────

# Фаза 1: якорные вопросы (всегда задаются)
# 6 questions = 5 mandatory slices (cp.rhy/wld/skl/int/agt) + 1 informational (cp.iwe).
# cp.skl is its own question, not a copy of cp.rhy (PD.FORM.089 §6.1, WP-385 Ф18.7);
# cp.agt is asked to everyone: it is in the minimum on every stage (pilot decision 20.10.2026).
PHASE1_QUESTIONS = [
    {
        "slot": "cp.rhy",
        "text": (
            "📍 <b>Вопрос 1 из 6</b>\n\n"
            "Сколько времени в неделю вы уделяете своему развитию (учёба, чтение, практика) и насколько регулярно?\n"
            "Сколько это примерно часов?"
        ),
        "labels": {
            1: "Не выделяю время, как получится",
            2: "Стараюсь, но без графика (1–2 ч в неделю)",
            3: "Каждую неделю в заранее выбранное время (3–4 ч)",
            4: "Каждый день, веду учёт времени (5–10 ч в неделю)",
            5: "Вошло в привычку, после занятий остаются результаты: записи, схемы, тексты (10+ ч в неделю)",
        },
    },
    {
        "slot": "cp.wld",
        "text": (
            "📍 <b>Вопрос 2 из 6</b>\n\n"
            "Как вы обычно принимаете важные решения?"
        ),
        "labels": {
            1: "В основном интуитивно",
            2: "Пробую разные способы, своего пока нет",
            3: "Опираюсь на свои принципы и взгляд на жизнь",
            4: "Разбираю по шагам: цели, ограничения, варианты",
            5: "Действую по своим принципам и делюсь ими с другими",
        },
    },
    {
        "slot": "cp.skl",
        "text": (
            "📍 <b>Вопрос 3 из 6</b>\n\n"
            "Когда вы хотите чему-то научиться или справиться с трудностью в своём развитии, "
            "как вы выбираете способ действовать?"
        ),
        "labels": {
            1: "Не знаю, какой способ подойдёт",
            2: "Повторяю готовую инструкцию",
            3: "Сам(а) применяю знакомый способ",
            4: "Выбираю из нескольких способов и подстраиваю под ситуацию",
            5: "Придумываю или улучшаю свои способы и помогаю другим их освоить",
        },
    },
    {
        "slot": "cp.int",
        "text": (
            "📍 <b>Вопрос 4 из 6</b>\n\n"
            "Когда вы решаете рабочую задачу, выделяете ли вы систему, с которой имеете дело: "
            "из чего она состоит, какую роль играет каждая часть, где её границы и во что она входит?"
        ),
        "labels": {
            1: "Нет опыта",
            2: "Слышал(а), но не применяю",
            3: "Различаю роли, функции и границы",
            4: "Так разбираю системы в своих рабочих задачах",
            5: "Строю свои модели и учу этому других",
        },
    },
    {
        "slot": "cp.agt",
        "text": (
            "📍 <b>Вопрос 5 из 6</b>\n\n"
            "Какая доля задач за последний месяц инициирована вами лично — "
            "не «спустили», а вы сами увидели и взяли?"
        ),
        "labels": {
            1: "Почти всё спущено сверху",
            2: "Иногда сам(а), редко",
            3: "Около половины — мои",
            4: "Большинство задач — моя инициатива",
            5: "Определяю, чем занимаются другие",
        },
    },
    {
        "slot": "cp.iwe",  # informational — не входит в mandatory, не блокирует ступень
        "text": (
            "📍 <b>Вопрос 6 из 6</b>\n\n"
            "Насколько у вас налажена работа со знаниями: где хранятся заметки и личная база, "
            "какими программами и ИИ-помощниками вы пользуетесь?"
        ),
        "labels": {
            1: "Ничего такого нет",
            2: "Простейшее (заметки в телефоне, папка в облаке)",
            3: "Базово настроено, пользуюсь регулярно",
            4: "Несколько программ, записи упорядочены и связаны, есть поиск",
            5: "Выстроил(а) единую систему: программы и ИИ-помощники работают по моим правилам и шаблонам",
        },
    },
]

# Фаза 2: уточняющие вопросы по bottleneck (задаётся один)
PHASE2_QUESTIONS = {
    "cp.skl": {
        "slot": "cp.skl",
        "text": (
            "🔍 <b>Уточняющий вопрос</b>\n\n"
            "Вспомните последний раз, когда вы применили какой-то способ развития к реальной задаче.\n"
            "Можете ли вы сказать, почему выбрали именно его и что изменилось?"
        ),
        "labels": {
            1: "Не припомню такого случая",
            2: "Был случай, но способ выбрал(а) наугад",
            3: "Назову способ и что изменилось",
            4: "Объясню, почему выбрал(а) этот способ, и покажу результат",
            5: "Сравниваю результаты разных способов и улучшаю их",
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
            5: "Принципы согласованы между собой, я действую по ним постоянно",
        },
    },
    "cp.int": {
        "slot": "cp.int",
        "text": (
            "🔍 <b>Уточняющий вопрос</b>\n\n"
            "Пробовали ли вы в рабочей ситуации разобрать систему, с которой имеете дело:\n"
            "во что она входит, из чего состоит, кто какую роль играет и какие есть ограничения?"
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
            "Есть ли у вас привычка в начале или конце недели планировать и подводить итоги?"
        ),
        "labels": {
            1: "Ничего регулярного нет",
            2: "Иногда подвожу итоги",
            3: "Есть еженедельная привычка",
            4: "Чёткий порядок: план на неделю, итоги, выводы",
            5: "Полностью выстроенное планирование и подведение итогов (день, неделя, месяц)",
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
            5: "Задаю планку для других",
        },
    },
}

# Minimum answered questions for a valid stage: all five mandatory slices are asked in PHASE1.
MIN_ANCHORS = len(MANDATORY_SLOTS)

# Человеко-читаемые имена ступеней (единый словарь — db/queries/cp_assessment.py)
STAGE_NAMES = CP_STAGE_NAMES["ru"]

CONSULT_HINT = "Что выбрать, подскажет консультация в IWE или у наставника."


def _program_line(stage: int) -> str:
    """Recommended programme stage for a learner by the admission table.

    Stage 1 may also start with S1; stage 4 may take S3 or go straight to «Рабочее развитие»
    (pilot 15.11.2026, WP-385 Ф18).
    """
    code = program_stage_for(stage)
    if code == "РР":
        return (f"Следующая программа: <b>{PROGRAM_STAGE_LABELS[code]}</b>\n"
                f"Следующие роли: <b>{NEXT_ROLES_HINT}</b>")
    line = f"Подходящий этап программы «Личное развитие»: <b>{PROGRAM_STAGE_LABELS[code]}</b>"
    if code == "S0":
        line += "\nС первой ступени можно начать и сразу с S1 «Собранность»."
    if stage == 4:
        line += f" — или сразу программа «Рабочее развитие». {CONSULT_HINT}"
    return line


def _qualified_result(qualification: str, stage: int, bottleneck: str | None,
                      stage5_candidate: bool) -> str:
    """Two separate answers for «Работник» and above: the programme by qualification, and the
    gaps in the learner role this diagnostic measures (pilot 15.11.2026, WP-385 Ф18)."""
    programme = qualification_programme(qualification)
    head = (
        f"🎓 Квалификация: <b>{html.escape(qualification)}</b>\n"
        "Эта диагностика измеряет роль Ученика — умение развивать себя и свою ИИ-среду. "
        "Квалификацию она не меняет и не подтверждает.\n\n"
        f"Для вашей квалификации рекомендуется программа {programme}."
    )
    if stage5_candidate or stage >= 5:
        role = "Диагностика по роли Ученика пробелов не показала — дополнительно проходить ничего не нужно."
    else:
        role = (f"Однако диагностика по роли Ученика показала, что у вас ступень {stage} "
                f"«{STAGE_NAMES.get(stage, stage)}»")
        if bottleneck and bottleneck != "none":
            role += f", слабее всего — {describe_bottleneck(bottleneck)}"
        label = PROGRAM_STAGE_LABELS[program_stage_for(stage)]
        if stage == 4:
            role += (f". Можно также пройти этап <b>{label}</b> программы «Личное развитие». "
                     f"Нужно ли, подскажет консультация в IWE или у наставника.")
        else:
            role += f". Поэтому вам рекомендуется также пройти этап <b>{label}</b> программы «Личное развитие»."
    return f"{head}\n\n{role}"

# strftime('%B') зависит от locale процесса (не задан на проде → английские имена
# месяцев); locale.setlocale() не годится — глобален для процесса, гонка между
# конкурентными asyncio-запросами разных пользователей.
_MONTH_NAMES_RU = {
    1: "январь", 2: "февраль", 3: "март", 4: "апрель",
    5: "май", 6: "июнь", 7: "июль", 8: "август",
    9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь",
}


def _format_month_year_ru(dt: datetime) -> str:
    return f"{_MONTH_NAMES_RU[dt.month]} {dt.year}"


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


# Same ceiling as the optional twin reads of /progress (states/utilities/progress.py).
QUALIFICATION_READ_SECONDS = 8


async def _qualification(chat_id: int) -> str | None:
    """Qualification name for «Работник» and above; None for «Ученик», no twin or a failed read.

    Stages 1..5 exist only inside «Ученик» (WP-149), so a qualified person sees the
    qualification instead of the stage (WP-417 Ф8). Best effort: the diagnostic result is shown
    with the stage when the twin cannot be read.
    """
    from clients.gateway_mcp import gateway_mcp
    if not gateway_mcp.is_connected(chat_id):
        return None
    try:
        degree = await asyncio.wait_for(gateway_mcp.dt_read_degree(chat_id), QUALIFICATION_READ_SECONDS)
    except Exception as e:
        logger.warning("[diagnose] qualification read failed (chat=%s): %r", chat_id, e)
        return None
    return qualified_level(degree)


def _stage_line(stage: int) -> str:
    stage_name = STAGE_NAMES.get(stage, f"Ступень {stage}")
    return f"Ступень: <b>{stage_name} ({stage} из 5)</b>"


STAGE5_HINT = ("По вашим ответам у вас есть признаки пятой ступени. Её не присваивают по одной "
               "самооценке: подтверждение — через наставника или по данным вашей активности.")


def _learner_result(stage: int, bottleneck: str | None, stage5_candidate: bool) -> str:
    # WP-370: bottleneck = None или 'none' при stage ≥ 4 → нет узких мест.
    if bottleneck is None or bottleneck == 'none':
        priority_line = "Узких мест нет — поддерживайте темп и берите следующие ступени."
    else:
        priority_line = f"Приоритет роста: {describe_bottleneck(bottleneck)}"
    program_line = _program_line(stage)
    if stage5_candidate:
        program_line = f"{STAGE5_HINT}\n\n{program_line}"
    return f"{_stage_line(stage)}\n\n{priority_line}\n\n{program_line}"


def _result_body(stage: int, bottleneck: str | None, stage5_candidate: bool,
                 qualification: str | None) -> str:
    if qualification:
        return _qualified_result(qualification, stage, bottleneck, stage5_candidate)
    return _learner_result(stage, bottleneck, stage5_candidate)


def _format_result(profile: dict, valid_until_iso: str | None, qualification: str | None = None) -> str:
    valid_str = ""
    if valid_until_iso:
        try:
            dt = datetime.fromisoformat(valid_until_iso)
            valid_str = f"\nАктуально до: {_format_month_year_ru(dt)}"
        except Exception:
            pass

    body = _result_body(profile["stage"], profile["bottleneck_slot"],
                        bool(profile.get("stage5_candidate")), qualification)
    return f"📊 <b>Результаты диагностики</b>\n\n{body}{valid_str}"


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
                async with keep_typing(message):
                    qualification = await _qualification(chat_id)
                no_gaps = learner_role_has_no_gaps(existing["stage"], existing.get("cp_scores"))
                body = _result_body(existing["stage"], existing.get("bottleneck_slot"),
                                    no_gaps, qualification)
                force_kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🔄 Повторить сейчас", callback_data=CB_FORCE_RESTART)
                ]])
                await message.answer(
                    f"У вас уже есть свежая диагностика ({age_days} дн. назад).\n\n"
                    f"{body}\n\n"
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
    await state.update_data(scores={}, account_id=account_id, q_count=0,
                            attempt_id=str(uuid.uuid4()), pending_save=None)
    q = PHASE1_QUESTIONS[0]
    await state.set_state(DiagnoseStates.q1)
    # WP-406 Ф19: обёртка «зачем→что» вокруг вопросов. Сами вопросы/шкала не меняются
    # (формальный инструмент FORM.089 §6.1, ответы считают ступень мастерства).
    preamble = (
        "🔬 <b>Найдём вашу точку старта</b>\n\n"
        "6 вопросов о том, как вы сейчас работаете и развиваетесь. Здесь нет "
        "проходного балла, но по ответам определяется ступень мастерства — "
        "чтобы подобрать руководство под ваш уровень, а не общее для всех. "
        "Занимает ~3 минуты.\n\n"
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
    # 6 questions in Phase 1 (5 mandatory + cp.iwe informational), states q1..q6.
    states_map = [
        DiagnoseStates.q1, DiagnoseStates.q2, DiagnoseStates.q3,
        DiagnoseStates.q4, DiagnoseStates.q5, DiagnoseStates.q6,
    ]
    if next_idx < len(PHASE1_QUESTIONS):
        nq = PHASE1_QUESTIONS[next_idx]
        await state.set_state(states_map[next_idx])
        await send_diagnostic_question(callback.message, nq, next_idx)
    else:
        # Фаза 1 завершена. Phase 2 drill-down: только по mandatory bottleneck (cp.iwe — informational, не триггерит).
        bottleneck_slot = _determine_phase2_slot(scores)
        if bottleneck_slot and bottleneck_slot in PHASE2_QUESTIONS and bottleneck_slot != "cp.iwe":
            p2q = PHASE2_QUESTIONS[bottleneck_slot]
            await state.set_state(DiagnoseStates.clarify)
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
    await _process_answer(callback, state, 4)


@diagnose_router.callback_query(DiagnoseStates.q6, F.data.startswith(CB_PREFIX + ":"))
async def answer_q6(callback: CallbackQuery, state: FSMContext) -> None:
    """Phase 1, last question — cp.iwe informational. A Phase 2 clarifying question may follow."""
    await _process_answer(callback, state, 5)


@diagnose_router.callback_query(DiagnoseStates.clarify, F.data.startswith(CB_PREFIX + ":"))
async def answer_clarify(callback: CallbackQuery, state: FSMContext) -> None:
    """Phase 2: the clarifying answer replaces the bottleneck slice value."""
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
    """Вычислить профиль, записать самооценку через инструмент, показать результат.

    PD.FORM.089 §6.1:
    - a mandatory slice without an answer → partial: «не завершена», stage is not computed
      (also guards a run started before the question set changed).
    - every mandatory slice comes from its own answer; behaviour signals (bh.*) do not raise
      cp.* (§6.1 phase 3, WP-385 Ф18.8) — they are a cross-check for the stage calculation.
    - all mandatory ≥ 4 → no bottleneck; stage is at most 4 (Ф18.9).

    WP-578 Ф21.4: запись идёт только через инструмент сервиса учебного контекста.
    Завершённая попытка (ключ, время, профиль) остаётся в FSM, пока запись не подтверждена:
    повтор той же попытки не создаёт второй записи.
    """
    data = await state.get_data()

    account_id = data.get("account_id")
    scores_raw = data.get("scores", {})
    q_count = data.get("q_count", 0)

    # Fewer answers than mandatory slices — the diagnostic is not valid.
    answered = sum(1 for slot in MANDATORY_SLOTS if slot in scores_raw)
    if q_count < MIN_ANCHORS or answered < MIN_ANCHORS:
        await state.clear()
        await message.answer(
            "⚠️ <b>Диагностика не завершена</b>\n\n"
            f"Получено ответов: {answered} из {MIN_ANCHORS} обязательных.\n"
            "Для корректной оценки ступени нужно пройти все обязательные вопросы.\n\n"
            "Запустите заново: /diagnose",
            parse_mode="HTML",
        )
        return

    scores = dict(scores_raw)

    profile = compute_cp_stage(scores)

    if not account_id:
        await state.clear()
        logger.warning("[diagnose] no account_id — skip save")
        await message.answer(
            _format_result(profile, None, await _qualification(message.chat.id)),
            parse_mode="HTML",
        )
        from core.onboarder.x3 import check_x3_return_to_bridge
        await check_x3_return_to_bridge(message.bot, message.chat.id, profile)
        return

    # Попытка фиксируется до сетевого вызова. attempt_id создан на старте опроса; если FSM
    # остался от версии без него — новый, ключ всё равно стабилен для всех повторов этой попытки.
    attempt_id = data.get("attempt_id") or str(uuid.uuid4())
    # Read before touching the FSM: a slow twin must not widen the window between
    # set_state(None) and state.clear() in _save_attempt (cold review, 14.11).
    qualification = await _qualification(message.chat.id)
    await state.set_state(None)
    await state.update_data(pending_save={
        "event_id": f"tg-diagnose:{attempt_id}",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "account_id": account_id,
        "profile": profile,
        "cp_scores": scores,
        "questions_count": q_count,
        "qualification": qualification,
    })
    await _save_attempt(message, state, first_pass=True)

    # WP-406 Ф5: мост обратно в Онбордер Х3 (bridge path)
    from core.onboarder.x3 import check_x3_return_to_bridge
    await check_x3_return_to_bridge(message.bot, message.chat.id, profile)


# Исходы записи самооценки через инструмент (РП-578 Ф21.4)
SAVE_OK = "saved"
SAVE_NO_EVENT = "saved_no_event"  # самооценка записана, событие в журнал активности — нет
SAVE_AUTH = "auth"
SAVE_IDENTITY = "identity"
SAVE_UNKNOWN = "unknown"  # ответа нет или он не разобран: запись могла состояться

_AUTH_ERRORS = frozenset({"not_authorized", "token_expired", "no_subscription"})

_SAVE_NOTES = {
    SAVE_NO_EVENT: (
        "⚠️ Результат сохранён, но отметка в журнале активности не записана. "
        "Нажмите «Повторить сохранение» — второй записи не будет."
    ),
    SAVE_AUTH: (
        "⚠️ Результат не сохранён: нужно подключить аккаунт Aisystant "
        "(Настройки → Подключения). После подключения нажмите «Повторить сохранение»."
    ),
    SAVE_IDENTITY: (
        "⚠️ Результат не сохранён: не удалось подтвердить, что подключённый аккаунт — "
        "ваш профиль. Переподключите аккаунт и нажмите «Повторить сохранение»."
    ),
    SAVE_UNKNOWN: (
        "⚠️ Не удалось подтвердить сохранение. "
        "Нажмите «Повторить сохранение» — запись не задвоится."
    ),
}


def classify_save_result(result, last_error: str) -> str:
    """Исход вызова инструмента: ответ с event_recorded — запись подтверждена сервисом."""
    if isinstance(result, dict) and "event_recorded" in result:
        return SAVE_OK if result["event_recorded"] else SAVE_NO_EVENT
    if result is None and last_error in _AUTH_ERRORS:
        return SAVE_AUTH
    return SAVE_UNKNOWN


async def _write_attempt(chat_id: int, pending: dict) -> str:
    """Записать попытку через шлюз; не пишет, если владельца токена нельзя сверить с профилем."""
    from clients.gateway_mcp import gateway_mcp

    token_ory_id = await gateway_mcp.token_ory_id(chat_id)
    if token_ory_id is None:
        if not gateway_mcp.is_connected(chat_id):
            return SAVE_AUTH
        logger.warning("[diagnose] token has no ory_id, cannot verify identity (chat=%s)", chat_id)
        return SAVE_IDENTITY
    if str(token_ory_id).lower() != str(pending["account_id"]).lower():
        logger.warning("[diagnose] token ory_id differs from profile account (chat=%s)", chat_id)
        return SAVE_IDENTITY

    result = await gateway_mcp.record_cp_assessment(
        chat_id,
        event_id=pending["event_id"],
        profile=pending["profile"],
        cp_scores=pending["cp_scores"],
        questions_count=pending["questions_count"],
        occurred_at=pending["occurred_at"],
    )
    outcome = classify_save_result(result, gateway_mcp.last_call_error)
    if outcome != SAVE_OK:
        logger.warning("[diagnose] save outcome=%s last_error=%s", outcome, gateway_mcp.last_call_error)
    return outcome


async def _save_attempt(message: Message, state: FSMContext, *, first_pass: bool) -> None:
    """Записать сохранённую в FSM попытку и показать исход; FSM чистится только после полного успеха."""
    data = await state.get_data()
    pending = data.get("pending_save")
    if not pending:
        await message.answer("Нет несохранённой диагностики. Запустите заново: /diagnose")
        return

    profile = pending["profile"]
    try:
        outcome = await _write_attempt(message.chat.id, pending)
    except Exception:
        # Исход неизвестен: результат и кнопка повтора всё равно показываются.
        logger.exception("[diagnose] save attempt raised (chat=%s)", message.chat.id)
        outcome = SAVE_UNKNOWN
    recorded = outcome in (SAVE_OK, SAVE_NO_EVENT)

    if first_pass:
        valid_until = None
        if recorded:
            valid_until = (
                datetime.fromisoformat(pending["occurred_at"]) + timedelta(days=CP_TTL_DAYS)
            ).isoformat()
        await message.answer(
            _format_result(profile, valid_until, pending.get("qualification")),
            parse_mode="HTML",
        )

    if outcome == SAVE_OK:
        if first_pass:
            await state.clear()
        else:
            # Повтор мог прийти из другого сценария: убираем только свою попытку.
            await state.update_data(pending_save=None)
        if not first_pass:
            await message.answer("✅ Результат сохранён.")
    else:
        retry_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔁 Повторить сохранение", callback_data=CB_RETRY_SAVE)
        ]])
        await message.answer(_SAVE_NOTES[outcome], reply_markup=retry_kb)

    if recorded:
        # WP-349 mini-sync: мгновенное обновление cp_stage в onboarding_state
        try:
            from db.queries.onboarding_journey import sync_cp_stage_to_onboarding_state
            await sync_cp_stage_to_onboarding_state(pending["account_id"], profile["stage"])
        except Exception as sync_e:
            logger.warning("[diagnose] mini-sync cp_stage failed (non-fatal): %s", sync_e)
    logger.info("[diagnose] outcome=%s stage=%s", outcome, profile["stage"])


@diagnose_router.callback_query(F.data == CB_RETRY_SAVE)
async def cb_retry_save(callback: CallbackQuery, state: FSMContext) -> None:
    """Повторить запись той же попытки (тот же event_id) без нового опроса."""
    await callback.answer()
    await _save_attempt(callback.message, state, first_pass=False)
