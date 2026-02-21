"""
Команды разработчика: /stats, /usage, /qa, /health, /latency, /errors, autofix callbacks.

Доступны только для DEVELOPER_CHAT_ID.
"""

import logging
import os

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command

from helpers.message_split import truncate_safe

logger = logging.getLogger(__name__)

dev_router = Router(name="dev")


def _is_developer(chat_id: int) -> bool:
    dev = os.getenv("DEVELOPER_CHAT_ID")
    return bool(dev and str(chat_id) == dev)


@dev_router.message(Command("stats"))
async def cmd_stats(message: Message):
    """/stats — статистика пользователей и активности."""
    if not _is_developer(message.chat.id):
        return

    from db.queries.dev_stats import (
        get_user_stats, get_language_distribution,
        get_complexity_distribution, get_integration_stats,
    )

    try:
        s = await get_user_stats()
        langs = await get_language_distribution()
        complexity = await get_complexity_distribution()
        integrations = await get_integration_stats()
    except Exception as e:
        logger.error(f"[Dev] /stats error: {e}")
        await message.answer("Ошибка загрузки статистики.")
        return

    sep = "\u2500" * 20

    lang_str = " | ".join(f"{r['lang']}: {r['cnt']}" for r in langs)
    complexity_str = " | ".join(f"L{r['lvl']}: {r['cnt']}" for r in complexity)

    text = (
        f"<b>Статистика пользователей</b>\n{sep}\n\n"
        f"<b>Пользователи</b>\n"
        f"  Всего: {s.get('total', 0)} | Онбординг пройден: {s.get('onboarded', 0)}\n"
        f"  Активны сегодня: {s.get('active_today', 0)} | За неделю: {s.get('active_week', 0)}\n\n"
        f"<b>Режимы</b>\n"
        f"  \U0001f4da Марафон: {s.get('marathon_active', 0)} актив."
        f" | {s.get('marathon_completed', 0)} завершено"
        f" | {s.get('marathon_paused', 0)} пауза\n"
        f"  \U0001f4d6 Лента: {s.get('feed_active', 0)} актив.\n"
        f"  \U0001f504 Оба: {s.get('both_active', 0)}\n\n"
        f"<b>Вовлечённость</b>\n"
        f"  Ср. активных дней: {s.get('avg_active_days', 0)}\n"
        f"  Ср. серия: {s.get('avg_streak', 0)} | Макс: {s.get('max_streak', 0)}\n"
        f"  Ср. сложность: {s.get('avg_complexity', 0)}\n\n"
        f"<b>Сложность</b>: {complexity_str}\n"
        f"<b>Языки</b>: {lang_str}\n\n"
        f"<b>Интеграции</b>\n"
        f"  \U0001f4bb GitHub: {integrations.get('github_connected', 0)}\n"
        f"  \U0001f9ea Тестирование: {integrations.get('assessed_users', 0)}"
        f" ({integrations.get('total_assessments', 0)} тестов)\n"
    )

    await message.answer(text, parse_mode="HTML")


@dev_router.message(Command("usage"))
async def cmd_usage(message: Message):
    """/usage — популярность сервисов."""
    if not _is_developer(message.chat.id):
        return

    from db.queries.dev_stats import get_global_service_usage, get_schedule_distribution

    try:
        services = await get_global_service_usage()
        schedule = await get_schedule_distribution()
    except Exception as e:
        logger.error(f"[Dev] /usage error: {e}")
        await message.answer("Ошибка загрузки использования.")
        return

    sep = "\u2500" * 20

    svc_lines = ""
    for r in services:
        svc_lines += f"  {r['service_id']}: {r['cnt']} ({r['users']} польз.)\n"

    sched_lines = ""
    for r in schedule:
        sched_lines += f"  {r['hour']}: {r['cnt']} польз.\n"

    text = (
        f"<b>Использование сервисов</b>\n{sep}\n\n"
        f"<b>Топ сервисов</b> (всего нажатий | уник. пользователей):\n"
        f"{svc_lines}\n"
        f"<b>Расписание (распределение)</b>:\n"
        f"{sched_lines}"
    )

    text = truncate_safe(text)

    await message.answer(text, parse_mode="HTML")


