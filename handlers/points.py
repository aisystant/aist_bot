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
from decimal import Decimal

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
    get_recent_redeemed_events,
    get_today_total,
    get_domain_multipliers,
    get_earned_total,
    get_user_daily_cap,
    get_student_stage_multipliers,
    get_qualification_multipliers_list,
    get_qualification_levels_v4_list,
    get_loyalty_rate,
    get_today_typing_stats,
)
from i18n import t
from core.tier_detector import detect_ui_tier
from core.tier_config import UITier
from config import PLATFORM_URLS

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
    # Набор текста
    "user_typing_tracked": "Набор текста",
    # Клуб (Discourse, WP-327)
    "club_post_created": "Пост в клубе",
    "club_topic_created": "Тема создана в клубе",
    "club_like_created": "Лайк поставлен в клубе",
    "club_like_received": "Лайк получен в клубе",
    "club_comment_received": "Ответ получен в клубе",
    "club_trust_promoted": "Уровень доверия повышен",
    "club_badge_granted": "Значок получен",
    "club_user_created": "Аккаунт создан в клубе",
    "club_email_confirmed": "Email подтверждён в клубе",
    "club_solution_accepted": "Решение принято в клубе",
    # Подписка
    "subscription_first_purchased": "Первая подписка",
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


_PURPOSE_LABELS = {
    "SEMINAR": "Семинар",
    "SUBSCRIPTION": "Подписка",
    "EVENT": "Мероприятие",
}


def _purpose_label(purpose: str) -> str:
    return _PURPOSE_LABELS.get(purpose, purpose or "Оплата")


def _format_redeemed_compact(rev: dict) -> str:
    """Компактная строка списания. WP-188 Ф17 #4."""
    try:
        label = _purpose_label(rev.get("purpose"))
        pts = int(float(rev.get("points_amount") or 0))
        rub = int(float(rev.get("discount_rub") or 0))
        when = _relative_time(rev.get("confirmed_at") or rev.get("reserved_at"))
        when_str = f" · <i>{when}</i>" if when else ""
        return f"• {label}: <b>−{pts}</b> ({rub}₽){when_str}"
    except Exception as e:
        logger.warning(f"[/points] _format_redeemed_compact failed: {e}")
        return "• списание"


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


_SPEND_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🎟 Семинары и мероприятия", callback_data="showcase_main")],
    [InlineKeyboardButton(text="💎 Подписка «Инженерия интеллекта»", callback_data="subscribe")],
])


