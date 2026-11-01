from __future__ import annotations

"""WP-117 Ф-cross-pipeline-contradiction: реестр источников факта активности.

Каждое правило из engagement_analyzer.RULES, чей кандидат проходит через
кросс-конвейерный арбитр (core/claims.py), декларирует здесь источник своего
факта. Назначение — не пустить повтор инцидента 02-03.10.2026 (похвала и
упрёк об одной неделе из разных источников) на ДРУГОМ правиле: новое правило
без записи здесь не получает защиты арбитра молча, тест
test_activity_source_registry.py проверяет полноту.
"""

# Правила, читающие живой источник (development.user_events, WP-117
# увеличение 1, db/queries/nudges.py:events_7d_live) — арбитр НЕ подавляет
# их кандидатов: они сами и есть самое свежее наблюдение ("свежее живое
# наблюдение побеждает", консенсус 02-writer.md).
LIVE_SOURCE_RULES: frozenset[str] = frozenset({
    "low_engagement_7d",
    "inactivity_3d",
    "streak_drop",
})

# Правила, читающие снимок цифрового двойника — его штатная синхронизация
# отключена (core/scheduler.py:1300-1303, WP-268), секция может устареть без
# предупреждения. Уже отключены через WP-117 stopgap (nudge_policy.py), но
# декларация здесь нужна для полноты реестра (тест) и на случай, если
# stopgap когда-то снимут.
DIGITAL_TWIN_SNAPSHOT_RULES: frozenset[str] = frozenset({
    "achievement_sessions",
    "achievement_active_days",
    "stage_upgrade",
})
