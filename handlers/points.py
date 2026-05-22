"""
/points — баланс бонусов + последние начисления с разложением.

WP-306 (WP-121 Ф3): read-only surface над `rewards.point_balances` + `applied_events`.
WP-327 Ф5b: терминология «Бонусы» (burnable currency, DP.D.050) — то, что
отображается здесь, может быть списано при оплате. Earned-total «Баллы» (gamification
score, монотонный рост) — отдельная величина, появится в Phase 2 refactor.

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
    get_domain_multipliers,
)
from i18n import t

logger = logging.getLogger(__name__)

points_router = Router(name="points")


_EVENT_LABELS = {
    # Учёба
    "lesson_completed": "📖 Урок завершён",
    "learning_completed": "🎓 Курс завершён",
    "training_passed": "✅ Тренировка пройдена",
    "training_attempt": "🔄 Тренировка (попытка)",
    "test_passed": "🧪 Тест пройден",
    "assessment_completed": "📋 Аттестация",
    "task_submitted": "📝 Задание отправлено",
    "text_submitted": "✍️ Текст отправлен",
    "table_submitted": "📊 Таблица отправлена",
    "feed_completed": "📰 Дайджест прочитан",
    "marathon_step": "🏃 Шаг марафона",
    "marathon_task": "🏃 Задание марафона",
    "marathon_tasks": "🏃 Задания марафона",
    "workbook_push": "📓 Рабочая тетрадь",
    "pomodoro_completed": "🍅 Помодоро",
    "qualification_granted": "🏆 Квалификация присвоена",
    "strategy_session_completed": "🎯 Стратегическая сессия",
    "knowledge_extracted": "💡 Извлечение знания",
    "distinction_added": "🔍 Различение",
    "method_described": "📚 Метод описан",
    "topic_created": "💬 Тема в клубе",
    "comment_created": "💬 Комментарий",
    # Практика и ритм
    "day_open": "🌅 День открыт",
    "day_close": "🌆 День закрыт",
    "day_plan_opened": "🌅 День открыт",
    "day_plan_closed": "🌆 День закрыт",
    "week_plan_created": "📅 Неделя открыта",
    "week_plan_closed": "📅 Неделя закрыта",
    "month_plan_closed": "📆 Месяц закрыт",
    "slot_logged": "⏱ Слот саморазвития",
    "pack_updated": "📦 Pack обновлён",
    "iwe_session": "💻 Сессия в IWE",
    "ai_chat": "🤖 Чат с ИИ",
    "ai_interaction": "🤖 Взаимодействие с ИИ",
    "note_to_capture": "📝 Заметка",
    # Работа
    "wp_created": "📋 РП создан",
    "wp_closed": "✅ РП закрыт",
    "wp_completed": "✅ РП завершён",
    "git_commit": "⚙️ Коммит в git",
    "commit_created": "⚙️ Коммит (старое имя)",
    "fmt_commit_merged": "🏗 FMT-merge",
    "coding_time": "⏱ Время разработки",
    "content_published": "📰 Контент опубликован",
    # Прочее
    "payment_received": "💳 Оплата получена",
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
    Если cap_truncated — показываем raw (без cap) явно, чтобы пилот понимал,
    что не «0 за ничто», а «потолок дня уже исчерпан».
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

        # raw = что бы начислили без cap; round к 1 знаку для UX
        raw = round(base * dom * qual * streak, 1)

        when_str = f" <i>· {when}</i>" if when else ""
        breakdown = f"{base:g} × {dom:g} × {qual:g} × {streak:g}"

        if capped:
            if eff > 0:
                header = f"<b>+{eff:g}</b> <i>(могло быть +{raw:g} — потолок дня)</i>"
            else:
                header = f"<b>+0</b> <i>(могло быть +{raw:g} — потолок дня уже исчерпан другими действиями)</i>"
        else:
            header = f"<b>+{eff:g}</b>"

        return f"{header} · {label}{when_str}\n   <i>{breakdown}</i>"
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
            "🏆 <b>Бонусы</b>\n\n"
            "Аккаунт ещё не привязан к Aisystant.\n"
            "Привяжите профиль в /settings, чтобы начать копить бонусы.",
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
    text = f"🏆 <b>Бонусы:</b> {balance_text}{today_text}\n\n"

    if not events:
        # WP-311 Ф7 (DP.SC.136 critère «Honesty»): объясняем причину 0 бонусов
        text += (
            "<i>Пока нет начислений.</i>\n\n"
            "Возможные причины:\n"
            "• Не оформлено согласие на учёт активности — пройди /consent\n"
            "• Нет действий за период — закрывай день (/day_close), "
            "делай уроки, фиксируй слоты саморазвития, коммить в свои репозитории\n\n"
            "Полный список действий, дающих бонусы: /rules"
        )
    else:
        text += "<b>Последние начисления:</b>\n\n"
        text += "\n\n".join(_format_event(ev) for ev in events)
        text += (
            "\n\n<i>Разложение: база × домен × квалификация × серия.</i>\n"
            "<i>«Потолок дня» — суточный лимит, наименьший из лимита домена и лимита твоей "
            "ступени/квалификации. Например, у Ученика на ступени 1 потолок = 50 бонусов/день. "
            "Что выше потолка — теряется до следующего дня.</i>\n"
            "<i>Подробнее о потолках и множителях — /rules</i>"
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
    """Правила игры: за что даются бонусы (WP-311 Ф7, DP.SC.136 /rules; WP-327 Ф5b)."""
    chat_id = message.chat.id
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') if intern else 'ru'

    if not await is_onboarded(intern):
        await message.answer(t('profile.first_start', lang))
        return

    try:
        rules = await get_active_reward_rules()
        multipliers = await get_domain_multipliers()
    except Exception as e:
        logger.error(f"[/rules] chat_id={chat_id}: {e}")
        await message.answer(t('errors.processing_error', lang))
        return

    if not rules:
        await message.answer(
            "🎯 <b>Правила начисления бонусов</b>\n\n"
            "<i>Не удалось получить правила. Попробуй позже.</i>",
            parse_mode="HTML",
        )
        return

    # Скрываем legacy alias если новое имя уже представлено:
    # commit_created → скрываем при наличии git_commit
    # day_open / day_close → скрываем при наличии day_plan_opened / day_plan_closed
    trigger_set = {r["trigger_event"] for r in rules}
    legacy_to_hide = set()
    if "git_commit" in trigger_set:
        legacy_to_hide.add("commit_created")
    if "day_plan_opened" in trigger_set:
        legacy_to_hide.add("day_open")
    if "day_plan_closed" in trigger_set:
        legacy_to_hide.add("day_close")
    rules = [r for r in rules if r["trigger_event"] not in legacy_to_hide]

    # Группируем
    groups: dict[str, list] = {g: [] for g in _RULE_GROUPS}
    groups["📦 Другое"] = []
    for r in rules:
        groups[_group_for_rule(r["trigger_event"])].append(r)

    text = "🎯 <b>Правила начисления бонусов</b>\n\n"
    text += (
        "Каждое действие даёт <b>базу</b> бонусов. Финальный бонус = "
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

    # Реальные множители из reference.activity_domain_multipliers
    def _fmt_mult(domain_key: str) -> str:
        m = multipliers.get(domain_key, {})
        if not m:
            return "×?"
        return f"×{m['multiplier']:g} (потолок дня {m['daily_cap_default']:g})"

    text += (
        "<b>🔥 — действие наращивает серию (streak)</b>\n"
        "Закрытые подряд дни увеличивают множитель до 1.5× за неделю.\n\n"
        "<b>Множители домена</b>\n"
        f"   📚 Учёба: {_fmt_mult('learning')}\n"
        f"   🛠 Практика: {_fmt_mult('practice')}\n"
        f"   💼 Работа: {_fmt_mult('work')}\n\n"
        "<b>Множители ступени (Ученик)</b> — определяют твой персональный суточный потолок бонусов\n"
        "   1 Случайный — ×1.0, потолок 50/день\n"
        "   2 Практикующий — ×1.2, потолок 80/день\n"
        "   3 Систематический — ×1.5, потолок 120/день\n"
        "   4 Дисциплинированный — ×2.0, потолок 200/день\n"
        "   5 Проактивный — ×2.5, потолок 300/день\n\n"
        "<b>Множители квалификаций МИМ</b> (Работник и выше) — от ×1.3 до ×5.0, потолок 140–1000/день.\n\n"
        "<b>Как считается «потолок дня»</b>\n"
        "Берётся <b>наименьший</b> из двух: потолок домена (учёба/практика/работа) и потолок твоей ступени/квалификации. "
        "Например: ты Ученик-Систематический (потолок 120) делаешь коммит в work-репо (потолок 50) → потолок этого начисления = 50.\n"
        "Что не вошло — теряется до следующего дня.\n\n"
        "Свой бонусный баланс и историю — /points"
    )

    try:
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"[/rules] HTML render failed for chat_id={chat_id}: {e}")
        await message.answer(text.replace("<b>", "").replace("</b>", "")
                             .replace("<i>", "").replace("</i>", ""))
