from __future__ import annotations

"""
Хендлеры для записи учебного времени пилота (WP-310 Ф13b/c/d).

Команды:
- /slot [N] — active source: записать часы саморазвития сегодня
- /onboarding_time — wizard backfill: среднее ч/нед × N недель
- /edit_time — просмотр + редактирование последних 14 записей slot_logged

Все три пишут event_type='slot_logged' с payload.source ∈
{active, self_report_backfill, self_report_daily, self_report_weekly}.
Единый event_type для stage_evaluator (уже читает slot_logged).

Privacy guard: все UPDATE/DELETE включают AND user_id = $1 (L2-PRIVACY).
"""

import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Optional

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

from db.queries import get_intern
from db.queries.identity import get_user_uuid

logger = logging.getLogger(__name__)

slot_router = Router(name="slot")


# ── FSM States ─────────────────────────────────────────────────────────────

class OnboardingTimeStates(StatesGroup):
    """Wizard /onboarding_time: часы → период → подтверждение."""
    waiting_hours = State()
    waiting_weeks = State()
    confirm_backfill = State()


class EditTimeStates(StatesGroup):
    """Wizard /edit_time: редактирование конкретной записи."""
    waiting_new_value = State()


# ── Helpers ────────────────────────────────────────────────────────────────

_HOURS_PATTERN = re.compile(
    r'^\s*(\d+(?:[.,]\d+)?)\s*(?:ч|час(?:а|ов)?)?\s*$',
    re.IGNORECASE | re.UNICODE,
)


def _parse_hours(text: str) -> Optional[float]:
    """Извлечь число часов из строки. Поддерживает "1.5", "1,5", "2ч", "2 часа"."""
    m = _HOURS_PATTERN.match(text.strip())
    if not m:
        return None
    val = float(m.group(1).replace(',', '.'))
    if not (0.1 <= val <= 24):
        return None
    return val


def _slot_inline_keyboard() -> InlineKeyboardMarkup:
    """Кнопки быстрого выбора часов для /slot."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="0.5", callback_data="slot:0.5"),
            InlineKeyboardButton(text="1", callback_data="slot:1"),
            InlineKeyboardButton(text="1.5", callback_data="slot:1.5"),
            InlineKeyboardButton(text="2", callback_data="slot:2"),
        ],
        [
            InlineKeyboardButton(text="3", callback_data="slot:3"),
            InlineKeyboardButton(text="4", callback_data="slot:4"),
            InlineKeyboardButton(text="Свой ввод", callback_data="slot:custom"),
        ],
    ])


def _hours_inline_keyboard() -> InlineKeyboardMarkup:
    """Кнопки среднего ч/нед для /onboarding_time шаг 1."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="2", callback_data="ot_hours:2"),
            InlineKeyboardButton(text="4", callback_data="ot_hours:4"),
            InlineKeyboardButton(text="6", callback_data="ot_hours:6"),
        ],
        [
            InlineKeyboardButton(text="8", callback_data="ot_hours:8"),
            InlineKeyboardButton(text="10", callback_data="ot_hours:10"),
            InlineKeyboardButton(text="Свой ввод", callback_data="ot_hours:custom"),
        ],
    ])