@dev_router.message(Command("qa"))
async def cmd_qa(message: Message):
    """/qa — статистика консультаций."""
    if not _is_developer(message.chat.id):
        return

    from db.queries.dev_stats import get_qa_stats, get_qa_top_topics

    try:
        s = await get_qa_stats()
        topics = await get_qa_top_topics(8)
    except Exception as e:
        logger.error(f"[Dev] /qa error: {e}")
        await message.answer("Ошибка загрузки статистики консультаций.")
        return

    sep = "\u2500" * 20

    total = s.get('total', 0)
    helpful = s.get('helpful', 0)
    not_helpful = s.get('not_helpful', 0)
    rated = helpful + not_helpful
    rate = f"{helpful / rated * 100:.0f}%" if rated > 0 else "\u2014"

    topics_str = ""
    for r in topics:
        topics_str += f"  {r['topic']}: {r['cnt']}\n"

    text = (
        f"<b>Аналитика консультаций</b>\n{sep}\n\n"
        f"<b>Объём</b>\n"
        f"  Всего: {total} | Сегодня: {s.get('today', 0)} | За неделю: {s.get('this_week', 0)}\n"
        f"  Уник. пользователей: {s.get('unique_users', 0)}\n\n"
        f"<b>Качество</b>\n"
        f"  \U0001f44d {helpful} | \U0001f44e {not_helpful} | Рейтинг: {rate}\n"
        f"  Без оценки: {s.get('no_feedback', 0)} | С комментарием: {s.get('with_comments', 0)}\n\n"
        f"<b>Популярные темы</b>:\n"
        f"{topics_str}"
    )

    await message.answer(text, parse_mode="HTML")


@dev_router.message(Command("health"))
async def cmd_health(message: Message):
    """/health — техническое состояние."""
    if not _is_developer(message.chat.id):
        return

    from db.queries.dev_stats import get_table_sizes, get_pending_content_count
    from db.queries.feedback import get_report_stats

    try:
        tables = await get_table_sizes()
        pending = await get_pending_content_count()
        feedback = await get_report_stats()
    except Exception as e:
        logger.error(f"[Dev] /health error: {e}")
        await message.answer("Ошибка загрузки состояния системы.")
        return

    sep = "\u2500" * 20

    table_lines = ""
    for r in tables:
        cnt = r['count'] if r['count'] >= 0 else "ERR"
        table_lines += f"  {r['table']}: {cnt}\n"

    text = (
        f"<b>Состояние системы</b>\n{sep}\n\n"
        f"<b>Размеры таблиц</b>:\n"
        f"{table_lines}\n"
        f"<b>Марафон</b>\n"
        f"  Ожидает контент: {pending}\n\n"
        f"<b>Обратная связь</b>\n"
        f"  \U0001f195 Новые: {feedback.get('new_count', 0)}"
        f" | \U0001f534 Плохо: {feedback.get('red_count', 0)}"
        f" | \U0001f7e1 Средне: {feedback.get('yellow_count', 0)}"
        f" | \U0001f7e2 Хорошо: {feedback.get('green_count', 0)}\n"
    )

    await message.answer(text, parse_mode="HTML")