def _build_points_keyboard(spend_pts=None) -> InlineKeyboardMarkup:
    rows = []
    if spend_pts and spend_pts > 0:
        rows.append([InlineKeyboardButton(
            text=f"💳 Потратить {_fmt_pts(spend_pts)} бонусов",
            callback_data="points_spend",
        )])
    rows.append([InlineKeyboardButton(text="📋 Правила начисления", callback_data="points_show_rules")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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

    has_subscription = False
    try:
        tier = await detect_ui_tier(chat_id)
        has_subscription = tier >= UITier.T2_LEARNING
    except Exception:
        pass  # fail-open: показываем вид без подписки при ошибке

    try:
        balance = await get_points_balance(account_id)
        events = await get_recent_applied_events(account_id, limit=5)
        today_bonus = await get_today_total(account_id)     # effective, capped
        daily_cap = await get_user_daily_cap(account_id)
        rate = await get_loyalty_rate()
    except Exception as e:
        logger.error(f"[/points] chat_id={chat_id}: {e}")
        await message.answer(t('errors.processing_error', lang))
        return

    typing_stats = None
    try:
        typing_stats = await get_today_typing_stats(account_id)
    except Exception as e:
        logger.warning(f"[/points] typing_stats failed chat_id={chat_id}: {e}")

    earned_total = None
    try:
        earned_total = await get_earned_total(account_id)
    except Exception as e:
        _e = str(e).lower()
        if "does not exist" in _e or "undefined" in _e:
            logger.warning(f"[/points] redeemed_events schema missing chat_id={chat_id}: {e}")
        else:
            logger.error(f"[/points] earned_total query failed chat_id={chat_id}: {e}", exc_info=True)

    redeemed = []
    try:
        redeemed = await get_recent_redeemed_events(account_id, limit=3)
    except Exception as e:
        _e = str(e).lower()
        if "does not exist" in _e or "undefined" in _e:
            logger.warning(f"[/points] redeemed_events schema missing chat_id={chat_id}: {e}")
        else:
            logger.error(f"[/points] redeemed_events query failed chat_id={chat_id}: {e}", exc_info=True)

    balance_num = float(balance or 0)
    earned_num = float(earned_total) if earned_total is not None else balance_num
    today_bonus_num = float(today_bonus or 0)

    # Баллы сегодня — effective (как и бонусы, без raw-накрутки)
    today_pts_str = f"+{_fmt_pts(int(today_bonus_num))}" if today_bonus_num > 0 else "—"

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
    if has_subscription:
        text += f"Всего бонусов: <b>{_fmt_pts(int(balance_num))}</b>\n"
        text += f"Начислено за сегодня: {today_bonus_line}\n"
        if balance_num > 0:
            discount_rub = int(balance_num * float(rate))
            text += f"Доступная скидка: <b>~{_fmt_pts(discount_rub)} ₽</b>\n"
        text += f"\nКурс: <b>{float(rate):.2f} ₽/бонус</b>"
        # WP-327 v4.4: hint про целевой курс 0.20 пока курс ниже него
        if 0 < float(rate) < 0.20:
            text += " (целевой — 0.20, бонусы дорожают)"
        text += ".\n"
        if balance_num == 0:
            text += "Развивайся систематично — бонусы растут с квалификацией.\n"
    else:
        sub_url = PLATFORM_URLS.get("subscription", "https://system-school.ru/open-endedness")
        text += "💎 <b>Бонусы:</b> доступны по подписке «Инженерия интеллекта»\n"
        text += f'➡️ <a href="{sub_url}">Оформить подписку</a>\n'

    # Typing stats section (WP-327 Phase 3) — show always when connected, including 0
    if typing_stats is not None:
        t_chars = int(typing_stats.get("total_chars") or 0)
        t_pts = float(typing_stats.get("points_earned") or 0)
        TYPING_DAILY_CAP = 50
        t_bar = _progress_bar(t_pts, TYPING_DAILY_CAP)
        text += (
            f"\n🖊 <b>Набрано сегодня:</b> {_fmt_pts(t_chars)} симв. "
            f"→ <b>+{int(t_pts)}</b> балл. "
            f"{t_bar} {int(t_pts)}/{TYPING_DAILY_CAP}\n"
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

    if redeemed:
        text += "\n<b>Последние списания бонусов:</b>\n"
        for rev in redeemed:
            text += _format_redeemed_compact(rev) + "\n"

    spend_pts = int(balance_num) if (has_subscription and balance_num > 0) else None
    kb = _build_points_keyboard(spend_pts=spend_pts)
    try:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.error(f"[/points] HTML render failed for chat_id={chat_id}: {e}")
        await message.answer(
            text.replace("<b>", "").replace("</b>", "")
                .replace("<i>", "").replace("</i>", ""),
            reply_markup=kb,
        )


@points_router.callback_query(F.data == "points_spend")
async def cb_points_spend(callback: CallbackQuery):
    """Show spend menu: choose product to pay with bonuses."""
    await callback.answer()
    await callback.message.answer(
        "💳 <b>Потратить бонусы</b>\n\n"
        "Выбери, к чему применить скидку:\n\n"
        "🎟 <b>Семинары</b> — скидка рассчитается автоматически по квалификации.\n"
        "💎 <b>Подписка</b> — оформить или продлить «Инженерия интеллекта».",
        parse_mode="HTML",
        reply_markup=_SPEND_KEYBOARD,
    )


@points_router.callback_query(F.data == "points_show_rules")
async def cb_points_show_rules(callback: CallbackQuery):
    """Inline-кнопка «Правила начисления» → отправляет /rules ответом."""
    await callback.answer()
    await cmd_rules(callback.message)


@points_router.callback_query(F.data == "points_how_to_spend")
async def cb_points_how_to_spend(callback: CallbackQuery):
    """Inline-кнопка «Где потратить бонусы?»."""
    await callback.answer()
    text = (
        "💡 <b>Как потратить бонусы</b>\n\n"
        "Бонусы применяются при оплате:\n"
        "• <b>Семинара</b> — выбери семинар, нажми «Оплатить»; "
        "если в копилке есть бонусы, система предложит скидку автоматически\n"
        "• <b>Подписки «Инженерия интеллекта»</b> — аналогично при продлении\n\n"
        "<i>Скидка рассчитывается по курсу в момент оплаты. "
        "Чем выше квалификация — тем больше суточный потолок бонусов.</i>"
    )
    await callback.message.answer(text, parse_mode="HTML")


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
        "topic_created", "qualification_granted",
        "club_post_created", "club_topic_created", "club_like_created",
        "club_like_received", "club_comment_received", "club_trust_promoted",
        "club_badge_granted", "club_user_created", "club_email_confirmed",
        "club_solution_accepted",
    },
    "🛠 Практика и ритм": {
        "day_open", "day_close", "day_plan_opened", "day_plan_closed",
        "week_plan_created", "week_plan_closed", "month_plan_closed",
        "slot_logged", "pack_updated", "iwe_session", "ai_chat",
        "ai_interaction", "note_to_capture", "user_typing_tracked",
    },
    "💼 Работа (по repo)": {
        "wp_created", "wp_closed", "wp_completed", "commit_created",
        "git_commit", "fmt_commit_merged", "coding_time", "content_published",
    },
    "💳 Прочее": {
        "payment_received",
        "subscription_first_purchased",
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

    try:
        qual_rows = await get_qualification_levels_v4_list()
        rate = await get_loyalty_rate()
    except Exception as e:
        logger.error(f"[/rules] chat_id={chat_id}: {e}")
        qual_rows = []
        rate = Decimal("0.10")  # WP-327 v4.4: новый стартовый курс

    cap_lines = []
    for q in qual_rows:
        cap_lines.append(f"   {q['name']} — {int(q['daily_cap_bonuses'])}\n")

    text = (
        "🎯 <b>Правила начисления баллов и бонусов</b>\n\n"
        "<b>Баллы</b> — монотонный счётчик развития. "
        "Складываются за всё время, никогда не обнуляются.\n"
        "<b>Бонусы</b> — скидка при оплате. "
        f"Курс: <b>{float(rate):.2f} ₽/бонус</b>.\n\n"
        "<b>Как начисляются баллы</b>\n"
        "-- Баллы = база × квалификация × серия\n"
        "-- Содержательные действия (уроки, задания, публикации): "
        "База = время^0.6 × группа × редкость\n"
        "-- Маркерные действия (коммит, открытие дня, чат с ИИ, сессия в IWE): "
        "База = группа × редкость\n"
        "-- Группа: G1 \"Личное\" — ×1, "
        "G2 \"Продукт\" — ×2, "
        "G3 \"Публичное знание\" — ×3, "
        "G4 \"Сообщество\" — ×4\n\n"
        "<b>Серия дней (страйк)</b>\n"
        "-- 0–6 дней — ×1.0\n"
        "-- 7–13 дней — ×1.3\n"
        "-- 14–27 дней — ×1.7\n"
        "-- 28+ дней — ×2.2\n"
        "Пропуск 1 дня — мягкий сброс (-1 уровень). 2 пропуска подряд — сброс до ×1.0.\n\n"
        "<b>Как баллы переходят в бонусы</b>\n"
        "-- Бонусы каждого дня = баллы за день (срез по суточному лимиту уже применён)\n"
        "-- Суточный потолок бонусов зависит от квалификации:\n"
        + "".join(cap_lines)
        + "\nСвой баланс — /points"
    )

    try:
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"[/rules] HTML render failed for chat_id={chat_id}: {e}")
        await message.answer(
            text.replace("<b>", "").replace("</b>", "")
                .replace("<i>", "").replace("</i>", "")
        )
