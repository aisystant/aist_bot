"""
/remind — пользовательские напоминания (WP-320 Ф3, DP.SC.134).

Команды:
- /remind текст [время]   — создать напоминание
- /remind list            — список активных напоминаний
- /remind cancel <id>     — отмена напоминания

Время (необязательно): «через 2 часа», «через три минуты», «через минуту»,
«через полчаса», «через 1 час 30 минут», «в 15:30», «завтра в 9:00»,
ISO-дата «2026-05-17 15:00», «17 мая 15:00» (абсолютное время — по Москве).
Без времени — через 1 час, и подтверждение прямо это говорит. Время названо
(«через …»), но не разобрано — напоминание не создаётся, бот просит переформулировать.

Пишет в learning.reminder (reminder_type='custom', text=пользовательский текст).
Scheduler подбирает и доставляет через send_user_reminder().
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Moscow is UTC+3 (no DST). Used to interpret user-input HH:MM as Moscow time.
_MOSCOW_UTC_OFFSET = timedelta(hours=3)

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

logger = logging.getLogger(__name__)

remind_router = Router(name="remind")

# ── Время-парсер ─────────────────────────────────────────────────

_MONTHS_RU = {
    "янв": 1, "января": 1,
    "фев": 2, "февраля": 2,
    "мар": 3, "марта": 3,
    "апр": 4, "апреля": 4,
    "май": 5, "мая": 5,
    "июн": 6, "июня": 6,
    "июл": 7, "июля": 7,
    "авг": 8, "августа": 8,
    "сен": 9, "сентября": 9,
    "окт": 10, "октября": 10,
    "ноя": 11, "ноября": 11,
    "дек": 12, "декабря": 12,
}

# Relative times «через …» (WP-568, 14.11: unparsed phrases such as «через минуту»
# or «через три минуты» silently became the +1 h default).
_NUMBER_WORDS = {
    "один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
}
_NUM = r"(\d+|" + "|".join(_NUMBER_WORDS) + r")"
# A unit must not run into the next word: «через 5 мест» is not «через 5 м».
_END = r"(?![а-яёa-z])"
_MIN = r"(?:мин(?:ут[аыу]?|утк[аиу]|уток)?|м)" + _END
_HOUR = r"(?:час(?:а|ов|ик[аов]?)?|ч)" + _END
_DAY = r"(?:ден[ьея]|дн(?:ей|я)?|сут(?:ки|ок)|д)" + _END
_WEEK = r"(?:недел[юиь]|нед)" + _END

# «через 1 час 30 минут», «через час 30 минут», «через 2ч 15м», «через два часа и десять минут»
_RE_THROUGH_COMPOUND = re.compile(
    r"через\s+(?:" + _NUM + r"\s*)?" + _HOUR + r"\s*(?:и\s+)?" + _NUM + r"\s*" + _MIN,
    re.IGNORECASE,
)
# «через полчаса», «через полтора часа»
_RE_THROUGH_HALF = re.compile(r"через\s+(полчаса|полтора\s+часа)" + _END, re.IGNORECASE)
# «через 3 минуты», «через три дня», «через 2 недели»
_RE_THROUGH = re.compile(
    r"через\s+" + _NUM + r"\s*(" + _MIN + "|" + _HOUR + "|" + _DAY + "|" + _WEEK + ")",
    re.IGNORECASE,
)
# «через минуту», «через час», «через день», «через неделю» — one unit
_RE_THROUGH_ONE = re.compile(
    r"через\s+(минуту|час|день|неделю|сутки)" + _END, re.IGNORECASE
)
# The user clearly meant a relative time but none of the patterns above matched —
# refuse instead of guessing. Only whole words count: «через дверь», «через семью»,
# «через поликлинику», «через 2ГИС» name no time and keep the +1 h default.
_TIME_UNIT_WORD = (
    r"(?:сек\w*|мин\w*|м|час\w*|ч|ден[ьея]|дн(?:ей|я)|д|сут\w*|недел\w*|нед|месяц\w*|год\w*|лет)"
)
_RE_TIME_INTENT = re.compile(
    r"через\s+(?:"
    r"\d+\S*?\s*" + _TIME_UNIT_WORD + _END  # «через 2x часа», «через 3 месяца»
    + r"|(?:пол(?:часа|часика|тора|дня)|пар[уы]|несколько|секунду|" + "|".join(_NUMBER_WORDS)
    + r"|минуту|час|день|сутки|неделю|месяц|год)" + _END
    + r")",
    re.IGNORECASE,
)
# "в HH:MM" или "в H:MM"
_RE_AT_TIME = re.compile(r"в\s+(\d{1,2}):(\d{2})", re.IGNORECASE)
# "завтра [в HH:MM]"
_RE_TOMORROW = re.compile(r"завтра(?:\s+в\s+(\d{1,2}):(\d{2}))?", re.IGNORECASE)
# "17 мая [в] HH:MM" или "17 мая"
_RE_DATE_RU = re.compile(
    r"(\d{1,2})\s+(" + "|".join(_MONTHS_RU) + r")(?:\s+(?:в\s+)?(\d{1,2}):(\d{2}))?",
    re.IGNORECASE,
)
# ISO "2026-05-17 15:00" or "2026-05-17T15:00"
_RE_ISO = re.compile(r"(\d{4}-\d{2}-\d{2})[T\s](\d{1,2}:\d{2})")


def _to_int(token: str) -> int:
    return int(token) if token.isdigit() else _NUMBER_WORDS[token.lower()]


def _unit_delta(n: int, unit: str) -> timedelta:
    unit = unit.lower()
    if unit.startswith("м"):
        return timedelta(minutes=n)
    if unit.startswith("ч"):
        return timedelta(hours=n)
    if unit.startswith("нед"):
        return timedelta(weeks=n)
    return timedelta(days=n)


def _cut(text: str, m: re.Match) -> str:
    """Text without the matched time phrase."""
    return text[: m.start()].strip() + " " + text[m.end() :].strip()


def has_time_intent(text: str) -> bool:
    """True when the text names a relative time that _parse_time may fail to read."""
    return bool(_RE_TIME_INTENT.search(text))


def _parse_time(text: str) -> tuple[datetime | None, str]:
    """Return (scheduled_for_utc_naive, remaining_text_without_time_part).

    Relative times ("через N"): UTC-based, no offset needed.
    Absolute times ("в HH:MM", "завтра", dates, ISO): user inputs Moscow time;
    we subtract _MOSCOW_UTC_OFFSET before storing so the DB always holds UTC.
    """
    now_utc = datetime.utcnow()
    now_moscow = now_utc + _MOSCOW_UTC_OFFSET  # naive Moscow time for absolute comparisons

    m = _RE_THROUGH_COMPOUND.search(text)
    if m:
        hours = _to_int(m.group(1)) if m.group(1) else 1
        delta = timedelta(hours=hours, minutes=_to_int(m.group(2)))
        return now_utc + delta, _cut(text, m)

    m = _RE_THROUGH_HALF.search(text)
    if m:
        minutes = 30 if m.group(1).lower() == "полчаса" else 90
        return now_utc + timedelta(minutes=minutes), _cut(text, m)

    m = _RE_THROUGH.search(text)
    if m:
        return now_utc + _unit_delta(_to_int(m.group(1)), m.group(2)), _cut(text, m)

    m = _RE_THROUGH_ONE.search(text)
    if m:
        unit = m.group(1).lower()
        delta = timedelta(days=1) if unit == "сутки" else _unit_delta(1, unit)
        return now_utc + delta, _cut(text, m)

    m = _RE_TOMORROW.search(text)
    if m:
        h = int(m.group(1)) if m.group(1) else 9
        mn = int(m.group(2)) if m.group(2) else 0
        dt_moscow = (now_moscow + timedelta(days=1)).replace(hour=h, minute=mn, second=0, microsecond=0)
        return dt_moscow - _MOSCOW_UTC_OFFSET, _cut(text, m)

    m = _RE_DATE_RU.search(text)
    if m:
        day = int(m.group(1))
        month = _MONTHS_RU[m.group(2).lower()]
        h = int(m.group(3)) if m.group(3) else 9
        mn = int(m.group(4)) if m.group(4) else 0
        year = now_utc.year
        dt_moscow = datetime(year, month, day, h, mn)
        if dt_moscow - _MOSCOW_UTC_OFFSET < now_utc:
            dt_moscow = dt_moscow.replace(year=year + 1)
        return dt_moscow - _MOSCOW_UTC_OFFSET, _cut(text, m)

    m = _RE_ISO.search(text)
    if m:
        dt_str = m.group(1) + " " + m.group(2)
        try:
            dt_moscow = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
            return dt_moscow - _MOSCOW_UTC_OFFSET, _cut(text, m)
        except ValueError:
            pass

    m = _RE_AT_TIME.search(text)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        dt_moscow = now_moscow.replace(hour=h, minute=mn, second=0, microsecond=0)
        if dt_moscow <= now_moscow:
            dt_moscow += timedelta(days=1)
        return dt_moscow - _MOSCOW_UTC_OFFSET, _cut(text, m)

    return None, text


_MOSCOW_TZ = ZoneInfo("Europe/Moscow")

DEFAULT_TIME_NOTE = "через 1 час (время не указано)"
UNPARSED_TIME_REPLY = (
    "Не понял время напоминания, поэтому ничего не поставил.\n\n"
    "Так понимаю:\n"
    "• через 3 минуты, через три минуты, через минуту\n"
    "• через полчаса, через 1 час 30 минут, через 2ч 15м\n"
    "• в 18:30, завтра в 10:00, 17 мая 15:00\n\n"
    "Без времени напомню через 1 час."
)


def _format_dt(dt_utc_naive: datetime) -> str:
    """UTC-naive value from the database shown as Moscow time (users enter Moscow time)."""
    local = dt_utc_naive.replace(tzinfo=timezone.utc).astimezone(_MOSCOW_TZ)
    return local.strftime("%d.%m.%Y %H:%M") + " МСК"


# ── Обработчики ──────────────────────────────────────────────────

@remind_router.message(Command("remind"))
async def cmd_remind(message: Message):
    chat_id = message.chat.id
    raw = (message.text or "").strip()
    # Remove "/remind" prefix (with optional @bot_username)
    body = re.sub(r"^/remind(?:@\w+)?", "", raw, count=1).strip()

    if not body:
        await message.answer(
            "Формат: /remind текст [время]\n\n"
            "Примеры:\n"
            "/remind позвонить в IND через 2 часа\n"
            "/remind встреча завтра в 10:00\n"
            "/remind купить хлеб в 18:30\n\n"
            "/remind list — мои напоминания\n"
            "/remind cancel <id> — отменить"
        )
        return

    if body.lower() == "list":
        await _cmd_list(message, chat_id)
        return

    m = re.match(r"cancel\s+(\d+)$", body, re.IGNORECASE)
    if m:
        await _cmd_cancel(message, chat_id, int(m.group(1)))
        return

    scheduled_for, text = _parse_time(body)
    text = " ".join(text.split())  # normalize whitespace

    if not text:
        await message.answer("Не получилось разобрать текст напоминания. Попробуй снова.")
        return

    time_note = ""
    if scheduled_for is None:
        if has_time_intent(body):
            logger.info("[Remind] time not understood for chat_id=%s", chat_id)
            await message.answer(UNPARSED_TIME_REPLY)
            return
        scheduled_for = datetime.utcnow() + timedelta(hours=1)
        time_note = f" — {DEFAULT_TIME_NOTE}"

    try:
        from db.connection import get_learning_pool
        pool = await get_learning_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "ALTER TABLE public.reminder ADD COLUMN IF NOT EXISTS text TEXT"
            )
            # bot_id колонку гарантирует миграция 024 (WP-212 Layer 1) при старте.
            row = await conn.fetchrow(
                """INSERT INTO public.reminder (chat_id, reminder_type, scheduled_for, text, bot_id)
                   VALUES ($1, 'custom', $2, $3, $4)
                   RETURNING id""",
                chat_id, scheduled_for, text, message.bot.id,
            )
        reminder_id = row["id"]
    except Exception:
        logger.exception("[Remind] insert failed for chat_id=%s", chat_id)
        await message.answer("Не удалось сохранить напоминание. Попробуй позже.")
        return

    await message.answer(
        f"🔔 Напомню: «{text}»\n"
        f"📅 {_format_dt(scheduled_for)}{time_note}\n\n"
        f"ID: {reminder_id} (для отмены: /remind cancel {reminder_id})"
    )


async def _cmd_list(message: Message, chat_id: int):
    try:
        from db.connection import get_learning_pool
        pool = await get_learning_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, text, scheduled_for
                   FROM public.reminder
                   WHERE chat_id = $1 AND sent = FALSE AND reminder_type = 'custom'
                   ORDER BY scheduled_for
                   LIMIT 10""",
                chat_id,
            )
    except Exception:
        logger.exception("[Remind] list failed for chat_id=%s", chat_id)
        await message.answer("Не удалось загрузить напоминания.")
        return

    if not rows:
        await message.answer("У тебя нет активных напоминаний.")
        return

    lines = ["Активные напоминания:"]
    for row in rows:
        text = row["text"] or "—"
        dt = _format_dt(row["scheduled_for"])
        lines.append(f"• [{row['id']}] {dt} — {text}")
    lines.append("\nОтмена: /remind cancel <id>")
    await message.answer("\n".join(lines))


async def _cmd_cancel(message: Message, chat_id: int, reminder_id: int):
    try:
        from db.connection import get_learning_pool
        pool = await get_learning_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """DELETE FROM public.reminder
                   WHERE id = $1 AND chat_id = $2 AND sent = FALSE AND reminder_type = 'custom'""",
                reminder_id, chat_id,
            )
    except Exception:
        logger.exception("[Remind] cancel failed for chat_id=%s id=%s", chat_id, reminder_id)
        await message.answer("Не удалось отменить напоминание.")
        return

    deleted = int(result.split()[-1])
    if deleted:
        await message.answer(f"Напоминание {reminder_id} отменено.")
    else:
        await message.answer(f"Напоминание {reminder_id} не найдено или уже отправлено.")
