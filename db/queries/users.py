from __future__ import annotations

"""
Запросы для работы с пользователями.

ЦД-native (WP-82 Phase 3):
  public.users — identity + profile
  development.user_state — bot state

get_intern() и update_intern() — адаптеры, возвращающие тот же dict-формат.
"""

import asyncio
import hashlib
import json
from datetime import datetime, date, timedelta
from typing import Optional, List

from config import get_logger, MOSCOW_TZ, MULTILANG_ENABLED
from db.connection import get_pool, get_learning_pool
from db.sql_helpers import update as _update_sql
from helpers.dual_write import post_event, resolve_ory_id_from_chat

logger = get_logger(__name__)

# ─── Field routing: какие поля в какой таблице ───

PROFILE_FIELDS = frozenset({
    'name', 'occupation', 'role', 'domain', 'interests', 'motivation', 'goals',
    'language', 'experience_level', 'difficulty_preference', 'learning_style',
    'delivery_format', 'detail_level',
    'study_duration', 'current_problems', 'desires', 'tg_username',
    'aisystant_id', 'aisystant_linked_at', 'dt_connected_at',
    'tier', 'email', 'timezone',
})

# Security: whitelist для user_state (предотвращает SQL injection через column names)
STATE_FIELDS = frozenset({
    'mode', 'current_context', 'current_state', 'topic_order',
    'schedule_time', 'schedule_time_2', 'feed_schedule_time',
    'marathon_status', 'marathon_start_date', 'marathon_paused_at',
    'current_topic_index', 'completed_topics', 'topics_today', 'last_topic_date',
    'complexity_level', 'topics_at_current_complexity',
    'feed_status', 'feed_started_at',
    'active_days_total', 'active_days_streak', 'longest_streak', 'last_active_date',
    'onboarding_completed', 'bot_blocked', 'bot_blocked_at', 'bot_recheck_at', 'trial_started_at',
    'assessment_state', 'assessment_date', 'stats_reset_date',
    'notify_template_updates', 'notify_nudges',
    'retry_exhausted_date',
})

# All known fields (union)
ALL_KNOWN_FIELDS = PROFILE_FIELDS | STATE_FIELDS


def moscow_now() -> datetime:
    """Получить текущее время по Москве"""
    return datetime.now(MOSCOW_TZ)


def moscow_today() -> date:
    """Получить текущую дату по Москве"""
    return moscow_now().date()


# ─── SQL для JOIN users + user_state ───

# IDCOL1 (WP-7, 2026-07-06/23): intern['dt_user_id'] — исторический ключ, теперь
# читает канонический u.ory_id (uuid). Этап 2 (2026-07-23) удалил физическую
# колонку dt_user_id, at-source sync и daily reconcile-job — единственный
# источник identity теперь ory_id. Явный ::text cast, т.к. потребители
# (points.py, referral.py и др.) ожидают str, не UUID.
_SELECT_JOINED = '''
    SELECT
        u.id AS user_id, u.telegram_id AS chat_id,
        u.name, u.occupation, u.role, u.domain, u.interests,
        u.motivation, u.goals, u.language, u.experience_level,
        u.difficulty_preference, u.learning_style, u.study_duration,
        u.current_problems, u.desires, u.tg_username,
        u.aisystant_id, u.aisystant_linked_at, u.dt_connected_at,
        u.ory_id::text AS dt_user_id, u.tier, u.email, u.timezone, u.created_at,
        s.mode, s.current_context, s.current_state, s.topic_order,
        s.schedule_time, s.schedule_time_2, s.feed_schedule_time,
        s.marathon_status, s.marathon_start_date, s.marathon_paused_at,
        s.current_topic_index, s.completed_topics, s.topics_today,
        s.last_topic_date, s.complexity_level, s.topics_at_current_complexity,
        s.feed_status, s.feed_started_at,
        s.active_days_total, s.active_days_streak, s.longest_streak,
        s.last_active_date, s.bot_blocked, s.bot_blocked_at, s.bot_recheck_at,
        s.trial_started_at, s.assessment_state, s.assessment_date,
        s.stats_reset_date, s.notify_template_updates,
        s.onboarding_completed, s.retry_exhausted_date,
        COALESCE(s.updated_at, u.updated_at) AS updated_at
    FROM public.users u
    LEFT JOIN development.user_state s ON s.user_id = u.id
    WHERE u.telegram_id = $1
'''


