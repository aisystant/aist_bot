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
from datetime import datetime, timezone

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

from db.queries import get_intern
from db.queries.users import is_onboarded
from db.queries.rewards import (
    get_points_balance,
    get_recent_applied_events,
    get_today_total,
    get_active_reward_rules,
)
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


def _relative_time(applied_at) -> str:
    """Относительное время: «5 мин назад», «3 ч назад», «2 дн назад», «12 апр»."""
    if not applied_at:
        return ""
    try:
        now = datetime.now(timezone.utc)
        delta = now - applied_at
        secs = int(delta.total_seconds())
        if secs < 60:
            return "только что"
        if secs < 3600:
            return f"{secs // 60} мин назад"
        if secs < 86400:
            return f"{secs // 3600} ч назад"
        if secs < 7 * 86400:
            return f"{secs // 86400} дн назад"
        return applied_at.strftime("%d.%m")
    except Exception:
        return ""


def _format_event(ev: dict) -> str:
    """Одна строка детализации.

    Формат: `+effective · label (HH мин назад)` + разложение base × dom × qual × streak.
    """
    try:
        label = _event_label(ev["event_type"])
        base = float(ev["base_amount"] or 0)
        dom = float(ev["dom_mult"] or 1)
        qual = float(ev["qual_mult"] or 1)
        streak = float(ev["streak_mult"] or 1)
        eff = float(ev["effective"] or 0)
        capped = ev.get("cap_truncated", False)
        when = _relative_time(ev.get("applied_at"))

        when_str = f" <i>· {when}</i>" if when else ""
        breakdown = f"{base:g} × {dom:g} × {qual:g} × {streak:g}"
        cap_mark = " <i>(лимит дня)</i>" if capped else ""
        return f"<b>+{eff:g}</b> · {label}{when_str}\n   <i>{breakdown}</i>{cap_mark}"
    except Exception as e:
        logger.warning(f"[/points] _format_event failed: {e} (ev={ev.get('event_id')})")
        return f"• {ev.get('event_type', '?')} (ошибка отображения)"


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
        today_total = await get_today_total(account_id)
    except Exception as e:
        logger.error(f"[/points] chat_id={chat_id}: {e}")
        await message.answer(t('errors.processing_error', lang))
        return

    balance_text = f"{float(balance):g}" if balance is not None else "0"
    today_text = f" <i>(+{float(today_total):g} за сегодня)</i>" if today_total and float(today_total) > 0 else ""
    text = f"🏆 <b>Баллы:</b> {balance_text}{today_text}\n\n"

    if not events:
        # WP-311 Ф7 (DP.SC.136 critère «Honesty»): объясняем причину 0 баллов
        text += (
            "<i>Пока нет начислений.</i>\n\n"
            "Возможные причины:\n"
            "• Не оформлено согласие на учёт активности — пройди /consent\n"
            "• Нет действий за период — закрывай день (/day_close), "
            "делай уроки, фиксируй слоты саморазвития, коммить в свои репозитории\n\n"
            "Полный список действий, дающих баллы: /rules"
        )
    else:
        text += "<b>Последние начисления:</b>\n\n"
        text += "\n\n".join(_format_event(ev) for ev in events)
        text += (
            "\n\n<i>Разложение: база × домен × квалификация × серия. "
            "«лимит дня» — упёрлось в дневной cap квалификации.</i>\n"
            "<i>Все правила: /rules</i>"
        )

    try:
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"[/points] HTML render failed for chat_id={chat_id}: {e}")
        # Fallback без форматирования (см. CLAUDE.md §10.2)
        await message.answer(text.replace("<b>", "").replace("</b>", "")
                             .replace("<i>", "").replace("</i>", ""))