def _weeks_inline_keyboard() -> InlineKeyboardMarkup:
    """Кнопки периода для /onboarding_time шаг 2."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="более 1 нед", callback_data="ot_weeks:1"),
            InlineKeyboardButton(text="более 1 мес", callback_data="ot_weeks:4"),
            InlineKeyboardButton(text="более 2 мес", callback_data="ot_weeks:8"),
        ],
        [
            InlineKeyboardButton(text="более квартала", callback_data="ot_weeks:12"),
            InlineKeyboardButton(text="более полугода", callback_data="ot_weeks:24"),
        ],
    ])


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Да, записать", callback_data="ot_confirm:yes"),
            InlineKeyboardButton(text="Отменить", callback_data="ot_confirm:no"),
        ]
    ])


async def _get_weekly_stats(user_id: int) -> tuple[float, float]:
    """Сумма за 7 дней и среднее ч/нед по slot_logged.

    Returns: (sum_7d, avg_per_week)
    """
    try:
        from db.connection import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT payload
                FROM development.user_events
                WHERE user_id = $1
                  AND event_type = 'slot_logged'
                  AND created_at >= NOW() - INTERVAL '7 days'
            ''', user_id)
        total = 0.0
        for r in rows:
            p = r['payload']
            if isinstance(p, str):
                p = json.loads(p)
            total += float((p or {}).get('hours', 0))
        return total, total  # 7 дней = 1 нед, avg = total
    except Exception as e:
        logger.warning(f"[slot] _get_weekly_stats error: {e}")
        return 0.0, 0.0


def _stage_weekly_goal(stage: Optional[int]) -> Optional[float]:
    """Целевые ч/нед по ступени (FORM.089 §12.2)."""
    goals = {1: 2.0, 2: 5.0, 3: 6.0, 4: 8.0, 5: 10.0}
    return goals.get(stage) if stage else None


async def _get_user_stage(user_id: int) -> Optional[int]:
    """Получить текущую ступень пользователя из ЦД (best-effort)."""
    try:
        from db.queries import get_intern
        intern = await get_intern(user_id)
        if not intern:
            return None
        user_uuid = intern.get('user_uuid') or await get_user_uuid(user_id)
        if not user_uuid:
            return None
        from db.connection import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow('''
                SELECT data->'3_derived'->'3_4_qualification'->>'stage' as stage
                FROM digital_twins
                WHERE user_id = $1::text
                LIMIT 1
            ''', str(user_uuid))
        if row and row['stage']:
            return int(row['stage'])
    except Exception as e:
        logger.debug(f"[slot] _get_user_stage fallback: {e}")
    return None


async def _send_slot_summary(message: Message, user_id: int, hours: float) -> None:
    """Отправить сводку после записи слота."""
    sum_7d, _ = await _get_weekly_stats(user_id)
    stage = await _get_user_stage(user_id)
    goal = _stage_weekly_goal(stage)

    lines = [f"✅ Записал {hours} ч саморазвития сегодня."]
    lines.append(f"За последние 7 дней: {sum_7d:.1f} ч.")
    if goal and stage:
        lines.append(f"Цель на ступени {stage}: {goal} ч/нед.")

    await message.answer("\n".join(lines))


# ── /slot command ──────────────────────────────────────────────────────────

@slot_router.message(Command("slot"))
async def cmd_slot(message: Message, state: FSMContext) -> None:
    """/slot [N] — записать часы саморазвития сегодня."""
    user_id = message.chat.id
    text = message.text or ""
    parts = text.strip().split(maxsplit=1)
    arg = parts[1] if len(parts) > 1 else None

    if arg:
        hours = _parse_hours(arg)
        if hours is None:
            await message.answer(
                "Не удалось распознать число часов. Пример: /slot 1.5 или /slot 2 часа"
            )
            return
        await _do_log_slot(message, user_id, hours)
        return

    # Нет аргумента → показать кнопки
    await message.answer(
        "Сколько часов саморазвития сегодня?",
        reply_markup=_slot_inline_keyboard(),
    )


@slot_router.callback_query(F.data.startswith("slot:"))
async def on_slot_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор часов через inline-кнопки для /slot."""
    await callback.answer()
    value = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id

    if value == "custom":
        await state.update_data(slot_custom_pending=True)
        await callback.message.answer("Введи число часов (например, 2.5):")
        await state.set_state(EditTimeStates.waiting_new_value)
        # Re-use EditTimeStates temporarily — distinguish by fsm data
        await state.update_data(slot_custom_pending=True, edit_event_id=None)
        return

    try:
        hours = float(value)
    except ValueError:
        await callback.message.answer("Ошибка формата. Попробуй /slot")
        return

    if not (0.1 <= hours <= 24):
        await callback.message.answer("Некорректное число часов.")
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await _do_log_slot(callback.message, user_id, hours)


async def _do_log_slot(message: Message, user_id: int, hours: float) -> None:
    """Записать slot_logged event и отправить сводку."""
    user_uuid = await get_user_uuid(user_id)
    if user_uuid is None:
        await message.answer(
            "Сначала привяжи аккаунт через /start. "
            "Без привязки время не учтётся в твоей ступени."
        )
        return

    from db.queries.events import log_event
    await log_event(
        user_id=user_id,
        event_type="slot_logged",
        payload={"hours": hours, "source": "active", "confidence": "measured"},
    )
    await _send_slot_summary(message, user_id, hours)