@dev_router.message(Command("latency"))
async def cmd_latency(message: Message):
    """/latency — отчёт по латентности с пороговыми значениями."""
    if not _is_developer(message.chat.id):
        return

    from db.queries.traces import get_latency_report, classify_command, get_color, THRESHOLDS

    try:
        report = await get_latency_report(hours=24)
    except Exception as e:
        logger.error(f"[Dev] /latency error: {e}")
        await message.answer("Ошибка загрузки отчёта по латентности.")
        return

    sep = "\u2500" * 20
    s = report['summary']

    # Пороги
    legend = (
        "\U0001f7e2 навиг. &lt;1с | тяжёлые &lt;3с | консульт. &lt;8с\n"
        "\U0001f7e1 навиг. &lt;3с | тяжёлые &lt;8с | консульт. &lt;20с\n"
        "\U0001f534 выше жёлтого\n"
    )

    # По командам
    cmd_lines = ""
    for r in report['by_command']:
        cat = classify_command(r['command'])
        color = get_color(r['avg_ms'], cat)
        cmd_lines += f"  {color} {r['command']}: {r['avg_ms']}мс сред. | p95={r['p95_ms']}мс | n={r['count']}\n"

    # Самые медленные операции
    span_lines = ""
    for r in report['slowest_spans'][:6]:
        span_lines += f"  {r['name']}: {r['avg_ms']}мс сред. | макс={r['max_ms']}мс\n"

    # Красная зона
    red_lines = ""
    if report['red_traces']:
        for r in report['red_traces']:
            ms = int(r['total_ms'])
            red_lines += f"  \U0001f534 {r['command']}: {ms}мс\n"
    else:
        red_lines = "  \u2014 нет\n"

    text = (
        f"<b>Отчёт по латентности (24ч)</b>\n{sep}\n\n"
        f"<b>Сводка</b>\n"
        f"  Запросов: {s['total']} | Среднее: {s['avg_ms']}мс | P95: {s['p95_ms']}мс\n"
        f"  \U0001f534 Красная зона: {report['red_count']}\n\n"
        f"<b>Пороги</b>\n{legend}\n"
        f"<b>По командам</b>\n{cmd_lines}\n"
        f"<b>Медленные операции</b>\n{span_lines}\n"
        f"<b>Красная зона</b>\n{red_lines}"
    )

    text = truncate_safe(text)

    await message.answer(text, parse_mode="HTML")


@dev_router.message(Command("errors"))
async def cmd_errors(message: Message):
    """/errors — отчёт по ошибкам за 24h."""
    if not _is_developer(message.chat.id):
        return

    from db.queries.errors import get_error_report

    try:
        report = await get_error_report(hours=24)
    except Exception as e:
        logger.error(f"[Dev] /errors error: {e}")
        await message.answer("Ошибка загрузки отчёта по ошибкам.")
        return

    sep = "\u2500" * 20
    s = report['summary']

    if s['unique_errors'] == 0:
        await message.answer(
            f"<b>Отчёт по ошибкам (24ч)</b>\n{sep}\n\n"
            f"\U0001f7e2 Ошибок за последние 24 часа нет.",
            parse_mode="HTML"
        )
        return

    # По источникам
    logger_lines = ""
    for r in report['by_logger']:
        logger_lines += f"  {r['logger_name']}: {r['count']} уник. ({r['total_occurrences']} всего)\n"

    # Последние ошибки
    recent_lines = ""
    for r in report['recent'][:8]:
        emoji = "\U0001f534" if r['level'] == 'CRITICAL' else "\U0001f7e1"
        msg = (r['message'] or '')[:60]
        count_str = f" x{r['occurrence_count']}" if r['occurrence_count'] > 1 else ""
        recent_lines += f"  {emoji} {r['logger_name']}: {msg}{count_str}\n"

    text = (
        f"<b>Отчёт по ошибкам (24ч)</b>\n{sep}\n\n"
        f"<b>Сводка</b>\n"
        f"  Уник. ошибок: {s['unique_errors']}"
        f" | Всего случаев: {s['total_occurrences']}\n"
        f"  \U0001f534 Критических: {s['critical_count']}\n\n"
        f"<b>По источникам</b>\n{logger_lines}\n"
        f"<b>Последние</b>\n{recent_lines}"
    )

    text = truncate_safe(text)

    await message.answer(text, parse_mode="HTML")


@dev_router.message(Command("analytics"))
async def cmd_analytics(message: Message):
    """/analytics — сводная аналитика IWE (users, sessions, quality, retention, trends)."""
    if not _is_developer(message.chat.id):
        return

    from db.queries.analytics import get_analytics_report

    try:
        report = await get_analytics_report(hours=24)
    except Exception as e:
        logger.error(f"[Dev] /analytics error: {e}")
        await message.answer("Ошибка загрузки аналитики.")
        return

    text = _format_analytics(report)
    await message.answer(text, parse_mode="HTML")


