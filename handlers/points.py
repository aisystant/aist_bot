"""
/points — баланс баллов + бонусов + последние начисления.

Баллы (earned_total) — монотонный счёт саморазвития, никогда не убывают.
Бонусы (point_balances.points) — доступная скидка при оплате (убывают после burn).
WP-327 Phase 2: разделение двух счётчиков, прогресс-бар, inline-кнопки.

Источник истины: Neon БД `rewards` (writer — DP.ROLE.034, DP.SC.122).
Бот не пересчитывает — показывает то, что свернул projection-worker.
"""

import logging
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.filters import Command

from db.queries import get_intern
from db.queries.users import is_onboarded
from db.queries.rewards import (
    get_points_balance,
    get_recent_applied_events,
    get_today_total,
    get_today_raw_total,
    get_active_reward_rules,
    get_domain_multipliers,
    get_earned_total,
    get_user_daily_cap,
    get_student_stage_multipliers,
    get_qualification_multipliers_list,
)
from i18n import t

logger = logging.getLogger(__name__)

points_router = Router(name="points")


_EVENT_LABELS = {
    # Учёба
    "lesson_completed": "Урок завершён",
    "learning_completed": "Курс завершён",
    "training_passed": "Тренировка пройдена",
    "training_attempt": "Тренировка (попытка)",
    "test_passed": "Тест пройден",
    "assessment_completed": "Аттестация",
    "task_submitted": "Задание отправлено",
    "text_submitted": "Текст отправлен",
    "table_submitted": "Таблица отправлена",
    "feed_completed": "Дайджест прочитан",
    "marathon_step": "Шаг марафона",
    "marathon_task": "Задание марафона",
    "marathon_tasks": "Задания марафона",
    "workbook_push": "Рабочая тетрадь",
    "pomodoro_completed": "Помодоро",
    "qualification_granted": "Квалификация присвоена",
    "strategy_session_completed": "Стратегическая сессия",
    "knowledge_extracted": "Извлечение знания",
    "distinction_added": "Различение",
    "method_described": "Метод описан",
    "topic_created": "Тема в клубе",
    "comment_created": "Комментарий",
    # Практика и ритм
    "day_open": "День открыт",
    "day_close": "День закрыт",
    "day_plan_opened": "День открыт",
    "day_plan_closed": "День закрыт",
    "week_plan_created": "Неделя открыта",
    "week_plan_closed": "Неделя закрыта",
    "month_plan_closed": "Месяц закрыт",
    "slot_logged": "Слот саморазвития",
    "pack_updated": "Pack обновлён",
    "iwe_session": "Сессия в IWE",
    "ai_chat": "Чат с ИИ",
    "ai_interaction": "Взаимодействие с ИИ",
    "note_to_capture": "Заметка",
    # Работа
    "wp_created": "РП создан",
    "wp_closed": "РП закрыт",
    "wp_completed": "РП завершён",
    "git_commit": "Коммит в git",
    "commit_created": "Коммит (старое имя)",
    "fmt_commit_merged": "FMT-merge",
    "coding_time": "Время разработки",
    "content_published": "Контент опубликован",
    # Прочее
    "payment_received": "Оплата получена",
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



def _progress_bar(current: float, cap: float, width: int = 10) -> str:
    if cap <= 0:
        return "░" * width
    filled = round(min(current / cap, 1.0) * width)
    return "▓" * filled + "░" * (width - filled)


def _fmt_pts(n: float) -> str:
    """Форматирование числа баллов: пробел как разделитель тысяч."""
    return f"{int(n):,}".replace(",", " ")


def _format_event_compact(ev: dict) -> str:
    """Компактная строка начисления баллов. Баллы = raw, всегда > 0."""
    try:
        label = _event_label(ev["event_type"])
        base = float(ev.get("base_amount") or 0)
        dom  = float(ev.get("dom_mult")    or 1)
        qual = float(ev.get("qual_mult")   or 1)
        stk  = float(ev.get("streak_mult") or 1)
        when = _relative_time(ev.get("applied_at"))
        when_str = f" · <i>{when}</i>" if when else ""
        raw = round(base * dom * qual * stk, 1)
        return f"• {label}: <b>+{raw:g}</b>{when_str}"
    except Exception as e:
        logger.warning(f"[/points] _format_event_compact failed: {e}")
        return f"• {ev.get('event_type', '?')}"


def _build_points_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Правила начисления", callback_data="points_show_rules")],
    ])


