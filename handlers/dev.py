from __future__ import annotations

"""
Команды разработчика: /stats, /usage, /qa, /health, /latency, /errors, /tailor, autofix callbacks.

Доступны только для DEVELOPER_CHAT_ID.
"""

import logging
import os

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command

from helpers.message_split import truncate_safe

from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

MOSCOW_TZ = timezone(timedelta(hours=3))

dev_router = Router(name="dev")


def _msk_now() -> str:
    """Текущее время МСК для заголовков отчётов: '23.02 14:35 MSK'."""
    now = datetime.now(MOSCOW_TZ)
    return now.strftime('%d.%m %H:%M MSK')


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
        f"<b>Статистика пользователей</b> ({_msk_now()})\n{sep}\n\n"
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
        f"<b>Использование сервисов</b> ({_msk_now()})\n{sep}\n\n"
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
        f"<b>Аналитика консультаций</b> ({_msk_now()})\n{sep}\n\n"
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

    try:
        from db.queries.dev_stats import get_table_sizes, get_pending_content_count
        from db.queries.feedback import get_report_stats

        tables = await get_table_sizes()
        pending = await get_pending_content_count()
        feedback = await get_report_stats()

        sep = "\u2500" * 20

        table_lines = ""
        for r in tables:
            cnt = r['count'] if r['count'] >= 0 else "ERR"
            table_lines += f"  {r['table']}: {cnt}\n"

        # Scheduler processes info
        try:
            from core.scheduler import _scheduler
            if _scheduler is not None:
                sched_status = "running" if _scheduler.running else "stopped"
                job_count = len(_scheduler.get_jobs()) if _scheduler.running else 0
            else:
                sched_status = "disabled"
                job_count = 0
        except Exception:
            sched_status = "error"
            job_count = 0

        text = (
            f"<b>Состояние системы</b> ({_msk_now()})\n{sep}\n\n"
            f"<b>Размеры таблиц</b>:\n"
            f"{table_lines}\n"
            f"<b>Марафон</b>\n"
            f"  Ожидает контент: {pending}\n\n"
            f"<b>Обратная связь</b>\n"
            f"  \U0001f195 Новые: {feedback.get('new_count', 0)}"
            f" | \U0001f534 Плохо: {feedback.get('red_count', 0)}"
            f" | \U0001f7e1 Средне: {feedback.get('yellow_count', 0)}"
            f" | \U0001f7e2 Хорошо: {feedback.get('green_count', 0)}\n\n"
            f"<b>Процессы</b> ({sched_status}, {job_count} jobs)\n"
            f"  Доставка уроков: каждую мин\n"
            f"  Пре-генерация: каждую мин (3ч ahead)\n"
            f"  Публикатор: :07,:37 + скан 05:07\n"
            f"  Discourse comments: :03,:18,:33,:48\n"
            f"  C3 milestones: 11:00\n"
            f"  C7 events: 12:00\n"
            f"  Feed digest: при доставке\n"
            f"  Trial expiry: 10:00\n"
            f"  Integrity check: 08:00\n"
            f"  Feedback digest: 21:00 / Пн 10:00\n"
        )

        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"[Dev] /health error: {e}", exc_info=True)
        await message.answer(f"<b>/health error:</b>\n<code>{e}</code>", parse_mode="HTML")


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
        avg_ms = r.get('avg_ms')
        color = get_color(avg_ms, cat) if avg_ms is not None else '⚪'
        cmd_lines += f"  {color} {r['command']}: {avg_ms if avg_ms is not None else 'N/A'}мс сред. | p95={r['p95_ms'] if r.get('p95_ms') is not None else 'N/A'}мс | n={r['count']}\n"

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
        f"<b>Отчёт по латентности</b> (24ч, {_msk_now()})\n{sep}\n\n"
        f"<b>Сводка</b>\n"
        f"  Запросов: {s['total']} | Среднее: {s['avg_ms'] if s.get('avg_ms') is not None else 'N/A'}мс | P95: {s['p95_ms'] if s.get('p95_ms') is not None else 'N/A'}мс\n"
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

    import html as html_mod
    from db.queries.errors import get_error_report

    try:
        report = await get_error_report(hours=24)

        sep = "\u2500" * 20
        s = report['summary']

        if s['unique_errors'] == 0:
            await message.answer(
                f"<b>Отчёт по ошибкам</b> (24ч, {_msk_now()})\n{sep}\n\n"
                f"\U0001f7e2 Ошибок за последние 24 часа нет.",
                parse_mode="HTML"
            )
            return

        # По источникам
        logger_lines = ""
        for r in report['by_logger']:
            name = html_mod.escape(r['logger_name'])
            logger_lines += f"  {name}: {r['count']} уник. ({r['total_occurrences']} всего)\n"

        # Последние ошибки
        recent_lines = ""
        for r in report['recent'][:8]:
            emoji = "\U0001f534" if r['level'] == 'CRITICAL' else "\U0001f7e1"
            msg = html_mod.escape((r['message'] or '')[:60])
            count_str = f" x{r['occurrence_count']}" if r['occurrence_count'] > 1 else ""
            recent_lines += f"  {emoji} {html_mod.escape(r['logger_name'])}: {msg}{count_str}\n"

        text = (
            f"<b>Отчёт по ошибкам</b> (24ч, {_msk_now()})\n{sep}\n\n"
            f"<b>Сводка</b>\n"
            f"  Уник. ошибок: {s['unique_errors']}"
            f" | Всего случаев: {s['total_occurrences']}\n"
            f"  \U0001f534 Критических: {s['critical_count']}\n\n"
            f"<b>По источникам</b>\n{logger_lines}\n"
            f"<b>Последние</b>\n{recent_lines}"
        )

        text = truncate_safe(text)

        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"[Dev] /errors error: {e}")
        await message.answer("Ошибка загрузки отчёта по ошибкам.")


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
    e = report['errors']
    cmd = report['commands']
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
    avg_ms_val = q.get('avg_ms')
    lat_emoji = "\U0001f7e2" if (avg_ms_val is not None and avg_ms_val < 3000) else ("\U0001f7e1" if (avg_ms_val is not None and avg_ms_val < 8000) else "\U0001f534")

    # Error rate color
    err_emoji = "\U0001f7e2" if e['error_rate_pct'] < 5 else ("\U0001f7e1" if e['error_rate_pct'] < 15 else "\U0001f534")

    # Error categories
    cat_str = ", ".join(f"{c['category']}({c['count']})" for c in e.get('by_category', [])[:4]) or "\u2014"

    # Severity breakdown
    sev_str = " | ".join(f"{s_['severity']}:{s_['count']}" for s_ in e.get('by_severity', [])) or "\u2014"

    # Top commands
    top_cmd_str = ""
    for c in cmd.get('top', [])[:5]:
        avg = c.get('avg_ms')
        top_cmd_str += f"  {c['command']}: {c['count']} req, ~{avg if avg is not None else 'N/A'}ms\n"
    top_cmd_str = top_cmd_str or "  \u2014\n"

    # Slowest commands
    slow_str = ""
    for c in cmd.get('slowest', [])[:3]:
        avg = c.get('avg_ms')
        p95 = c.get('p95_ms')
        slow_str += f"  {c['command']}: avg {avg if avg is not None else 'N/A'}ms, p95 {p95 if p95 is not None else 'N/A'}ms ({c['count']} req)\n"
    slow_str = slow_str or "  \u2014\n"

    text = (
        f"<b>Аналитика IWE</b> ({_msk_now()})\n{sep}\n\n"
        f"<b>\U0001f465 Пользователи</b>\n"
        f"  DAU: {u['dau']} | WAU: {u['wau']} | MAU: {u['mau']}\n"
        f"  Всего: {u['total']} | Новых сегодня: {u['new_today']} | за неделю: {u['new_week']}\n\n"
        f"<b>\U0001f4f1 Сессии (24ч)</b>\n"
        f"  Всего: {s['count']} | Средняя: {avg_min}м {avg_sec}с\n"
        f"  Средний запросов/сессия: {s['avg_requests']}\n"
        f"  Entry points: {entry_str}\n\n"
        f"<b>\u26a1 Latency (24ч)</b>\n"
        f"  {lat_emoji} p50: {q['p50_ms'] if q['p50_ms'] is not None else 'N/A'}ms | p95: {q['p95_ms'] if q['p95_ms'] is not None else 'N/A'}ms | p99: {q['p99_ms'] if q['p99_ms'] is not None else 'N/A'}ms\n"
        f"  Avg: {q['avg_ms'] if q['avg_ms'] is not None else 'N/A'}ms | Red-zone (>8s): {q['red_zone']}\n"
        f"  Запросов: {q['total_requests']} | QA helpful: {q['qa_helpful_rate']}% ({q['qa_total']})\n\n"
        f"<b>\U0001f6a8 Ошибки (24ч)</b>\n"
        f"  {err_emoji} Error rate: {e['error_rate_pct']}% | Всего: {e['total']}\n"
        f"  L3+: {e['l3_plus']} | Unknown: {e['unknown']}\n"
        f"  Severity: {sev_str}\n"
        f"  Top: {cat_str}\n\n"
        f"<b>\U0001f3af Команды (24ч)</b>\n"
        f"{top_cmd_str}\n"
        f"<b>\U0001f422 Самые медленные</b>\n"
        f"{slow_str}\n"
        f"<b>\U0001f4c8 Retention</b>\n"
        f"  D1: {r['d1']}% | D7: {r['d7']}% | D30: {r['d30']}%\n\n"
        f"<b>\U0001f525 Тренды (vs прошлая неделя)</b>\n"
        f"  {dau_arrow} WAU: {tr['dau_change_pct']:+d}% ({tr['dau_last_week']}\u2192{tr['dau_this_week']})\n"
        f"  {sess_arrow} Sessions: {tr['sessions_change_pct']:+d}% ({tr['sessions_last_week']}\u2192{tr['sessions_this_week']})\n"
    )

    text = truncate_safe(text)

    return text


