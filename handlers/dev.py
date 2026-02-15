"""
Команды разработчика: /stats, /usage, /qa, /health, /latency.

Доступны только для DEVELOPER_CHAT_ID.
"""

import logging
import os

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

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
        await message.answer("Error fetching stats.")
        return

    sep = "\u2500" * 20

    lang_str = " | ".join(f"{r['lang']}: {r['cnt']}" for r in langs)
    complexity_str = " | ".join(f"L{r['lvl']}: {r['cnt']}" for r in complexity)

    text = (
        f"<b>User Statistics</b>\n{sep}\n\n"
        f"<b>Users</b>\n"
        f"  Total: {s.get('total', 0)} | Onboarded: {s.get('onboarded', 0)}\n"
        f"  Active today: {s.get('active_today', 0)} | This week: {s.get('active_week', 0)}\n\n"
        f"<b>Modes</b>\n"
        f"  \U0001f4da Marathon: {s.get('marathon_active', 0)} active"
        f" | {s.get('marathon_completed', 0)} done"
        f" | {s.get('marathon_paused', 0)} paused\n"
        f"  \U0001f4d6 Feed: {s.get('feed_active', 0)} active\n"
        f"  \U0001f504 Both: {s.get('both_active', 0)}\n\n"
        f"<b>Engagement</b>\n"
        f"  Avg active days: {s.get('avg_active_days', 0)}\n"
        f"  Avg streak: {s.get('avg_streak', 0)} | Max: {s.get('max_streak', 0)}\n"
        f"  Avg complexity: {s.get('avg_complexity', 0)}\n\n"
        f"<b>Complexity</b>: {complexity_str}\n"
        f"<b>Languages</b>: {lang_str}\n\n"
        f"<b>Integrations</b>\n"
        f"  \U0001f4bb GitHub: {integrations.get('github_connected', 0)}\n"
        f"  \U0001f9ea Assessed: {integrations.get('assessed_users', 0)}"
        f" ({integrations.get('total_assessments', 0)} tests)\n"
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
        await message.answer("Error fetching usage.")
        return

    sep = "\u2500" * 20

    svc_lines = ""
    for r in services:
        svc_lines += f"  {r['service_id']}: {r['cnt']} ({r['users']} users)\n"

    sched_lines = ""
    for r in schedule:
        sched_lines += f"  {r['hour']}: {r['cnt']} users\n"

    text = (
        f"<b>Service Usage</b>\n{sep}\n\n"
        f"<b>Top services</b> (total clicks | unique users):\n"
        f"{svc_lines}\n"
        f"<b>Schedule distribution</b>:\n"
        f"{sched_lines}"
    )

    if len(text) > 4000:
        text = text[:4000] + "\n\n... (truncated)"

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
        await message.answer("Error fetching QA stats.")
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
        f"<b>Consultation Analytics</b>\n{sep}\n\n"
        f"<b>Volume</b>\n"
        f"  Total: {total} | Today: {s.get('today', 0)} | Week: {s.get('this_week', 0)}\n"
        f"  Unique users: {s.get('unique_users', 0)}\n\n"
        f"<b>Quality</b>\n"
        f"  \U0001f44d {helpful} | \U0001f44e {not_helpful} | Rate: {rate}\n"
        f"  No feedback: {s.get('no_feedback', 0)} | Comments: {s.get('with_comments', 0)}\n\n"
        f"<b>Top topics</b>:\n"
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
        await message.answer("Error fetching health.")
        return

    sep = "\u2500" * 20

    table_lines = ""
    for r in tables:
        cnt = r['count'] if r['count'] >= 0 else "ERR"
        table_lines += f"  {r['table']}: {cnt}\n"

    text = (
        f"<b>System Health</b>\n{sep}\n\n"
        f"<b>Table sizes</b>:\n"
        f"{table_lines}\n"
        f"<b>Marathon</b>\n"
        f"  Pending content: {pending}\n\n"
        f"<b>Feedback</b>\n"
        f"  \U0001f195 New: {feedback.get('new_count', 0)}"
        f" | \U0001f534 Red: {feedback.get('red_count', 0)}"
        f" | \U0001f7e1 Yellow: {feedback.get('yellow_count', 0)}"
        f" | \U0001f7e2 Green: {feedback.get('green_count', 0)}\n"
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
        await message.answer("Error fetching latency report.")
        return

    sep = "\u2500" * 20
    s = report['summary']

    # Thresholds legend
    legend = (
        "\U0001f7e2 nav &lt;1s | heavy &lt;3s | consult &lt;8s\n"
        "\U0001f7e1 nav &lt;3s | heavy &lt;8s | consult &lt;20s\n"
        "\U0001f534 above yellow\n"
    )

    # By command
    cmd_lines = ""
    for r in report['by_command']:
        cat = classify_command(r['command'])
        color = get_color(r['avg_ms'], cat)
        cmd_lines += f"  {color} {r['command']}: {r['avg_ms']}ms avg | p95={r['p95_ms']}ms | n={r['count']}\n"

    # Slowest spans
    span_lines = ""
    for r in report['slowest_spans'][:6]:
        span_lines += f"  {r['name']}: {r['avg_ms']}ms avg | max={r['max_ms']}ms\n"

    # Red alerts
    red_lines = ""
    if report['red_traces']:
        for r in report['red_traces']:
            ms = int(r['total_ms'])
            red_lines += f"  \U0001f534 {r['command']}: {ms}ms\n"
    else:
        red_lines = "  \u2014 none\n"

    text = (
        f"<b>Latency Report (24h)</b>\n{sep}\n\n"
        f"<b>Summary</b>\n"
        f"  Requests: {s['total']} | Avg: {s['avg_ms']}ms | P95: {s['p95_ms']}ms\n"
        f"  \U0001f534 Red zone: {report['red_count']}\n\n"
        f"<b>Thresholds</b>\n{legend}\n"
        f"<b>By command</b>\n{cmd_lines}\n"
        f"<b>Slowest spans</b>\n{span_lines}\n"
        f"<b>Red alerts</b>\n{red_lines}"
    )

    if len(text) > 4000:
        text = text[:4000] + "\n\n... (truncated)"

    await message.answer(text, parse_mode="HTML")
