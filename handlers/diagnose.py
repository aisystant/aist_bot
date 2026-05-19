"""
/diagnose — Диагностика ученика R28 (WP-318 Ф6).

# see DP.SC.132, DP.ROLE.042, PD.FORM.089 §6.1 v4.2

Алгоритм CAT: ≤5 вопросов, старт со ст. 3.
Фаза 1 (2-3 якорных вопроса) → Фаза 2 (1-2 drill-down по bottleneck).
Структурированные ответы 1-5 (chip/inline-keyboard).
"""
from __future__ import annotations

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


# ── FSM States ────────────────────────────────────────────────────────────────

class DiagnoseStates(StatesGroup):
    q1 = State()
    q2 = State()
    q3 = State()
    q4 = State()
    q5 = State()


# ── Вопросы FORM.089 §6.1 ─────────────────────────────────────────────────────

# Фаза 1: якорные вопросы (всегда задаются)
PHASE1_QUESTIONS = [
    {
        "slot": "cp.skl",
        "text": (
            "📍 <b>Вопрос 1 из 5</b>\n\n"
            "Вы осознанно выделяете время на изучение нового — не просто читаете что попадётся, "
            "а именно отводите время под развитие?\n"
            "Сколько примерно часов в неделю?"
        ),
        "labels": {
            1: "Не выделяю осознанно, по ситуации",
            2: "Стараюсь всегда учиться, но ритма нет",
            3: "Явно знаю, что получается 3-4 ч/нед",
            4: "Регулярно, не менее 1 часа в день и до 8 ч/нед",
            5: "Ежедневная практика и более 10 ч/нед",
        },
    },
    {
        "slot": "cp.agt",
        "text": (
            "📍 <b>Вопрос 2 из 5</b>\n\n"
            "Используете ли вы конкретные методы для своего развития?\n"
            "Например: ведение заметок, учёт времени, регулярные сессии стратегирования и планирования."
        ),
        "labels": {
            1: "Нет",
            2: "Иногда пробую что-то, но не приживается",
            3: "Есть 1-2 приёма, применяю",
            4: "Есть много методов, которые осознанно добавляю",
            5: "Развиваю и передаю методы другим",
        },
    },
    {
        "slot": "cp.wld",
        "text": (
            "📍 <b>Вопрос 3 из 5</b>\n\n"
            "Есть ли у вас принципы, которые определяют ваши важные решения?\n"
            "Насколько они явные — вы могли бы их сформулировать прямо сейчас?"
        ),
        "labels": {
            1: "Решаю интуитивно",
            2: "Что-то есть, но смутно",
            3: "Могу назвать 2-3 принципа",
            4: "Принципы явные, записаны",
            5: "Есть целостное мировоззрение, передаю другим",
        },
    },
    {
        "slot": "cp.iwe",
        "text": (
            "📍 <b>Вопрос 4 из 5</b>\n\n"
            "Насколько хорошо у вас настроен инструмент хранения и обработки знаний — "
            "заметки, база знаний, инструменты?"
        ),
        "labels": {
            1: "У меня его нет",
            2: "Сделал самый простой (заметки в телефоне, папка в облаке)",
            3: "Есть рабочий инструмент, пользуюсь регулярно",
            4: "Настроен процесс работы с несколькими сервисами: структура, связи, поиск",
            5: "Регулярно развиваю его",
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

# Дефолтные значения cp-слотов (не задавались в диалоге)
_DEFAULT_CP = {s: 2 for s in MANDATORY_SLOTS}  # консервативный дефолт

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
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_scale_keyboard(slot: str, q_idx: int, labels: dict[int, str]) -> InlineKeyboardMarkup:
    """Inline-клавиатура 1-5 с подписями."""
    buttons = []
    for val in range(1, 6):
        label = labels.get(val, str(val))
        cb = f"{CB_PREFIX}:{slot}:{val}:{q_idx}"
        buttons.append([InlineKeyboardButton(text=f"{val} — {label}", callback_data=cb[:64])])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _determine_phase2_slot(scores: dict) -> str | None:
    """Слот с наименьшим значением среди заданных в Фазе 1."""
    asked = {k: v for k, v in scores.items() if k in [q["slot"] for q in PHASE1_QUESTIONS]}
    if not asked:
        return None
    bottleneck = min(asked, key=asked.get)
    return bottleneck if asked[bottleneck] < 3 else None


def _format_result(profile: dict, valid_until_iso: str | None) -> str:
    stage = profile["stage"]
    bottleneck = profile["bottleneck_slot"]
    stream = profile["recommended_stream"]

    stage_name = STAGE_NAMES.get(stage, f"Ступень {stage}")
    stream_label = STREAM_LABELS.get(stream, stream)

    # Человеко-читаемый bottleneck (без cp.NNN)
    bottleneck_human = {
        "cp.rhy": "регулярность и ритм занятий",
        "cp.wld": "мировоззрение и системный взгляд",
        "cp.skl": "учёт времени и собранность",
        "cp.iwe": "рабочая среда и инструменты",
        "cp.int": "системное мышление",
        "cp.agt": "агентность и инициатива",
    }.get(bottleneck, bottleneck)

    valid_str = ""
    if valid_until_iso:
        try:
            dt = datetime.fromisoformat(valid_until_iso)
            valid_str = f"\nАктуально до: {dt.strftime('%B %Y')}"
        except Exception:
            pass

    return (
        f"📊 <b>Результаты диагностики</b>\n\n"
        f"Ступень: <b>{stage_name} ({stage} из 5)</b>\n\n"
        f"Приоритет роста: {bottleneck_human}\n\n"
        f"Рекомендованное руководство: <b>{stream}</b> — {stream_label}"
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

    # Проверить свежий cp-срез (≤30 дней)
    existing = await get_latest_cp_assessment(account_id) if account_id else None
    if existing:
        assessed_at = existing.get("assessed_at", "")
        try:
            dt = datetime.fromisoformat(assessed_at)
            age_days = (datetime.now(timezone.utc) - dt).days
            if age_days < 30:
                stage = existing["stage"]
                stream = existing["recommended_stream"]
                stage_name = STAGE_NAMES.get(stage, f"Ступень {stage}")
                await message.answer(
                    f"У вас уже есть свежая диагностика ({age_days} дн. назад).\n\n"
                    f"Ступень: <b>{stage_name} ({stage} из 5)</b>\n"
                    f"Рекомендованное руководство: <b>{stream}</b>\n\n"
                    f"Следующая диагностика доступна через {30 - age_days} дн.",
                    parse_mode="HTML",
                )
                return
        except Exception:
            pass

    await state.update_data(scores={}, account_id=account_id, q_count=0)
    q = PHASE1_QUESTIONS[0]
    await state.set_state(DiagnoseStates.q1)
    await message.answer(
        "🔬 <b>Диагностика ступени мастерства</b>\n\n"
        "До 5 вопросов с вариантами ответа. Занимает ~3 минуты.\n"
        "Выбирайте то, что ближе всего к вашей текущей практике.\n\n"
        + q["text"],
        parse_mode="HTML",
        reply_markup=_make_scale_keyboard(q["slot"], 0, q["labels"]),
    )


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
    if next_idx < len(PHASE1_QUESTIONS):
        # Следующий якорный вопрос
        nq = PHASE1_QUESTIONS[next_idx]
        states_map = [DiagnoseStates.q1, DiagnoseStates.q2, DiagnoseStates.q3, DiagnoseStates.q4]
        await state.set_state(states_map[next_idx])
        await callback.message.answer(
            nq["text"],
            parse_mode="HTML",
            reply_markup=_make_scale_keyboard(nq["slot"], next_idx, nq["labels"]),
        )
    else:
        # Фаза 1 завершена — определить нужен ли Phase 2
        bottleneck_slot = _determine_phase2_slot(scores)
        if bottleneck_slot and bottleneck_slot in PHASE2_QUESTIONS and q_count < 5:
            p2q = PHASE2_QUESTIONS[bottleneck_slot]
            await state.set_state(DiagnoseStates.q5)
            await callback.message.answer(
                p2q["text"],
                parse_mode="HTML",
                reply_markup=_make_scale_keyboard(p2q["slot"], 99, p2q["labels"]),
            )
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
    """Вычислить профиль, сохранить, показать результат."""
    data = await state.get_data()
    await state.clear()

    account_id = data.get("account_id")
    scores_raw = data.get("scores", {})
    q_count = data.get("q_count", 0)

    # Фаза 3 (bh-прокси): автоматически подтягивает bh-индексы
    # Реализовано через get_bh_proxy (если доступны данные WP-310)
    scores = dict(_DEFAULT_CP)
    scores.update(scores_raw)
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