@dev_router.message(Command("fix_marathon_startdate"))
async def cmd_fix_marathon_startdate(message: Message):
    """/fix_marathon_startdate <@username|chat_id> — исправить marathon_start_date сбитого пользователя.

    Диагностирует: marathon_start_date, completed_topics, marathon_day.
    Если start_date = сегодня (при наличии пройденных тем) → сдвигает на вчера,
    очищает notification_sent_at в marathon_content чтобы catch-up доставил Day 2.
    """
    if not _is_developer(message.chat.id):
        return

    import json as _json
    from datetime import date, timedelta, timezone
    from db.queries.users import get_intern, update_intern, moscow_today
    from db.queries.channels import find_user_by_username
    from core.topics import get_marathon_day
    from db.connection import get_learning_pool

    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer(
            "<b>Использование:</b> /fix_marathon_startdate @username\n"
            "или: /fix_marathon_startdate &lt;chat_id&gt;",
            parse_mode="HTML",
        )
        return

    target = parts[1].lstrip('@')
    chat_id = None

    # Resolve username → chat_id
    try:
        chat_id = int(target)
    except ValueError:
        row = await find_user_by_username(target)
        if row:
            chat_id = row['chat_id']

    if not chat_id:
        await message.answer(f"❌ Пользователь <code>{target}</code> не найден.", parse_mode="HTML")
        return

    intern = await get_intern(chat_id)
    if not intern:
        await message.answer(f"❌ Intern-запись для {chat_id} не найдена.", parse_mode="HTML")
        return

    today = moscow_today()
    start_date = intern.get('marathon_start_date')
    if start_date and hasattr(start_date, 'date'):
        start_date = start_date.date()

    completed_raw = intern.get('completed_topics') or '[]'
    if isinstance(completed_raw, str):
        try:
            completed = _json.loads(completed_raw)
        except Exception:
            completed = []
    else:
        completed = list(completed_raw) if completed_raw else []

    marathon_day = get_marathon_day(intern)
    marathon_status = intern.get('marathon_status', '—')

    lines = [
        f"<b>Диагностика @{target} ({chat_id})</b>\n",
        f"marathon_status: <code>{marathon_status}</code>",
        f"marathon_start_date: <code>{start_date}</code>",
        f"marathon_day (текущий): <code>{marathon_day}</code>",
        f"completed_topics: <code>{len(completed)}</code> шт.",
        f"current_topic_index: <code>{intern.get('current_topic_index', 0)}</code>",
    ]

    # Нужна ли починка?
    needs_fix = (
        start_date == today
        and len(completed) > 0
        and marathon_status == 'active'
    )

    if not needs_fix:
        lines.append("\n✅ Починка не нужна: start_date ≠ today или нет прогресса.")
        await message.answer("\n".join(lines), parse_mode="HTML")
        return

    # Вычислить корректную дату: days_done = кол-во завершённых дней
    days_done = max(1, len(completed) // 2)  # 2 темы на день
    correct_date = today - timedelta(days=days_done)
    lines.append(f"\n⚠️ start_date = today при {len(completed)} пройденных темах → <b>баг</b>")
    lines.append(f"Корректная дата: <code>{correct_date}</code> (дней пройдено: {days_done})")
    lines.append("Исправляю...")

    await update_intern(chat_id, marathon_start_date=correct_date)

    # Очистить notification_sent_at для текущей темы чтобы catch-up доставил следующий день
    topic_index = intern.get('current_topic_index', 0)
    today_str = today.strftime('%Y-%m-%d')
    try:
        pool = await get_learning_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                '''UPDATE marathon_content
                   SET notification_sent_at = NULL
                   WHERE chat_id = $1 AND topic_index = $2''',
                chat_id, topic_index
            )
            # Очищаем idempotency-запись из learning.domain_event чтобы catch-up снова смог отправить
            marathon_key = f"marathon_lesson:{chat_id}:{today_str}:topic{topic_index}"
            await conn.execute(
                "DELETE FROM domain_event WHERE source = 'aist-bot' AND external_id = $1",
                f"notification-{marathon_key}",
            )
        lines.append("✅ notification_sent_at и idempotency очищены → catch-up пришлёт следующий день в течение 30 мин.")
    except Exception as e:
        lines.append(f"⚠️ Не удалось очистить notification_sent_at: <code>{e}</code>")
        lines.append("Пользователь получит следующий день завтра в своё время.")

    new_day = get_marathon_day({'marathon_start_date': correct_date})
    lines.append(f"\n✅ Готово. Новый marathon_day: <code>{new_day}</code>")

    await message.answer("\n".join(lines), parse_mode="HTML")


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
    report_date = report.get('report_date', '')

    # Per-user lines
    user_lines = ""
    for u in report['users']:
        status = u['status']
        if status == 'sent_read':
            emoji = "\U0001f7e2"
            label = f"открыт ({u['time']})"
        elif status == 'sent_unread':
            emoji = "\U0001f7e1"
            label = f"отправлен, не открыт ({u['time']})"
        elif status == 'not_yet':
            emoji = "\u23f3"
            label = f"ждёт {u['schedule']}"
        elif status == 'missed':
            emoji = "\U0001f534"
            label = f"НЕ ДОСТАВЛЕН (план {u['schedule']})"
        else:
            emoji = "\u2753"
            label = status

        name = u.get('username') or str(u.get('chat_id', '?'))
        user_lines += f"  {emoji} @{name}: {label}\n"

    s = report['summary']

    text = (
        f"<b>Доставка марафона</b> ({report_date}, {_msk_now()})\n{sep}\n\n"
        f"<b>Сводка</b>\n"
        f"  Активных: {s['active']}\n"
        f"  \U0001f7e2 Отправлено: {s['sent']} (открыт урок: {s['sent_read']})\n"
        f"  \u23f3 Время не наступило: {s['not_yet']}\n"
        f"  \U0001f534 Не доставлено: {s['missed']}\n\n"
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


@dev_router.message(Command("dt_sync"))
async def cmd_dt_sync(message: Message):
    """/dt_sync — ручной запуск sync engagement → digital_twins."""
    if not _is_developer(message.chat.id):
        return

    await message.answer("⏳ Запускаю sync engagement → digital_twins...")

    try:
        from db.queries.dt_sync import sync_engagement_to_dt
        stats = await sync_engagement_to_dt()

        text = (
            f"<b>DT Sync</b> ({_msk_now()})\n\n"
            f"✅ Synced: <b>{stats['synced']}</b>\n"
            f"⏭ Skipped: {stats['skipped']}\n"
            f"❌ Errors: {stats['errors']}"
        )
        if stats.get('first_error'):
            text += f"\n\n<b>First error:</b>\n<code>{stats['first_error'][:500]}</code>"
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"[Dev] /dt_sync error: {e}", exc_info=True)
        await message.answer(f"<b>/dt_sync error:</b>\n<code>{e}</code>", parse_mode="HTML")