def _format_analytics(report: dict) -> str:
    """Форматирование аналитического отчёта в HTML."""
    sep = "\u2500" * 20
    u = report['users']
    s = report['sessions']
    q = report['quality']
    r = report['retention']
    tr = report['trends']

    # Trends arrows
    dau_arrow = "\u2197\ufe0f" if tr['dau_change_pct'] > 0 else ("\u2198\ufe0f" if tr['dau_change_pct'] < 0 else "\u2794")
    sess_arrow = "\u2197\ufe0f" if tr['sessions_change_pct'] > 0 else ("\u2198\ufe0f" if tr['sessions_change_pct'] < 0 else "\u2794")

    # Entry points
    entry_str = ""
    for ep in s.get('entry_points', [])[:3]:
        entry_str += f"{ep['point']} ({ep['count']}), "
    entry_str = entry_str.rstrip(", ") or "\u2014"

    # Duration formatting
    avg_min = s['avg_duration_sec'] // 60
    avg_sec = s['avg_duration_sec'] % 60

    # Latency color
    lat_emoji = "\U0001f7e2" if q['avg_ms'] < 3000 else ("\U0001f7e1" if q['avg_ms'] < 8000 else "\U0001f534")

    text = (
        f"<b>Аналитика IWE</b>\n{sep}\n\n"
        f"<b>\U0001f465 Пользователи</b>\n"
        f"  DAU: {u['dau']} | WAU: {u['wau']} | MAU: {u['mau']}\n"
        f"  Всего: {u['total']} | Новых сегодня: {u['new_today']} | за неделю: {u['new_week']}\n\n"
        f"<b>\U0001f4f1 Сессии (24ч)</b>\n"
        f"  Всего: {s['count']} | Средняя: {avg_min}м {avg_sec}с\n"
        f"  Средний запросов/сессия: {s['avg_requests']}\n"
        f"  Entry points: {entry_str}\n\n"
        f"<b>\u26a1 Качество (24ч)</b>\n"
        f"  {lat_emoji} Avg latency: {q['avg_ms']}ms | P95: {q['p95_ms']}ms\n"
        f"  Red-zone (>8s): {q['red_zone']} запросов\n"
        f"  QA helpful: {q['qa_helpful_rate']}% ({q['qa_total']} консультаций)\n\n"
        f"<b>\U0001f4c8 Retention</b>\n"
        f"  D1: {r['d1']}% | D7: {r['d7']}% | D30: {r['d30']}%\n\n"
        f"<b>\U0001f525 Тренды (vs прошлая неделя)</b>\n"
        f"  {dau_arrow} DAU: {tr['dau_change_pct']:+d}% ({tr['dau_last_week']}\u2192{tr['dau_this_week']})\n"
        f"  {sess_arrow} Sessions: {tr['sessions_change_pct']:+d}% ({tr['sessions_last_week']}\u2192{tr['sessions_this_week']})\n"
    )

    text = truncate_safe(text)

    return text


@dev_router.message(Command("reset"))
async def cmd_reset(message: Message):
    """/reset <chat_id> — полный wipe тестера (удаляет ВСЁ, включая профиль → повторный онбординг)."""
    if not _is_developer(message.chat.id):
        return

    args = message.text.strip().split()
    if len(args) < 2:
        await message.answer(
            "<b>Использование:</b> /reset &lt;chat_id&gt;\n\n"
            "Полный wipe: удаляет ВСЕ данные пользователя (профиль, прогресс, подписки).\n"
            "При следующем /start тестер проходит онбординг заново.\n\n"
            "<i>Для мягкого сброса (только прогресс) — пользователь сам через /mydata.</i>",
            parse_mode="HTML",
        )
        return

    try:
        target_id = int(args[1])
    except ValueError:
        await message.answer("chat_id должен быть числом.")
        return

    from db.queries import get_intern
    intern = await get_intern(target_id)
    if not intern:
        await message.answer(f"Пользователь {target_id} не найден.")
        return

    from db.queries.profile import delete_all_user_data
    result = await delete_all_user_data(target_id)
    total = sum(result.values())

    name = intern.get('name', '—')
    details = " | ".join(f"{k}: {v}" for k, v in result.items() if v > 0)

    await message.answer(
        f"<b>Полный wipe выполнен</b>\n\n"
        f"Пользователь: {name} ({target_id})\n"
        f"Удалено строк: {total}\n"
        f"Детали: {details or 'нет данных'}\n\n"
        f"При следующем /start — онбординг заново.",
        parse_mode="HTML",
    )