# ── /onboarding_time wizard ────────────────────────────────────────────────

@slot_router.message(Command("onboarding_time"))
async def cmd_onboarding_time(message: Message, state: FSMContext) -> None:
    """/onboarding_time — wizard для backfill исторических часов."""
    await state.clear()
    user_uuid = await get_user_uuid(message.chat.id)
    if user_uuid is None:
        await message.answer(
            "Сначала привяжи аккаунт через /start. "
            "Без привязки время не учтётся в твоей ступени."
        )
        return

    await message.answer(
        "Сколько в среднем часов в неделю ты инвестируешь на саморазвитие?",
        reply_markup=_hours_inline_keyboard(),
    )
    await state.set_state(OnboardingTimeStates.waiting_hours)


@slot_router.callback_query(OnboardingTimeStates.waiting_hours, F.data.startswith("ot_hours:"))
async def on_ot_hours_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор ч/нед через кнопку (шаг 1 wizard)."""
    await callback.answer()
    value = callback.data.split(":", 1)[1]

    if value == "custom":
        await callback.message.answer("Введи среднее количество часов в неделю (например, 3.5):")
        # Остаёмся в том же стейте — следующее сообщение будет текстовым
        return

    try:
        hours = float(value)
    except ValueError:
        await callback.message.answer("Ошибка формата. Попробуй /onboarding_time")
        await state.clear()
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await state.update_data(ot_hours=hours)
    await callback.message.answer(
        f"Понял: {hours} ч/нед.\n\nУкажи период в месяцах:",
        reply_markup=_weeks_inline_keyboard(),
    )
    await state.set_state(OnboardingTimeStates.waiting_weeks)


@slot_router.message(OnboardingTimeStates.waiting_hours)
async def on_ot_hours_text(message: Message, state: FSMContext) -> None:
    """Ручной ввод ч/нед (шаг 1 wizard)."""
    hours = _parse_hours(message.text or "")
    if hours is None:
        await message.answer("Введи число часов, например: 3.5")
        return

    await state.update_data(ot_hours=hours)
    await message.answer(
        f"Понял: {hours} ч/нед.\n\nУкажи период в месяцах:",
        reply_markup=_weeks_inline_keyboard(),
    )
    await state.set_state(OnboardingTimeStates.waiting_weeks)


@slot_router.callback_query(OnboardingTimeStates.waiting_weeks, F.data.startswith("ot_weeks:"))
async def on_ot_weeks_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор периода через кнопку (шаг 2 wizard)."""
    await callback.answer()
    value = callback.data.split(":", 1)[1]

    try:
        weeks = int(value)
    except ValueError:
        await callback.message.answer("Ошибка формата. Попробуй /onboarding_time")
        await state.clear()
        return

    data = await state.get_data()
    ot_hours = data.get("ot_hours", 0)

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await state.update_data(ot_weeks=weeks)
    total = round(ot_hours * weeks, 1)
    total_days = weeks * 7
    hours_per_day = round(total / total_days, 2)

    await callback.message.answer(
        f"Подтверди:\n"
        f"{ot_hours} ч/нед × {weeks} нед = {total} ч\n"
        f"({hours_per_day} ч/день — {total_days} синтетических событий за {weeks} нед).\n\n"
        "Это будут синтетические события за период. Записать?",
        reply_markup=_confirm_keyboard(),
    )
    await state.set_state(OnboardingTimeStates.confirm_backfill)