def coerce_ui_lang(lang: Optional[str]) -> str:
    """Language actually surfaced to the user.

    WP-440: while multilingual is disabled (track A bot), every user-facing
    surface is pinned to Russian regardless of the stored or Telegram value,
    so a half-translated non-ru locale never leaks. Flip MULTILANG_ENABLED to
    restore. Apply at every source that derives a UI language WITHOUT going
    through get_intern (onboarding from Telegram locale, scheduler direct-SQL
    reads, legacy fallback).
    """
    if not MULTILANG_ENABLED:
        return 'ru'
    return lang or 'ru'


async def get_intern(chat_id: int) -> dict:
    """Получить профиль + состояние пользователя из БД.

    Создаёт записи в users и user_state если не существуют.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_SELECT_JOINED, chat_id)

        if row:
            result = _row_to_dict(row)
            # user exists but no state → create state
            if result.get('mode') is None:
                await conn.execute('''
                    INSERT INTO development.user_state (user_id, chat_id)
                    VALUES ($1, $2) ON CONFLICT DO NOTHING
                ''', result['user_id'], chat_id)
                result.update(_get_default_state())
            return result

        # No user → create both
        user_row = await conn.fetchrow('''
            INSERT INTO public.users (telegram_id)
            VALUES ($1)
            ON CONFLICT (telegram_id) DO UPDATE SET telegram_id = EXCLUDED.telegram_id
            RETURNING id
        ''', chat_id)
        user_id = user_row['id']

        await conn.execute('''
            INSERT INTO development.user_state (user_id, chat_id)
            VALUES ($1, $2) ON CONFLICT DO NOTHING
        ''', user_id, chat_id)

        # WP-268 Phase 2 Issue 5 fix (verifier subagent a321d6bc):
        # user_registered.v1 эмитится ТОЛЬКО из identity.py:get_or_create_user
        # (более низкий уровень). Здесь — НЕТ дубликата emit.
        # Source-of-truth для регистрации = identity layer, не users.get_intern.

        result = _get_default_intern(chat_id)
        result['user_id'] = user_id
        return result


def _row_to_dict(row) -> dict:
    """Преобразовать строку БД в словарь"""
    def safe_get(key, default=''):
        return row[key] if key in row.keys() and row[key] is not None else default

    def safe_json(key, default=None):
        if default is None:
            default = []
        val = safe_get(key, None)
        if val is None:
            return default
        try:
            return json.loads(val) if isinstance(val, str) else val
        except Exception:
            return default

    return {
        'chat_id': row['chat_id'],
        'user_id': safe_get('user_id', None),
        'name': safe_get('name', ''),
        'occupation': safe_get('occupation', ''),
        'role': safe_get('role', ''),
        'domain': safe_get('domain', ''),
        'interests': safe_json('interests', []),
        'motivation': safe_get('motivation', ''),
        'experience_level': safe_get('experience_level', ''),
        'difficulty_preference': safe_get('difficulty_preference', ''),
        'learning_style': safe_get('learning_style', ''),
        'study_duration': safe_get('study_duration', 15),
        'current_problems': safe_get('current_problems', ''),
        'desires': safe_get('desires', ''),
        'goals': safe_get('goals', ''),
        'schedule_time': safe_get('schedule_time', '09:00'),
        'schedule_time_2': safe_get('schedule_time_2', None),
        'topic_order': safe_get('topic_order', 'default'),

        # Режимы
        'mode': safe_get('mode', 'marathon'),
        'current_context': safe_json('current_context', {}),

        # State Machine
        'current_state': safe_get('current_state', None),

        # Марафон
        'marathon_status': safe_get('marathon_status', 'not_started'),
        'marathon_start_date': safe_get('marathon_start_date', None),
        'marathon_paused_at': safe_get('marathon_paused_at', None),
        'current_topic_index': safe_get('current_topic_index', 0),
        'completed_topics': safe_json('completed_topics', []),
        'topics_today': safe_get('topics_today', 0),
        'last_topic_date': safe_get('last_topic_date', None),

        # Сложность
        'complexity_level': safe_get('complexity_level', None) if safe_get('complexity_level', None) is not None else 1,
        'topics_at_current_complexity': safe_get('topics_at_current_complexity', None) if safe_get('topics_at_current_complexity', None) is not None else 0,
        # Обратная совместимость (aliases)
        'bloom_level': safe_get('complexity_level', None) if safe_get('complexity_level', None) is not None else 1,
        'topics_at_current_bloom': safe_get('topics_at_current_complexity', None) if safe_get('topics_at_current_complexity', None) is not None else 0,

        # Лента
        'feed_status': safe_get('feed_status', 'not_started'),
        'feed_started_at': safe_get('feed_started_at', None),
        'feed_schedule_time': safe_get('feed_schedule_time', None),

        # Систематичность
        'active_days_total': safe_get('active_days_total', 0),
        'active_days_streak': safe_get('active_days_streak', 0),
        'longest_streak': safe_get('longest_streak', 0),
        'last_active_date': safe_get('last_active_date', None),

        # Оценка
        'assessment_state': safe_get('assessment_state', None),
        'assessment_date': safe_get('assessment_date', None),

        # Сброс статистики
        'stats_reset_date': safe_get('stats_reset_date', None),

        # Подписка / DT
        'trial_started_at': safe_get('trial_started_at', None),
        'dt_connected_at': safe_get('dt_connected_at', None),
        'dt_user_id': safe_get('dt_user_id', None),
        'created_at': safe_get('created_at', None),

        # Telegram
        'tg_username': safe_get('tg_username', None),

        # Статусы
        'onboarding_completed': safe_get('onboarding_completed', False),
        # WP-440: pin UI language to Russian while multilingual is disabled.
        'language': coerce_ui_lang(safe_get('language', 'ru')),

        # IWE template update notifications (WP-90)
        'notify_template_updates': safe_get('notify_template_updates', False),

        # Tier cache (WP-52 / WP-85) — persisted in public.users, parsed by onboarding
        'tier': safe_get('tier', None),

        # Scheduler exhaustion guard (BE5-fix)
        'retry_exhausted_date': safe_get('retry_exhausted_date', None),
    }


def _get_default_state() -> dict:
    """Дефолтные значения для state-полей (когда user_state только создан)."""
    return {
        'mode': 'marathon',
        'current_context': {},
        'current_state': None,
        'topic_order': 'default',
        'schedule_time': '09:00',
        'schedule_time_2': None,
        'feed_schedule_time': None,
        'marathon_status': 'not_started',
        'marathon_start_date': None,
        'marathon_paused_at': None,
        'current_topic_index': 0,
        'completed_topics': [],
        'topics_today': 0,
        'last_topic_date': None,
        'complexity_level': 1,
        'topics_at_current_complexity': 0,
        'bloom_level': 1,
        'topics_at_current_bloom': 0,
        'feed_status': 'not_started',
        'feed_started_at': None,
        'active_days_total': 0,
        'active_days_streak': 0,
        'longest_streak': 0,
        'last_active_date': None,
        'assessment_state': None,
        'assessment_date': None,
        'stats_reset_date': None,
        'trial_started_at': None,
        'onboarding_completed': False,
        'notify_template_updates': False,
        'retry_exhausted_date': None,
    }


def _get_default_intern(chat_id: int) -> dict:
    """Получить дефолтные значения для нового пользователя"""
    result = {
        'chat_id': chat_id,
        'user_id': None,
        'name': '',
        'occupation': '',
        'role': '',
        'domain': '',
        'interests': [],
        'motivation': '',
        'experience_level': '',
        'difficulty_preference': '',
        'learning_style': '',
        'study_duration': 15,
        'current_problems': '',
        'desires': '',
        'goals': '',
        'dt_connected_at': None,
        'dt_user_id': None,
        'created_at': None,
        'tg_username': None,
        'language': 'ru',
    }
    result.update(_get_default_state())
    return result


async def is_onboarded(intern: dict) -> bool:
    """Check if user completed onboarding, auto-heal if active but flag is False."""
    if not intern:
        logger.debug(f"[is_onboarded] intern is None/empty")
        return False
    chat_id = intern.get('chat_id')
    onb = intern.get('onboarding_completed')
    if onb:
        logger.debug(f"[is_onboarded] {chat_id} already onboarded")
        return True
    # Auto-heal: user has active marathon/feed → clearly onboarded
    marathon_status = intern.get('marathon_status', 'not_started')
    feed_status = intern.get('feed_status', 'not_started')
    if marathon_status != 'not_started' or feed_status != 'not_started':
        logger.info(f"[is_onboarded] auto-heal {chat_id}: marathon={marathon_status}, feed={feed_status}")
        await update_intern(chat_id, onboarding_completed=True)
        logger.info(f"[auto-heal] onboarding_completed set for chat_id={chat_id}")
        return True
    # Auto-heal: user has active marathon_progress (WP-330 newcomer marathon) → onboarded
    try:
        from db.queries.marathon_newcomer import get_or_create_progress
        progress = await get_or_create_progress(chat_id)
        if progress.get('status') not in (None, 'registered'):
            logger.info(f"[is_onboarded] auto-heal {chat_id}: marathon_progress status={progress.get('status')}")
            await update_intern(chat_id, marathon_status=progress.get('status', 'active'), onboarding_completed=True)
            return True
    except Exception as e:
        logger.debug(f"[is_onboarded] marathon_progress check failed: {e}")
    logger.debug(f"[is_onboarded] {chat_id} NOT onboarded (flag=False, both statuses=not_started)")
    return False


async def update_intern(chat_id: int, **kwargs):
    """Обновить данные пользователя (роутинг: profile → users, state → user_state)."""
    if not kwargs:
        return

    # Normalize: resolve aliases, serialize JSON, zero-pad schedule times
    columns = {}
    for key, value in kwargs.items():
        # JSON-поля
        if key in ['interests', 'completed_topics', 'current_context']:
            value = json.dumps(value) if not isinstance(value, str) else value

        # Zero-pad schedule times: "7:30" → "07:30"
        if key in ('schedule_time', 'feed_schedule_time', 'schedule_time_2') and value:
            value = value.zfill(5) if isinstance(value, str) and len(value) == 4 else value

        # Синхронизация bloom <-> complexity (aliases → canonical column)
        if key in ('bloom_level', 'complexity_level'):
            columns['complexity_level'] = value
            continue
        if key in ('topics_at_current_complexity', 'topics_at_current_bloom'):
            columns['topics_at_current_complexity'] = value
            continue

        columns[key] = value

    if not columns:
        return

    # Security: reject unknown column names to prevent SQL injection
    unknown = set(columns.keys()) - ALL_KNOWN_FIELDS
    if unknown:
        logger.error(f"[update_intern] Rejected unknown fields: {unknown} for chat_id={chat_id}")
        columns = {k: v for k, v in columns.items() if k in ALL_KNOWN_FIELDS}
        if not columns:
            return

    # Split into profile and state updates
    profile_updates = {k: v for k, v in columns.items() if k in PROFILE_FIELDS}
    state_updates = {k: v for k, v in columns.items() if k in STATE_FIELDS}

    pool = await get_pool()
    async with pool.acquire() as conn:
        if profile_updates:
            set_parts = []
            params = [chat_id]  # $1 = telegram_id
            for i, (col, val) in enumerate(profile_updates.items(), start=2):
                set_parts.append(f"{col} = ${i}")
                params.append(val)
            columns = [sp.split(" = ", 1)[0] for sp in set_parts]
            query = _update_sql(
                'public.users', columns, 'telegram_id = $1',
                extra_set=["updated_at = (NOW() AT TIME ZONE 'utc')"],
            )
            await conn.execute(query, *params)

        if state_updates:
            set_parts = []
            params = [chat_id]  # $1 = chat_id
            for i, (col, val) in enumerate(state_updates.items(), start=2):
                set_parts.append(f"{col} = ${i}")
                params.append(val)
            columns = [sp.split(" = ", 1)[0] for sp in set_parts]
            query = _update_sql(
                'development.user_state', columns, 'chat_id = $1',
                extra_set=["updated_at = (NOW() AT TIME ZONE 'utc')"],
            )
            await conn.execute(query, *params)

    # Инкрементальный sync в ЦД (fire-and-forget)
    try:
        from clients.gateway_mcp import gateway_mcp
        if gateway_mcp.is_connected(chat_id):
            mapped = {k: v for k, v in kwargs.items() if k in gateway_mcp.PROFILE_DT_MAPPING}
            if mapped:
                asyncio.create_task(gateway_mcp.sync_fields(chat_id, mapped))
    except Exception:
        pass  # DT sync — best effort

    # WP-268 Phase 2 dual-write: профиль/состояние обновлено
    # Audit fix (Phase 2): (1) PII — telegram_id заменён на account_id (ory_id) через
    # resolve_ory_id_from_chat; если ory не привязан — ключ омитится (PII-инвариант).
    # (2) external_id — стабильный hash от (chat_id + sorted affected fields), а не
    # epoch_ns; retry того же UPDATE даст тот же external_id ⇒ идемпотентность gateway.
    try:
        now = datetime.utcnow()
        affected_fields = sorted(columns.keys())
        ory_id = await resolve_ory_id_from_chat(chat_id)
        fields_hash = hashlib.sha256(
            f"{chat_id}:{','.join(affected_fields)}".encode()
        ).hexdigest()[:12]
        asyncio.create_task(post_event(
            source="aist-bot",
            external_id=f"user-updated-{chat_id}-{fields_hash}",
            event_type="user_updated",
            schema_version="v1",
            occurred_at=now,
            account_id=ory_id,  # None если T0 — gateway допускает
            payload={
                # PII-инвариант: telegram_id НЕ передаётся (см. CLAUDE.md distinctions PII).
                "fields_updated": affected_fields,
                "fields_count": len(affected_fields),
            },
        ))
    except Exception as exc:
        logger.warning(f"[dual-write] user_updated fire failed: {exc}")


async def update_tg_username(chat_id: int, username: str) -> None:
    """Обновить tg_username если изменился."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE public.users SET tg_username = $1 WHERE telegram_id = $2 AND tg_username IS DISTINCT FROM $1",
            username, chat_id,
        )

    # WP-268 Phase 2 dual-write: tg_username синхронизирован
    # Только если действительно был апдейт (UPDATE 1+, не UPDATE 0)
    # Audit fix (Phase 2): убран telegram_id из payload (PII), epoch_ns заменён
    # на стабильный hash от (chat_id, username) — retry с тем же значением
    # username идемпотентен.
    if result and result != "UPDATE 0":
        now = datetime.utcnow()
        ory_id = await resolve_ory_id_from_chat(chat_id)
        username_hash = hashlib.sha256(
            f"{chat_id}:{username or ''}".encode()
        ).hexdigest()[:12]
        asyncio.create_task(post_event(
            source="aist-bot",
            external_id=f"tg-synced-{chat_id}-{username_hash}",
            event_type="user_tg_synced",
            schema_version="v1",
            occurred_at=now,
            account_id=ory_id,
            payload={
                # PII-инвариант: telegram_id НЕ передаётся.
                # username — не PII в TG-смысле (публичный handle), но для безопасности кладём только наличие
                "username_present": bool(username),
            },
        ))