@dev_router.message(Command("delivery"))
async def cmd_delivery(message: Message):
    """/delivery — отчёт о доставке уроков марафона за сегодня."""
    if not _is_developer(message.chat.id):
        return

    from db.queries.dev_stats import get_delivery_report

    try:
        report = await get_delivery_report()
    except Exception as e:
        logger.error(f"[Dev] /delivery error: {e}")
        await message.answer("Ошибка загрузки отчёта о доставке.")
        return

    sep = "\u2500" * 20

    # Per-user lines
    user_lines = ""
    for u in report['users']:
        status = u['status']
        if status == 'delivered':
            emoji = "\U0001f7e2"
            label = f"доставлено {u['time']}"
        elif status == 'pending':
            emoji = "\U0001f7e1"
            label = f"ожидает клика ({u['time']})"
        elif status == 'not_yet':
            emoji = "\u23f3"
            label = f"ждёт {u['schedule']}"
        elif status == 'missed':
            emoji = "\U0001f534"
            label = f"ПРОПУСК (план {u['schedule']})"
        else:
            emoji = "\u2753"
            label = status

        name = u.get('username') or str(u.get('chat_id', '?'))
        user_lines += f"  {emoji} @{name}: {label}\n"

    s = report['summary']

    text = (
        f"<b>Доставка марафона</b>\n{sep}\n\n"
        f"<b>Сводка</b>\n"
        f"  Активных: {s['active']}\n"
        f"  \U0001f7e2 Доставлено: {s['delivered']}\n"
        f"  \U0001f7e1 Ожидает клика: {s['pending']}\n"
        f"  \u23f3 Время не наступило: {s['not_yet']}\n"
        f"  \U0001f534 Пропущено: {s['missed']}\n\n"
        f"<b>Пользователи</b>\n{user_lines}"
    )

    text = truncate_safe(text)
    await message.answer(text, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════
# L2 AUTO-FIX: approve/reject callbacks (WP-45 Phase 3)
# ═══════════════════════════════════════════════════════════

@dev_router.callback_query(F.data.startswith("autofix_"))
async def cb_autofix(callback: CallbackQuery):
    """Handle auto-fix approval/rejection via inline buttons."""
    if not _is_developer(callback.from_user.id):
        await callback.answer("Access denied", show_alert=True)
        return

    data = callback.data

    if data.startswith("autofix_approve_"):
        fix_id = int(data.split("_")[-1])
        await callback.answer("\u2699\ufe0f Applying fix...")

        from core.autofix import apply_fix
        pr_url = await apply_fix(fix_id)

        if pr_url:
            await callback.message.edit_text(
                f"\u2705 <b>Fix #{fix_id} applied</b>\n\n"
                f"PR: {pr_url}",
                parse_mode="HTML",
            )
        else:
            await callback.message.edit_text(
                f"\u26a0\ufe0f <b>Fix #{fix_id} failed</b>\n\n"
                f"Check logs: <code>[AutoFix]</code>",
                parse_mode="HTML",
            )

    elif data.startswith("autofix_reject_"):
        fix_id = int(data.split("_")[-1])
        await callback.answer("Fix rejected")

        from core.autofix import reject_fix
        await reject_fix(fix_id)

        await callback.message.edit_text(
            f"\u274c <b>Fix #{fix_id} rejected</b>",
            parse_mode="HTML",
        )