@dev_router.message(Command("tailor"))
async def cmd_tailor(message: Message):
    """/tailor — ручной триггер занятия Портного (WP-149, SC.020).

    Доступна всем пользователям (генерирует занятие для себя).
    force=True обходит idempotency для повторного тестирования.
    """
    chat_id = message.chat.id
    logger.info(f"[Dev] /tailor triggered by {chat_id}")

    await message.answer(
        "⚠️ <b>/tailor не реализован</b>\n\n"
        "deliver_tailor_lesson() переехала в activity-hub (платформа L2).\n"
        "Интеграция: WP-149 / WP-222.",
        parse_mode="HTML",
    )


@dev_router.message(Command("nudge_test"))
async def cmd_nudge_test(message: Message):
    """/nudge_test — ручной запуск engagement nudges (WP-85 5C)."""
    if not _is_developer(message.chat.id):
        return

    await message.answer("⏳ Запускаю engagement nudges...")

    try:
        from core.scheduler import send_engagement_nudges
        await send_engagement_nudges()
        await message.answer("✅ Nudges отправлены. Проверьте логи.", parse_mode="HTML")
    except Exception as e:
        logger.error(f"[Dev] /nudge_test error: {e}", exc_info=True)
        await message.answer(f"<b>/nudge_test error:</b>\n<code>{e}</code>", parse_mode="HTML")