@slot_router.callback_query(OnboardingTimeStates.confirm_backfill, F.data.startswith("ot_confirm:"))
async def on_ot_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждение или отмена backfill."""
    await callback.answer()
    answer = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id

    if answer == "no":
        await state.clear()
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer("Отменено.")
        return

    data = await state.get_data()
    ot_hours = data.get("ot_hours", 0)
    ot_weeks = data.get("ot_weeks", 0)
    await state.clear()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    user_uuid = await get_user_uuid(user_id)
    if user_uuid is None:
        await callback.message.answer(
            "Аккаунт не привязан. Сначала пройди /start."
        )
        return

    total_days = ot_weeks * 7
    hours_per_day = round(ot_hours * ot_weeks / total_days, 2) if total_days > 0 else 0.0
    batch_id = str(uuid.uuid4())
    total = round(ot_hours * ot_weeks, 1)

    await callback.message.answer(f"⏳ Записываю {total_days} событий...")

    # Batch INSERT — один запрос вместо N последовательных
    now = datetime.utcnow()
    records = []
    for i in range(total_days):
        occurred_at = now - timedelta(days=total_days - i - 1)
        records.append((
            user_id,
            "slot_logged",
            "bot",
            json.dumps({
                "hours": hours_per_day,
                "source": "self_report_backfill",
                "confidence": "estimated",
                "batch_id": batch_id,
            }),
            1.0,
            [],
            user_uuid,
            occurred_at,
        ))

    failed = 0
    try:
        from db.connection import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.executemany('''
                INSERT INTO development.user_events
                    (user_id, event_type, source, payload, confidence,
                     skill_ids, user_uuid, created_at)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8)
                ON CONFLICT DO NOTHING
            ''', records)
    except Exception as e:
        logger.error(f"[slot] batch backfill failed: {e}")
        failed = total_days

    ok = total_days - failed
    if failed:
        msg = (
            f"⚠️ Не удалось записать события (ошибка БД).\n"
            f"Записалось: {ok}/{total_days}. Попробуй /onboarding_time ещё раз."
        )
    else:
        msg = (
            f"✅ Записал {total} ч за {ot_weeks} нед "
            f"({ok} событий).\n\n"
            "Пересчёт ступени происходит каждые 4 часа. "
            "Проверь результат через /twin."
        )
    await callback.message.answer(msg)


# ── /edit_time ─────────────────────────────────────────────────────────────

@slot_router.message(Command("edit_time"))
async def cmd_edit_time(message: Message, state: FSMContext) -> None:
    """/edit_time — показать последние 14 записей slot_logged с кнопками редактирования."""
    await state.clear()
    user_id = message.chat.id
    rows = await _fetch_slot_rows(user_id, limit=14)

    if not rows:
        await message.answer("Нет записей slot_logged. Используй /slot чтобы добавить.")
        return

    await _send_edit_list(message, rows, user_id)


async def _fetch_slot_rows(user_id: int, limit: int = 14) -> list[dict]:
    """Получить последние N записей slot_logged для пользователя."""
    try:
        from db.connection import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT id, payload, created_at
                FROM development.user_events
                WHERE user_id = $1 AND event_type = 'slot_logged'
                ORDER BY created_at DESC
                LIMIT $2
            ''', user_id, limit)
        result = []
        for r in rows:
            p = r['payload']
            if isinstance(p, str):
                p = json.loads(p)
            result.append({
                'id': r['id'],
                'hours': float((p or {}).get('hours', 0)),
                'source': (p or {}).get('source', 'unknown'),
                'created_at': r['created_at'],
            })
        return result
    except Exception as e:
        logger.warning(f"[slot] _fetch_slot_rows error: {e}")
        return []


def _source_label(source: str) -> str:
    labels = {
        'active': 'active',
        'self_report_backfill': 'backfill',
        'self_report_daily': 'daily',
        'self_report_weekly': 'weekly',
    }
    return labels.get(source, source[:10])


async def _send_edit_list(message: Message, rows: list[dict], user_id: int) -> None:
    """Отправить список записей с кнопками редактирования и удаления."""
    lines = ["*Последние записи slot_logged:*\n"]
    buttons = []
    for r in rows:
        dt = r['created_at']
        date_str = dt.strftime('%Y-%m-%d') if hasattr(dt, 'strftime') else str(dt)[:10]
        src = _source_label(r['source'])
        lines.append(f"📅 {date_str}  ⏱ {r['hours']} ч  ({src})")
        buttons.append([
            InlineKeyboardButton(
                text=f"✏️ {date_str} {r['hours']}ч",
                callback_data=f"et_edit:{r['id']}:{r['hours']}",
            ),
            InlineKeyboardButton(
                text="🗑",
                callback_data=f"et_del:{r['id']}:{date_str}:{r['hours']}",
            ),
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


@slot_router.callback_query(F.data.startswith("et_edit:"))
async def on_et_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Начать редактирование конкретной записи."""
    await callback.answer()
    parts = callback.data.split(":", 2)
    event_id = int(parts[1])
    current_hours = parts[2]

    await state.set_state(EditTimeStates.waiting_new_value)
    await state.update_data(edit_event_id=event_id, slot_custom_pending=False)

    await callback.message.answer(
        f"Текущее: {current_hours} ч. Введи новое значение (число от 0.1 до 24):"
    )