@points_router.message(Command("points"))
async def cmd_points(message: Message):
    """Баланс баллов/бонусов + последние 5 начислений. WP-327 Phase 2."""
    chat_id = message.chat.id
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') if intern else 'ru'

    if not await is_onboarded(intern):
        await message.answer(t('profile.first_start', lang))
        return

    account_id = intern.get('dt_user_id')
    if not account_id:
        await message.answer(
            "🏆 <b>Ваши начисления</b>\n\n"
            "Аккаунт ещё не привязан к Aisystant.\n"
            "Привяжи профиль в /settings, чтобы начать копить баллы.",
            parse_mode="HTML",
        )
        return

    try:
        balance = await get_points_balance(account_id)
        earned_total = await get_earned_total(account_id)
        events = await get_recent_applied_events(account_id, limit=5)
        today_raw = await get_today_raw_total(account_id)   # баллы (raw, без cap)
        today_bonus = await get_today_total(account_id)     # бонусы (effective, capped)
        daily_cap = await get_user_daily_cap(account_id)
    except Exception as e:
        logger.error(f"[/points] chat_id={chat_id}: {e}")
        await message.answer(t('errors.processing_error', lang))
        return

    balance_num = float(balance or 0)
    earned_num = float(earned_total) if earned_total is not None else balance_num
    today_raw_num = float(today_raw or 0)
    today_bonus_num = float(today_bonus or 0)

    # Баллы сегодня — raw, без потолка
    today_pts_str = f"+{_fmt_pts(int(today_raw_num))}" if today_raw_num > 0 else "—"

    # Бонусы сегодня — effective с прогресс-баром vs дневной потолок
    today_bonus_str = f"+{_fmt_pts(int(today_bonus_num))}" if today_bonus_num > 0 else "—"
    if today_bonus_num > 0 and daily_cap and daily_cap > 0:
        bar = _progress_bar(today_bonus_num, float(daily_cap))
        pct = int(min(today_bonus_num / daily_cap, 1.0) * 100)
        today_bonus_line = f"{today_bonus_str} {bar} {int(today_bonus_num)}/{daily_cap} ({pct}%)"
    else:
        today_bonus_line = today_bonus_str

    text = "🏆 <b>Ваши начисления</b>\n\n"
    text += f"Всего баллов: <b>{_fmt_pts(int(earned_num))}</b>\n"
    text += f"Начислено за сегодня: {today_pts_str}\n"
    text += "\n"
    text += f"Всего бонусов: <b>{_fmt_pts(int(balance_num))}</b>\n"
    text += f"Начислено за сегодня: {today_bonus_line}\n"
    text += "\n"
    text += (
        "Бонусы можно использовать при оплате. "
        "Чтобы увеличить бонусы — повышай квалификацию, работай и развивайся систематично.\n"
    )

    if not events:
        text += (
            "\n<i>Пока нет начислений. "
            "Закрывай день /day_close, делай уроки, фиксируй слоты саморазвития.</i>"
        )
    else:
        text += "\n<b>Последние начисления баллов:</b>\n"
        for ev in events:
            text += _format_event_compact(ev) + "\n"

    kb = _build_points_keyboard()
    try:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.error(f"[/points] HTML render failed for chat_id={chat_id}: {e}")
        await message.answer(
            text.replace("<b>", "").replace("</b>", "")
                .replace("<i>", "").replace("</i>", ""),
            reply_markup=kb,
        )


@points_router.callback_query(F.data == "points_show_rules")
async def cb_points_show_rules(callback: CallbackQuery):
    """Inline-кнопка «Правила начисления» → отправляет /rules ответом."""
    await callback.answer()
    await cmd_rules(callback.message)


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
    """Правила начисления баллов и бонусов. WP-327 Phase 2."""
    chat_id = message.chat.id
    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') if intern else 'ru'

    if not await is_onboarded(intern):
        await message.answer(t('profile.first_start', lang))
        return

    try:
        qual_rows = await get_qualification_multipliers_list()
    except Exception as e:
        logger.error(f"[/rules] chat_id={chat_id}: {e}")
        await message.answer(t('errors.processing_error', lang))
        return

    text = (
        "🎯 <b>Правила начисления баллов и бонусов</b>\n\n"
        "<b>Баллы</b> — монотонный счётчик твоего развития. Никогда не убывают. "
        "Показывают, сколько ты вложил в себя за всё время и как помог развитию сообщества.\n\n"
        "<b>Бонусы</b> — доступная скидка при оплате. Ограничены дневной нормой, "
        "которая зависит от квалификации МИМ. Тратятся, когда применяешь их к заказу.\n\n"
        "Каждое активное действие на платформе даёт основу для начисления баллов и бонусов. "
        "Финальный балл = <b>база × домен × квалификация × серия</b>. "
        "Бонус зависит от финального балла, но имеет потолок.\n\n"
        "Баллы начисляются за действия в LMS, в IWE, в клубе, в боте и тп. "
        "Если работаете и развиваетесь непрерывно, то за удержание систематичности (streak) "
        "применяется множитель до ×1.5 за неделю.\n\n"
    )

    if qual_rows:
        text += "<b>Множители за квалификацию МИМ</b>\n"
        for q in qual_rows:
            raw_name = str(q.get('qualification', ''))
            name = raw_name.replace('_', ' ').title()
            mult = float(q.get('multiplier', 1))
            cap = int(q.get('daily_cap', 0))
            text += f"   {name} — ×{mult:g}, потолок бонусов {cap}/день\n"
        text += "\n"
    else:
        text += (
            "<b>Множители за квалификацию МИМ</b>\n"
            "   Ученик — ×1, потолок бонусов 100/день\n"
            "   Работник — ×1.3, потолок бонусов 140/день\n"
            "   Стратег — ×1.6, потолок бонусов 200/день\n"
            "   Специалист — ×2, потолок бонусов 280/день\n"
            "   Практик — ×2.5, потолок бонусов 360/день\n"
            "   Мастер — ×3, потолок бонусов 500/день\n"
            "   Реформатор — ×4, потолок бонусов 700/день\n"
            "   Общественный Деятель — ×5, потолок бонусов 1000/день\n\n"
        )

    text += (
        "<b>Как считается суточный потолок бонусов</b>\n"
        "Берётся наименьший из двух: баллы и потолок квалификации. "
        "Например: Ученик (потолок 100) получает 150 баллов → засчитывается не более 100 бонусов. "
        "Что не вошло в бонусы — остаётся в баллах.\n\n"
        "Свой баланс и историю — /points"
    )

    try:
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"[/rules] HTML render failed for chat_id={chat_id}: {e}")
        await message.answer(
            text.replace("<b>", "").replace("</b>", "")
                .replace("<i>", "").replace("</i>", "")
        )
