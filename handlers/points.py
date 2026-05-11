"""
/points — баланс баллов + последние начисления с разложением.

WP-306 (WP-121 Ф3): read-only surface над `rewards.point_balances` + `applied_events`.

Источник истины: Neon БД `rewards` (writer — multi-domain-projection-worker,
DP.ROLE.034, DP.SC.122). Бот не пересчитывает баллы — показывает то, что
projection-worker уже свернул из `learning.domain_event`.

Разложение в applied_events:
  effective = min(base × dom_mult × qual_mult × streak_mult, daily_cap)
  cap_truncated = True если effective упёрся в daily_cap
"""

import logging

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

from db.queries import get_intern
from db.queries.users import is_onboarded
from db.queries.rewards import get_points_balance, get_recent_applied_events
from i18n import t

logger = logging.getLogger(__name__)

points_router = Router(name="points")


_EVENT_LABELS = {
    "lesson_completed": "📖 Урок",
    "training_passed": "✅ Тренировка",
    "test_passed": "🧪 Тест",
    "marathon_tasks": "🏃 Задание марафона",
    "marathon_step": "🏃 Шаг марафона",
    "qualification_granted": "🏆 Квалификация",
    "payment_received": "💳 Оплата",
    "strategy_session_completed": "🎯 Стратегсессия",
    "knowledge_extracted": "💡 Извлечение знания",
    "day_plan_opened": "🌅 Day Open",
    "day_plan_closed": "🌆 Day Close",
    "day_close": "🌆 Day Close",
    "slot_logged": "⏱ Слот",
    "week_plan_closed": "📅 Закрытие недели",
    "month_plan_closed": "📆 Закрытие месяца",
    "pack_updated": "📦 Pack",
    "iwe_session": "💻 IWE сессия",
    "wp_created": "📋 РП создан",
    "wp_closed": "✅ РП закрыт",
    "wp_completed": "✅ РП закрыт",
    "git_commit": "⚙️ Коммит",
    "commit_created": "⚙️ Коммит",
    "note_to_capture": "📝 Заметка",
    "topic_created": "💬 Тема в клубе",
    "comment_created": "💬 Комментарий",
    "distinction_added": "🔍 Различение",
    "method_described": "📚 Метод",
    "pomodoro_completed": "🍅 Помодоро",
    "fmt_commit_merged": "🏗 FMT merge",
}


def _event_label(event_type: str) -> str:
    return _EVENT_LABELS.get(event_type, f"• {event_type}")


def _format_event(ev: dict) -> str:
    """Одна строка детализации.

    Формат: `<emoji> <label> — <effective> (base × dom × qual × streak)`
    Если cap_truncated — приписка «(cap)».
    """
    label = _event_label(ev["event_type"])
    base = ev["base_amount"]
    dom = ev["dom_mult"]
    qual = ev["qual_mult"]
    streak = ev["streak_mult"]
    eff = ev["effective"]
    capped = ev.get("cap_truncated", False)

    breakdown = f"{base:g} × {dom:g} × {qual:g} × {streak:g}"
    cap_mark = " <i>(лимит дня)</i>" if capped else ""
    return f"<b>+{eff:g}</b> · {label}\n   <i>{breakdown}</i>{cap_mark}"


@points_router.message(Command("points"))
async def cmd_points(message: Message):
    """Баланс баллов + последние 10 начислений с разложением."""
    chat_id = message.chat.id
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') if intern else 'ru'

    if not await is_onboarded(intern):
        await message.answer(t('profile.first_start', lang))
        return

    account_id = intern.get('dt_user_id')
    if not account_id:
        await message.answer(
            "🏆 <b>Баллы</b>\n\n"
            "Аккаунт ещё не привязан к Aisystant.\n"
            "Привяжите профиль в /settings, чтобы начать копить баллы.",
            parse_mode="HTML",
        )
        return

    try:
        balance = await get_points_balance(account_id)
        events = await get_recent_applied_events(account_id, limit=10)
    except Exception as e:
        logger.error(f"[/points] chat_id={chat_id}: {e}")
        await message.answer(t('errors.processing_error', lang))
        return

    balance_text = f"{int(balance)}" if balance is not None else "0"
    text = f"🏆 <b>Баллы:</b> {balance_text}\n\n"

    if not events:
        text += "<i>Пока нет начислений. Учитесь, делайте уроки, закрывайте РП — баллы появятся.</i>"
    else:
        text += "<b>Последние начисления:</b>\n\n"
        text += "\n\n".join(_format_event(ev) for ev in events)
        text += "\n\n<i>Разложение: база × домен × квалификация × streak. «лимит дня» — упёрлось в дневной cap квалификации.</i>"

    try:
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"[/points] HTML render failed for chat_id={chat_id}: {e}")
        # Fallback без форматирования (см. CLAUDE.md §10.2)
        await message.answer(text.replace("<b>", "").replace("</b>", "")
                             .replace("<i>", "").replace("</i>", ""))