@slot_router.callback_query(F.data.startswith("et_del:"))
async def on_et_del(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждение удаления записи."""
    await callback.answer()
    parts = callback.data.split(":", 3)
    event_id = int(parts[1])
    date_str = parts[2]
    hours = parts[3]

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Да, удалить", callback_data=f"et_del_ok:{event_id}"),
            InlineKeyboardButton(text="Отмена", callback_data="et_del_cancel"),
        ]
    ])
    await callback.message.answer(
        f"Удалить запись {date_str} {hours} ч?",
        reply_markup=keyboard,
    )


@slot_router.callback_query(F.data.startswith("et_del_ok:"))
async def on_et_del_ok(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждённое удаление записи. Privacy guard: AND user_id = $1."""
    await callback.answer()
    event_id = int(callback.data.split(":", 1)[1])
    user_id = callback.from_user.id

    try:
        from db.connection import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute('''
                DELETE FROM development.user_events
                WHERE id = $1 AND user_id = $2
            ''', event_id, user_id)
        if result == "DELETE 0":
            await callback.message.answer("Запись не найдена или уже удалена.")
        else:
            await callback.message.answer("🗑 Удалено.")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    except Exception as e:
        logger.error(f"[slot] delete event {event_id} error: {e}")
        await callback.message.answer(f"Ошибка удаления: {e}")


@slot_router.callback_query(F.data == "et_del_cancel")
async def on_et_del_cancel(callback: CallbackQuery) -> None:
    """Отмена удаления."""
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer("Отменено.")


@slot_router.message(EditTimeStates.waiting_new_value)
async def on_et_new_value(message: Message, state: FSMContext) -> None:
    """Получить новое значение часов и обновить запись (или записать /slot custom)."""
    data = await state.get_data()
    edit_event_id = data.get("edit_event_id")
    slot_custom = data.get("slot_custom_pending", False)
    await state.clear()

    hours = _parse_hours(message.text or "")
    if hours is None:
        await message.answer("Некорректное число. Введи часы от 0.1 до 24.")
        return

    user_id = message.chat.id

    # Путь 1: custom-ввод для /slot
    if slot_custom or edit_event_id is None:
        await _do_log_slot(message, user_id, hours)
        return

    # Путь 2: обновление существующей записи. Privacy guard: AND user_id = $1
    try:
        from db.connection import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute('''
                UPDATE development.user_events
                SET payload = jsonb_set(payload::jsonb, '{hours}', $3::text::jsonb)
                WHERE id = $1 AND user_id = $2
            ''', edit_event_id, user_id, str(hours))
        if result == "UPDATE 0":
            await message.answer("Запись не найдена или уже изменена.")
        else:
            await message.answer(f"✅ Обновлено: {hours} ч.")
    except Exception as e:
        logger.error(f"[slot] update event {edit_event_id} error: {e}")
        await message.answer(f"Ошибка обновления: {e}")


# ── Ф13c: ежедневный prompt от scheduler (22:00 МСК) ─────────────────────

@slot_router.callback_query(F.data.startswith("slot_daily:"))
async def on_slot_daily_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """Callback для ежедневного prompt — записать часы или пропустить (Ф13c, WP-310).

    Payload: slot_daily:0.5 / slot_daily:1.0 / slot_daily:2.0 / slot_daily:skip
    """
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    value = callback.data.split(":", 1)[1]
    if value == "skip":
        await callback.answer("OK, нет проблем — запишешь позже через /slot.")
        return

    hours = float(value)
    user_id = callback.message.chat.id
    await callback.answer()
    await _do_log_slot(callback.message, user_id, hours)