async def mark_bot_blocked(chat_id: int) -> None:
    """Пометить пользователя как заблокировавшего бота."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE development.user_state
               SET bot_blocked = TRUE,
                   bot_blocked_at = (NOW() AT TIME ZONE 'utc'),
                   bot_recheck_at = (NOW() AT TIME ZONE 'utc') + interval '1 day'
               WHERE chat_id = $1 AND bot_blocked IS NOT TRUE""",
            chat_id,
        )
    logger.info(f"[BlockedUser] Marked {chat_id} as bot_blocked")


async def clear_bot_blocked(chat_id: int) -> None:
    """Снять пометку bot_blocked (пользователь снова пишет боту)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE development.user_state
               SET bot_blocked = FALSE, bot_blocked_at = NULL, bot_recheck_at = NULL
               WHERE chat_id = $1 AND bot_blocked = TRUE""",
            chat_id,
        )
    if result and result != "UPDATE 0":
        logger.info(f"[BlockedUser] Cleared bot_blocked for {chat_id}")


async def get_users_to_recheck() -> list[int]:
    """Вернуть список chat_id пользователей, у которых подошло время recheck."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT chat_id FROM development.user_state
               WHERE bot_blocked = TRUE
                 AND bot_recheck_at IS NOT NULL
                 AND bot_recheck_at <= (NOW() AT TIME ZONE 'utc')"""
        )
    return [r["chat_id"] for r in rows]