@dev_router.message(Command("ory_test"))
async def cmd_ory_test(message: Message):
    """/ory_test — тест OAuth flow через Ory (WP-187)."""
    if not _is_developer(message.chat.id):
        return

    from clients.ory_oauth import ory_oauth
    from config import ORY_CLIENT_ID

    if not ORY_CLIENT_ID:
        await message.answer("ORY_CLIENT_ID не задан. Проверь env vars.")
        return

    auth_url, state = await ory_oauth.get_authorization_url(message.chat.id)

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Войти через Ory", url=auth_url)],
    ])

    await message.answer(
        "<b>Тест Ory OAuth (WP-187)</b>\n\n"
        f"Client: <code>{ORY_CLIENT_ID}</code>\n"
        f"State: <code>{state[:16]}...</code>\n\n"
        f"URL: <code>{auth_url}</code>\n\n"
        "Нажми кнопку или скопируй URL в браузер.",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────────────────────────────
# /broadcast_reconnect — разовая рассылка пользователям, у которых
# Gateway-подключение сброшено миграцией Ory на JWT (WP-209 Ф3).
# ─────────────────────────────────────────────────────────────────────

_RECONNECT_MESSAGE = {
    'ru': (
        "⚠️ <b>Сервис авторизации обновлён</b>\n\n"
        "Из-за обновления системы безопасности платформы нужно "
        "переподключить IWE к боту.\n\n"
        "Открой <b>Настройки → 🔗 Подключения → 🌐 Aisystant MCP (IWE) → 🔄 Переподключить</b>.\n\n"
        "После этого /twin, /me и поиск по знаниям снова заработают. "
        "Займёт 10 секунд."
    ),
    'en': (
        "⚠️ <b>Authorization service updated</b>\n\n"
        "Due to a platform security upgrade, you need to reconnect "
        "IWE to the bot.\n\n"
        "Open <b>Settings → 🔗 Connections → 🌐 Aisystant MCP (IWE) → 🔄 Reconnect</b>.\n\n"
        "After that /twin, /me and knowledge search will work again. "
        "Takes 10 seconds."
    ),
    'es': (
        "⚠️ <b>Servicio de autorización actualizado</b>\n\n"
        "Debido a una actualización de seguridad de la plataforma, "
        "necesitas reconectar IWE al bot.\n\n"
        "Abre <b>Ajustes → 🔗 Conexiones → 🌐 Aisystant MCP (IWE) → 🔄 Reconectar</b>.\n\n"
        "Después /twin, /me y la búsqueda de conocimiento volverán a funcionar. "
        "Toma 10 segundos."
    ),
    'fr': (
        "⚠️ <b>Service d'autorisation mis à jour</b>\n\n"
        "Suite à une mise à jour de sécurité de la plateforme, "
        "vous devez reconnecter IWE au bot.\n\n"
        "Ouvrez <b>Paramètres → 🔗 Connexions → 🌐 Aisystant MCP (IWE) → 🔄 Reconnecter</b>.\n\n"
        "Après cela /twin, /me et la recherche de connaissances fonctionneront à nouveau. "
        "10 secondes."
    ),
    'zh': (
        "⚠️ <b>授权服务已更新</b>\n\n"
        "由于平台安全升级，您需要将 IWE 重新连接到机器人。\n\n"
        "打开 <b>设置 → 🔗 连接 → 🌐 Aisystant MCP (IWE) → 🔄 重新连接</b>。\n\n"
        "之后 /twin、/me 和知识搜索将再次正常工作。需要 10 秒。"
    ),
}

# Момент инцидента Ory JWT миграции (UTC). Пользователи с ory_tokens,
# обновлёнными после этого времени, уже переподключились — их пропускаем.
_INCIDENT_CUTOFF_UTC = "2026-04-10 06:00:00"


async def _get_reconnect_candidates():
    """Список chat_id пользователей, которым нужна рассылка.

    Критерии:
    - users.dt_connected_at IS NOT NULL (хотя бы раз подключались)
    - НЕТ свежей записи в ory_tokens (updated_at <= cutoff или отсутствует)
    """
    from db.connection import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT u.telegram_id, COALESCE(u.language, 'ru') AS language, u.name,
                   u.dt_connected_at, ot.updated_at AS ory_updated_at
            FROM public.users u
            LEFT JOIN public.ory_tokens ot ON ot.chat_id = u.telegram_id
            WHERE u.dt_connected_at IS NOT NULL
              AND (ot.updated_at IS NULL OR ot.updated_at <= '{_INCIDENT_CUTOFF_UTC}'::timestamp)
            ORDER BY u.telegram_id
            """
        )
        return [dict(r) for r in rows]


@dev_router.message(Command("broadcast_reconnect"))
async def cmd_broadcast_reconnect(message: Message):
    """/broadcast_reconnect dry|send — рассылка «переподключись к Gateway».

    Использование:
      /broadcast_reconnect dry  — показать список кандидатов, ничего не отправлять
      /broadcast_reconnect send — реальная отправка
    """
    if not _is_developer(message.chat.id):
        return

    import asyncio
    from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter

    parts = (message.text or "").split()
    mode = parts[1].lower() if len(parts) > 1 else "dry"
    if mode not in ("dry", "send"):
        await message.answer("Использование: <code>/broadcast_reconnect dry|send</code>", parse_mode="HTML")
        return

    try:
        candidates = await _get_reconnect_candidates()
    except Exception as e:
        logger.error(f"[broadcast_reconnect] query failed: {e}")
        await message.answer(f"Ошибка запроса: {e}")
        return

    total = len(candidates)
    by_lang: dict = {}
    for c in candidates:
        by_lang[c['language']] = by_lang.get(c['language'], 0) + 1

    header = (
        f"<b>/broadcast_reconnect — {mode}</b>\n\n"
        f"Кандидатов: <b>{total}</b>\n"
        f"По языкам: {by_lang}\n"
        f"Cutoff (исключаем свежее): {_INCIDENT_CUTOFF_UTC} UTC\n"
    )

    if mode == "dry":
        # Показать первые 20 chat_id для визуальной проверки
        sample_lines = [
            f"• <code>{c['telegram_id']}</code> ({c['language']}, "
            f"{c.get('name') or '—'}, dt_connected={c['dt_connected_at']:%Y-%m-%d})"
            for c in candidates[:20]
        ]
        sample = "\n".join(sample_lines) if sample_lines else "—"
        await message.answer(
            header + "\nПервые 20:\n" + sample + "\n\n"
            "Запусти <code>/broadcast_reconnect send</code> для реальной отправки.",
            parse_mode="HTML",
        )
        return

    # mode == "send"
    await message.answer(header + "\n⏳ Начинаю рассылку...", parse_mode="HTML")

    sent = 0
    skipped_blocked = 0
    skipped_error = 0
    bot = message.bot

    for c in candidates:
        chat_id = c['telegram_id']
        lang = c['language'] if c['language'] in _RECONNECT_MESSAGE else 'ru'
        text = _RECONNECT_MESSAGE[lang]
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
            sent += 1
            logger.info(f"[broadcast_reconnect] sent to {chat_id} ({lang})")
        except TelegramForbiddenError:
            skipped_blocked += 1
            logger.info(f"[broadcast_reconnect] blocked by {chat_id}")
        except TelegramRetryAfter as e:
            logger.warning(f"[broadcast_reconnect] flood, sleeping {e.retry_after}s")
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.send_message(chat_id, text, parse_mode="HTML")
                sent += 1
            except Exception as e2:
                skipped_error += 1
                logger.error(f"[broadcast_reconnect] retry failed for {chat_id}: {e2}")
        except Exception as e:
            skipped_error += 1
            logger.error(f"[broadcast_reconnect] send to {chat_id} failed: {e}")
        await asyncio.sleep(0.05)  # 20 msg/sec, под лимитом Telegram 30

    await message.answer(
        f"✅ <b>Рассылка завершена</b>\n\n"
        f"Отправлено: <b>{sent}</b>\n"
        f"Заблокировали бота: {skipped_blocked}\n"
        f"Ошибки: {skipped_error}\n"
        f"Всего кандидатов: {total}",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────────────────────────────
# /user_repair — диагностика и починка GitHub-интеграции пользователя
# (WP-field fix: user_integrations отсутствует → activity-hub не собирает
# коммиты → нет начислений баллов)
# ─────────────────────────────────────────────────────────────────────

@dev_router.message(Command("user_repair"))
async def cmd_user_repair(message: Message):
    """/user_repair <email> [fix] — диагностика и починка GitHub-интеграции.

    Режимы:
      /user_repair email@example.com       — только диагностика
      /user_repair email@example.com fix   — диагностика + создать/обновить
                                             persona.user_integrations из github_connections

    Проверяет:
      1. users (chat_id, tier, email)
      2. ory_identity (account_id, ory_id)
      3. github_connections (github_username, user_uuid)
      4. persona.user_integrations (active, metadata.github_username)
    """
    if not _is_developer(message.chat.id):
        return

    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await message.answer(
            "Использование: <code>/user_repair email@example.com [fix]</code>",
            parse_mode="HTML",
        )
        return

    email = parts[1].strip().lower()
    do_fix = len(parts) >= 3 and parts[2].strip() == "fix"

    lines: list[str] = [f"<b>/user_repair {email}</b>\n"]

    try:
        from db.connection import get_pool, get_secrets_pool, get_persona_pool
        bot_pool = await get_pool()
        secrets_pool = await get_secrets_pool()
        persona_pool = await get_persona_pool()

        # 1. users
        async with bot_pool.acquire() as conn:
            u = await conn.fetchrow(
                "SELECT chat_id, tier, email FROM users WHERE lower(email) = $1 LIMIT 1",
                email,
            )
        if not u:
            lines.append(f"❌ Пользователь <code>{email}</code> не найден в <code>users</code>.")
            await message.answer("\n".join(lines), parse_mode="HTML")
            return

        chat_id = u["chat_id"]
        lines.append(f"✅ users: chat_id=<code>{chat_id}</code>, tier=<code>{u['tier']}</code>")

        # 2. ory_identity
        async with persona_pool.acquire() as conn:
            ory_row = await conn.fetchrow(
                "SELECT account_id, ory_id FROM ory_identity WHERE telegram_id = $1 LIMIT 1",
                chat_id,
            )
        if not ory_row or not ory_row["account_id"]:
            lines.append("❌ ory_identity: нет account_id → repair невозможен (нужен Ory-аккаунт)")
            await message.answer("\n".join(lines), parse_mode="HTML")
            return

        account_id = ory_row["account_id"]
        lines.append(f"✅ ory_identity: account_id=<code>{str(account_id)[:16]}…</code>")

        # 3. github_connections
        from db.queries.github import get_github_connection
        gh = await get_github_connection(chat_id)
        if not gh:
            lines.append("⚠️ github_connections: нет записи → GitHub не подключён через бота")
        else:
            gh_username = gh.get("github_username", "")
            has_token = bool(gh.get("access_token"))
            lines.append(
                f"✅ github_connections: username=<code>{gh_username or '—'}</code>, "
                f"token={'✅' if has_token else '❌'}"
            )

        # 4. persona.user_integrations
        async with persona_pool.acquire() as conn:
            ui = await conn.fetchrow(
                "SELECT active, metadata FROM user_integrations "
                "WHERE account_id = $1 AND service = 'github' LIMIT 1",
                account_id,
            )

        import json as _json
        if not ui:
            lines.append("❌ user_integrations: запись отсутствует → activity-hub не собирает коммиты")
        else:
            meta = ui["metadata"] or {}
            if isinstance(meta, str):
                meta = _json.loads(meta)
            ui_username = meta.get("github_username", "")
            lines.append(
                f"{'✅' if ui['active'] else '⚠️'} user_integrations: "
                f"active=<code>{ui['active']}</code>, "
                f"username=<code>{ui_username or '—'}</code>"
            )

        # 5. Repair
        if do_fix and gh:
            gh_username = gh.get("github_username", "")
            access_token = gh.get("access_token", "")
            if not access_token:
                lines.append("❌ fix: нет access_token в github_connections — нечего писать")
            elif not gh_username:
                lines.append("❌ fix: github_username пуст — нечего писать")
            else:
                try:
                    from db.queries.github import sync_github_to_user_integrations
                    await sync_github_to_user_integrations(
                        chat_id=chat_id,
                        access_token=access_token,
                        github_username=gh_username,
                    )
                    lines.append(
                        f"🔧 fix: persona.user_integrations обновлена "
                        f"(username=<code>{gh_username}</code>)\n"
                        "Activity-hub подхватит на следующем ежедневном sync-iwe."
                    )
                except Exception as fix_err:
                    lines.append(f"❌ fix failed: <code>{fix_err}</code>")
        elif do_fix and not gh:
            lines.append("❌ fix: нет github_connections — попроси пользователя переподключить GitHub через /github")

        if not do_fix:
            lines.append(
                "\n💡 Чтобы починить, добавь <code>fix</code>: "
                f"<code>/user_repair {email} fix</code>"
            )

    except Exception as e:
        logger.error("[user_repair] failed for %s: %s", email, e)
        lines.append(f"❌ Ошибка: <code>{e}</code>")

    await message.answer("\n".join(lines), parse_mode="HTML")
