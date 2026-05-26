#!/usr/bin/env python3
"""
Анализ начислений баллов / бонусов для пилота.

Usage:
    export REWARDS_URL="postgresql://..."
    python scripts/analyze_points.py <account_id>

Выводит:
- Сводку по дням (raw vs effective, cap достигнут?)
- Топ активностей по effective
- % дней с достигнутым потолком
- Рекомендации по оптимизации
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

import asyncpg


REWARD_COLORS = {
    "lesson_completed": "🎓",
    "learning_completed": "🎓",
    "training_passed": "🧠",
    "test_passed": "🧪",
    "task_submitted": "📝",
    "text_submitted": "📝",
    "table_submitted": "📊",
    "feed_completed": "📖",
    "marathon_step": "🏃",
    "marathon_task": "🏃",
    "workbook_push": "📓",
    "pomodoro_completed": "🍅",
    "qualification_granted": "🏅",
    "strategy_session_completed": "🎯",
    "knowledge_extracted": "💡",
    "distinction_added": "🔍",
    "method_described": "🔧",
    "topic_created": "💬",
    "comment_created": "💬",
    "day_open": "🌅",
    "day_close": "🌙",
    "week_plan_created": "📅",
    "week_plan_closed": "📅",
    "month_plan_closed": "📅",
    "slot_logged": "⏱️",
    "pack_updated": "🎒",
    "iwe_session": "🖥️",
    "ai_chat": "🤖",
    "ai_interaction": "🤖",
    "note_to_capture": "📝",
    "wp_created": "📋",
    "wp_closed": "📋",
    "wp_completed": "📋",
    "git_commit": "🐙",
    "commit_created": "🐙",
    "fmt_commit_merged": "🔀",
    "coding_time": "💻",
    "content_published": "📢",
    "payment_received": "💳",
}


def _icon(event_type: str) -> str:
    return REWARD_COLORS.get(event_type, "•")


async def main():
    if len(sys.argv) < 2:
        print("Usage: python analyze_points.py <account_id>")
        sys.exit(1)

    account_id = sys.argv[1]
    rewards_url = os.getenv("REWARDS_URL")
    if not rewards_url:
        print("❌ Установите REWARDS_URL")
        sys.exit(1)

    pool = await asyncpg.create_pool(rewards_url, min_size=1, max_size=3)

    async with pool.acquire() as conn:
        # ── 1. Общая сводка ──────────────────────────────────────────────
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS total_events,
                COALESCE(SUM(effective), 0) AS total_effective,
                COALESCE(SUM(base_amount * dom_mult * qual_mult * streak_mult), 0) AS total_raw,
                COALESCE(SUM(CASE WHEN cap_truncated THEN effective ELSE 0 END), 0) AS lost_to_cap,
                COUNT(CASE WHEN cap_truncated THEN 1 END) AS capped_events
            FROM applied_events
            WHERE account_id = $1
            """,
            account_id,
        )
        total_events = row["total_events"]
        total_effective = float(row["total_effective"])
        total_raw = float(row["total_raw"])
        lost_to_cap = float(row["lost_to_cap"])
        capped_events = row["capped_events"]

        print(f"\n{'='*60}")
        print(f"🏆 Анализ начислений для {account_id}")
        print(f"{'='*60}")
        print(f"Всего событий:        {total_events}")
        print(f"Суммарный raw:        {total_raw:.1f}")
        print(f"Суммарный effective:  {total_effective:.1f}")
        if total_raw > 0:
            print(f"Эффективность:        {total_effective/total_raw*100:.1f}% (остальное съел cap)")
        print(f"Событий с обрезкой:   {capped_events}")

        # ── 2. По дням ───────────────────────────────────────────────────
        day_rows = await conn.fetch(
            """
            SELECT
                DATE(applied_at) AS day,
                COUNT(*) AS events,
                COALESCE(SUM(effective), 0) AS eff,
                COALESCE(SUM(base_amount * dom_mult * qual_mult * streak_mult), 0) AS raw,
                BOOL_OR(cap_truncated) AS cap_hit,
                MAX(daily_cap) AS daily_cap
            FROM applied_events
            WHERE account_id = $1
            GROUP BY DATE(applied_at)
            ORDER BY day DESC
            LIMIT 30
            """,
            account_id,
        )

        print(f"\n📅 Последние 30 дней:")
        print(f"{'Дата':<12} {'Событий':>8} {'Raw':>8} {'Eff':>8} {'Cap':>6} {'%Cap':>6}")
        cap_days = 0
        total_days = 0
        for dr in day_rows:
            total_days += 1
            day = dr["day"].strftime("%d.%m")
            ev = dr["events"]
            raw = float(dr["raw"])
            eff = float(dr["eff"])
            cap = dr["daily_cap"]
            hit = "🔴" if dr["cap_hit"] else "🟢"
            if dr["cap_hit"]:
                cap_days += 1
            pct = f"{eff/cap*100:.0f}" if cap and cap > 0 else "—"
            print(f"{day:<12} {ev:>8} {raw:>8.1f} {eff:>8.1f} {hit:>6} {pct:>6}")

        if total_days > 0:
            print(f"\n📊 Дней с достигнутым потолком: {cap_days}/{total_days} ({cap_days/total_days*100:.0f}%)")

        # ── 3. Топ активностей ───────────────────────────────────────────
        top_rows = await conn.fetch(
            """
            SELECT
                event_type,
                COUNT(*) AS cnt,
                COALESCE(SUM(effective), 0) AS eff,
                COALESCE(AVG(effective), 0) AS avg_eff
            FROM applied_events
            WHERE account_id = $1
            GROUP BY event_type
            ORDER BY eff DESC
            LIMIT 15
            """,
            account_id,
        )

        print(f"\n🔥 Топ активностей по effective:")
        for tr in top_rows:
            icon = _icon(tr["event_type"])
            print(f"  {icon} {tr['event_type']:<30} {tr['cnt']:>3}×  = {float(tr['eff']):>8.1f} (средн. {float(tr['avg_eff']):.1f})")

        # ── 4. Последние события с raw=0 (возможные проблемы) ────────────
        zero_rows = await conn.fetch(
            """
            SELECT event_type, applied_at, raw_amount, effective, cap_truncated, daily_cap
            FROM applied_events
            WHERE account_id = $1 AND effective = 0
            ORDER BY applied_at DESC
            LIMIT 10
            """,
            account_id,
        )

        if zero_rows:
            print(f"\n⚠️ Последние события с effective = 0 (проверь, не упираешься ли в cap):")
            for zr in zero_rows:
                print(f"  • {_icon(zr['event_type'])} {zr['event_type']} @ {zr['applied_at'].strftime('%d.%m %H:%M')} | raw={float(zr['raw_amount']):.1f} cap={zr['cap_truncated']} daily_cap={zr['daily_cap']}")

        # ── 5. Рекомендации ──────────────────────────────────────────────
        print(f"\n💡 Рекомендации:")
        if cap_days / max(total_days, 1) < 0.3:
            print("  1. Ты редко достигаешь daily cap. Чтобы ускорить рост:")
            print("     • Закрывай день (/day_close) — стабильный +бонус.")
            print("     • Делай уроки и фиксируй слоты саморазвития — дают больше raw.")
            print("     • Повышай квалификацию — чем выше статус, тем выше cap.")
        elif cap_days / max(total_days, 1) > 0.8:
            print("  1. Ты почти каждый день упираешься в cap! Отлично.")
            print("     • Чтобы получать ЕЩЁ больше — повышай квалификацию (daily_cap растёт).")
            print("     • Фокусируйся на high-value активностях: уроки, стратсессии, публикации.")
        else:
            print("  1. Cap достигаешь примерно в половине дней.")
            print("     • Добавь 1–2 регулярных действия (/day_open + /day_close + слот) — и будет стабильный 100%.")

        # Проверяем, есть ли домены с высоким raw но низким effective
        domain_rows = await conn.fetch(
            """
            SELECT
                event_type,
                COALESCE(SUM(base_amount * dom_mult * qual_mult * streak_mult), 0) AS raw,
                COALESCE(SUM(effective), 0) AS eff
            FROM applied_events
            WHERE account_id = $1
            GROUP BY event_type
            HAVING COALESCE(SUM(base_amount * dom_mult * qual_mult * streak_mult), 0) > COALESCE(SUM(effective), 0) * 1.5
            ORDER BY (SUM(base_amount * dom_mult * qual_mult * streak_mult) - SUM(effective)) DESC
            LIMIT 5
            """,
            account_id,
        )
        if domain_rows:
            print(f"\n  2. Активности, где больше всего 'теряется' из-за cap:")
            for dr in domain_rows:
                loss = float(dr["raw"]) - float(dr["eff"])
                print(f"     • {_icon(dr['event_type'])} {dr['event_type']}: потеряно ~{loss:.1f} баллов")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