async def update_user_state(chat_id: int, state_name: str) -> None:
    """Обновить текущее состояние пользователя (для State Machine)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE development.user_state SET current_state = $1, updated_at = (NOW() AT TIME ZONE 'utc') WHERE chat_id = $2",
            state_name, chat_id
        )
    logger.debug(f"[SM] User {chat_id} state updated to: {state_name}")


def derive_mode(marathon_status: str, feed_status: str) -> str:
    """Вычислить эффективный режим из независимых статусов."""
    m_active = marathon_status in ('active', 'completed')
    f_active = feed_status == 'active'
    if m_active and f_active:
        return 'both'
    elif f_active:
        return 'feed'
    return 'marathon'


async def get_marathon_users_at_time(hour: int, minute: int) -> list:
    """Получить chat_id пользователей марафона, запланированных на указанное время."""
    pool = await get_pool()
    time_str = f"{hour:02d}:{minute:02d}"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''SELECT chat_id FROM development.user_state
               WHERE schedule_time = $1
                 AND marathon_status = 'active'
                 AND onboarding_completed = TRUE''',
            time_str
        )

    if not rows:
        return []

    chat_ids = [row['chat_id'] for row in rows]
    learning_pool = await get_learning_pool()
    async with learning_pool.acquire() as l_conn:
        progress_rows = await l_conn.fetch(
            'SELECT user_id FROM learning.marathon_progress WHERE user_id = ANY($1::bigint[])',
            chat_ids
        )
    completed_ids = {row['user_id'] for row in progress_rows}
    return [cid for cid in chat_ids if cid not in completed_ids]


async def get_all_scheduled_interns(hour: int, minute: int) -> List[tuple]:
    """Получить пользователей для отправки по расписанию."""
    pool = await get_pool()
    time_str = f"{hour:02d}:{minute:02d}"
    async with pool.acquire() as conn:
        # Марафон: schedule_time совпадает, марафон активен
        marathon_rows = await conn.fetch(
            '''SELECT chat_id FROM development.user_state
               WHERE schedule_time = $1
                 AND marathon_status = 'active'
                 AND onboarding_completed = TRUE
                 AND bot_blocked IS NOT TRUE''',
            time_str
        )
        raw_marathon_ids = {row['chat_id'] for row in marathon_rows}

        # Лента: feed_schedule_time совпадает, лента активна
        feed_rows = await conn.fetch(
            '''SELECT chat_id FROM development.user_state
               WHERE feed_schedule_time = $1
                 AND feed_status = 'active'
                 AND onboarding_completed = TRUE
                 AND bot_blocked IS NOT TRUE''',
            time_str
        )
        feed_ids = {row['chat_id'] for row in feed_rows}

        # Fallback: feed_schedule_time не задан → используем schedule_time
        fallback_rows = await conn.fetch(
            '''SELECT chat_id FROM development.user_state
               WHERE schedule_time = $1
                 AND feed_schedule_time IS NULL
                 AND feed_status = 'active'
                 AND onboarding_completed = TRUE
                 AND bot_blocked IS NOT TRUE''',
            time_str
        )
        feed_ids = feed_ids | {row['chat_id'] for row in fallback_rows}

    # Фильтр марафонцев по прогрессу (app-side join, Neon learning DB)
    if raw_marathon_ids:
        learning_pool = await get_learning_pool()
        async with learning_pool.acquire() as l_conn:
            progress_rows = await l_conn.fetch(
                'SELECT user_id FROM learning.marathon_progress WHERE user_id = ANY($1::bigint[])',
                list(raw_marathon_ids)
            )
        completed_ids = {row['user_id'] for row in progress_rows}
        marathon_ids = raw_marathon_ids - completed_ids
    else:
        marathon_ids = raw_marathon_ids

    # Объединяем
    result = []
    for cid in marathon_ids | feed_ids:
        if cid in marathon_ids and cid in feed_ids:
            result.append((cid, 'both'))
        elif cid in feed_ids:
            result.append((cid, 'feed'))
        else:
            result.append((cid, 'marathon'))
    return result


# --- Slot management (auto-stagger) ---

MAX_USERS_PER_SLOT = 50


async def get_slot_load(target_time: str, window_minutes: int = 5) -> dict[str, int]:
    """Подсчитать количество пользователей на каждом минутном слоте."""
    h, m = map(int, target_time.split(":"))
    slots = []
    for delta in range(-window_minutes, window_minutes + 1):
        total_min = h * 60 + m + delta
        if total_min < 0:
            total_min += 24 * 60
        elif total_min >= 24 * 60:
            total_min -= 24 * 60
        sh, sm = divmod(total_min, 60)
        slots.append(f"{sh:02d}:{sm:02d}")

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''SELECT schedule_time, COUNT(*) as cnt
               FROM development.user_state
               WHERE schedule_time = ANY($1)
                 AND onboarding_completed = TRUE
               GROUP BY schedule_time''',
            slots
        )
    counts = {s: 0 for s in slots}
    for row in rows:
        counts[row['schedule_time']] = row['cnt']
    return counts


async def find_best_slot(target_time: str) -> tuple[str, bool]:
    """Найти оптимальный слот для пользователя."""
    counts = await get_slot_load(target_time)
    target_count = counts.get(target_time, 0)

    if target_count < MAX_USERS_PER_SLOT:
        return target_time, False

    h, m = map(int, target_time.split(":"))
    target_total = h * 60 + m

    def sort_key(slot: str) -> tuple[int, int]:
        sh, sm = map(int, slot.split(":"))
        dist = abs((sh * 60 + sm) - target_total)
        if dist > 720:
            dist = 1440 - dist
        return (dist, counts[slot])

    candidates = sorted(counts.keys(), key=sort_key)
    for slot in candidates:
        if counts[slot] < MAX_USERS_PER_SLOT:
            return slot, slot != target_time

    best = min(counts.keys(), key=lambda s: counts[s])
    return best, best != target_time


def get_topics_today(intern: dict) -> int:
    """Получить количество тем, пройденных сегодня"""
    today = moscow_today()
    last_date = intern.get('last_topic_date')

    if last_date and last_date == today:
        return intern.get('topics_today', 0)
    return 0


async def get_template_update_subscribers() -> List[int]:
    """Получить chat_id пользователей, подписанных на обновления шаблона IWE."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''SELECT chat_id FROM development.user_state
               WHERE notify_template_updates = TRUE
                 AND bot_blocked IS NOT TRUE'''
        )
    return [row['chat_id'] for row in rows]