# WP-311 Ф7 (DP.SC.136): команда /rules — правила игры
# Группировка по семантике event_type (без обращения к compute_effective_amount).
# Маппинг event_type → group синхронизирован с _event_type_to_domain в
# 205-rewards-compute-effective-amount.sql на 17 мая 2026.
_RULE_GROUPS = {
    "📚 Учёба": {
        "lesson_completed", "learning_completed", "training_attempt",
        "training_passed", "test_passed", "assessment_completed",
        "task_submitted", "text_submitted", "table_submitted",
        "feed_completed", "pomodoro_completed", "marathon_step",
        "marathon_task", "marathon_tasks", "workbook_push",
        "strategy_session_completed", "knowledge_extracted",
        "distinction_added", "method_described", "comment_created",
        "topic_created", "club_post_created", "club_topic_created",
        "qualification_granted",
    },
    "🛠 Практика и ритм": {
        "day_open", "day_close", "day_plan_opened", "day_plan_closed",
        "week_plan_created", "week_plan_closed", "month_plan_closed",
        "slot_logged", "pack_updated", "iwe_session", "ai_chat",
        "ai_interaction", "note_to_capture",
    },
    "💼 Работа (по repo)": {
        "wp_created", "wp_closed", "wp_completed", "commit_created",
        "git_commit", "fmt_commit_merged", "coding_time", "content_published",
    },
    "💳 Прочее": {
        "payment_received",
    },
}


def _group_for_rule(event_type: str) -> str:
    for group, types in _RULE_GROUPS.items():
        if event_type in types:
            return group
    return "📦 Другое"


@points_router.message(Command("rules"))
async def cmd_rules(message: Message):
    """Правила игры: за что даются баллы (WP-311 Ф7, DP.SC.136 /rules)."""
    chat_id = message.chat.id
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') if intern else 'ru'

    if not await is_onboarded(intern):
        await message.answer(t('profile.first_start', lang))
        return

    try:
        rules = await get_active_reward_rules()
    except Exception as e:
        logger.error(f"[/rules] chat_id={chat_id}: {e}")
        await message.answer(t('errors.processing_error', lang))
        return

    if not rules:
        await message.answer(
            "🎯 <b>Правила начисления баллов</b>\n\n"
            "<i>Не удалось получить правила. Попробуй позже.</i>",
            parse_mode="HTML",
        )
        return

    # Группируем
    groups: dict[str, list] = {g: [] for g in _RULE_GROUPS}
    groups["📦 Другое"] = []
    for r in rules:
        groups[_group_for_rule(r["trigger_event"])].append(r)

    text = "🎯 <b>Правила начисления баллов</b>\n\n"
    text += (
        "Каждое действие даёт <b>базу</b> баллов. Финальный балл = "
        "<b>база × домен × квалификация × серия</b>, с потолком дня по квалификации.\n\n"
    )

    label_map = _EVENT_LABELS

    for group, group_rules in groups.items():
        if not group_rules:
            continue
        text += f"<b>{group}</b>\n"
        for r in group_rules:
            label = label_map.get(r["trigger_event"], f"• {r['trigger_event']}")
            base = f"{float(r['amount']):g}"
            streak_mark = " 🔥" if r["streak_eligible"] else ""
            text += f"   {label} — <b>{base}</b>{streak_mark}\n"
        text += "\n"

    text += (
        "<i>🔥 — действие наращивает серию (streak): закрытые подряд дни увеличивают множитель "
        "до 1.5× за неделю.</i>\n"
        "<i>Множитель домена: учёба ×?, практика ×?, работа ×? (зависит от настроек платформы).</i>\n"
        "<i>Множитель квалификации: от ×1.0 (Ученик) до ×5.0 (Общественный деятель).</i>\n"
        "<i>Потолок дня — лимит баллов в сутки по квалификации (не теряются — просто не сверх него).</i>\n\n"
        "Свой баланс и историю — /points"
    )

    try:
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"[/rules] HTML render failed for chat_id={chat_id}: {e}")
        await message.answer(text.replace("<b>", "").replace("</b>", "")
                             .replace("<i>", "").replace("</i>", ""))
