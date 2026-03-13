"""
Запросы для работы с пользователями.

ЦД-native (WP-82 Phase 3):
  public.users — identity + profile
  development.user_state — bot state

get_intern() и update_intern() — адаптеры, возвращающие тот же dict-формат.
"""

import asyncio
import json
from datetime import datetime, date, timedelta
from typing import Optional, List

from config import get_logger, MOSCOW_TZ
from db.connection import get_pool

logger = get_logger(__name__)

# ─── Field routing: какие поля в какой таблице ───

PROFILE_FIELDS = frozenset({
    'name', 'occupation', 'role', 'domain', 'interests', 'motivation', 'goals',
    'language', 'experience_level', 'difficulty_preference', 'learning_style',
    'study_duration', 'current_problems', 'desires', 'tg_username',
    'aisystant_id', 'aisystant_linked_at', 'dt_connected_at', 'dt_user_id',
    'tier', 'email', 'timezone',
})

# All other fields → development.user_state


def moscow_now() -> datetime:
    """Получить текущее время по Москве"""
    return datetime.now(MOSCOW_TZ)


def moscow_today() -> date:
    """Получить текущую дату по Москве"""
    return moscow_now().date()


# ─── SQL для JOIN users + user_state ───

_SELECT_JOINED = '''
    SELECT
        u.id AS user_id, u.telegram_id AS chat_id,
        u.name, u.occupation, u.role, u.domain, u.interests,
        u.motivation, u.goals, u.language, u.experience_level,
        u.difficulty_preference, u.learning_style, u.study_duration,
        u.current_problems, u.desires, u.tg_username,
        u.aisystant_id, u.aisystant_linked_at, u.dt_connected_at,
        u.dt_user_id, u.tier, u.email, u.timezone, u.created_at,
        s.mode, s.current_context, s.current_state, s.topic_order,
        s.schedule_time, s.schedule_time_2, s.feed_schedule_time,
        s.marathon_status, s.marathon_start_date, s.marathon_paused_at,
        s.current_topic_index, s.completed_topics, s.topics_today,
        s.last_topic_date, s.complexity_level, s.topics_at_current_complexity,
        s.feed_status, s.feed_started_at,
        s.active_days_total, s.active_days_streak, s.longest_streak,
        s.last_active_date, s.bot_blocked, s.bot_blocked_at,
        s.trial_started_at, s.assessment_state, s.assessment_date,
        s.stats_reset_date, s.notify_template_updates,
        s.onboarding_completed,
        COALESCE(s.updated_at, u.updated_at) AS updated_at
    FROM public.users u
    LEFT JOIN development.user_state s ON s.user_id = u.id
    WHERE u.telegram_id = $1
'''


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
        val = safe_get(key, '[]')
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
        'created_at': safe_get('created_at', None),

        # Telegram
        'tg_username': safe_get('tg_username', None),

        # Статусы
        'onboarding_completed': safe_get('onboarding_completed', False),
        'language': safe_get('language', 'ru'),

        # IWE template update notifications (WP-90)
        'notify_template_updates': safe_get('notify_template_updates', False),
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
        'created_at': None,
        'tg_username': None,
        'language': 'ru',
    }
    result.update(_get_default_state())
    return result


async def is_onboarded(intern: dict) -> bool:
    """Check if user completed onboarding, auto-heal if active but flag is False."""
    if not intern:
        return False
    if intern.get('onboarding_completed'):
        return True
    # Auto-heal: user has active marathon/feed → clearly onboarded
    if (intern.get('marathon_status', 'not_started') != 'not_started'
            or intern.get('feed_status', 'not_started') != 'not_started'):
        await update_intern(intern['chat_id'], onboarding_completed=True)
        logger.info(f"[auto-heal] onboarding_completed set for chat_id={intern['chat_id']}")
        return True
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

    # Split into profile and state updates
    profile_updates = {k: v for k, v in columns.items() if k in PROFILE_FIELDS}
    state_updates = {k: v for k, v in columns.items() if k not in PROFILE_FIELDS}

    pool = await get_pool()
    async with pool.acquire() as conn:
        if profile_updates:
            set_parts = []
            params = [chat_id]  # $1 = telegram_id
            for i, (col, val) in enumerate(profile_updates.items(), start=2):
                set_parts.append(f"{col} = ${i}")
                params.append(val)
            set_parts.append("updated_at = (NOW() AT TIME ZONE 'utc')")
            query = f"UPDATE public.users SET {', '.join(set_parts)} WHERE telegram_id = $1"
            await conn.execute(query, *params)

        if state_updates:
            set_parts = []
            params = [chat_id]  # $1 = chat_id
            for i, (col, val) in enumerate(state_updates.items(), start=2):
                set_parts.append(f"{col} = ${i}")
                params.append(val)
            set_parts.append("updated_at = (NOW() AT TIME ZONE 'utc')")
            query = f"UPDATE development.user_state SET {', '.join(set_parts)} WHERE chat_id = $1"
            await conn.execute(query, *params)

    # Инкрементальный sync в ЦД (fire-and-forget)
    try:
        from clients.digital_twin import digital_twin
        if digital_twin.is_connected(chat_id):
            mapped = {k: v for k, v in kwargs.items() if k in digital_twin.PROFILE_DT_MAPPING}
            if mapped:
                asyncio.create_task(digital_twin.sync_fields(chat_id, mapped))
    except Exception:
        pass  # DT sync — best effort


async def update_tg_username(chat_id: int, username: str) -> None:
    """Обновить tg_username если изменился."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.users SET tg_username = $1 WHERE telegram_id = $2 AND tg_username IS DISTINCT FROM $1",
            username, chat_id,
        )


async def mark_bot_blocked(chat_id: int) -> None:
    """Пометить пользователя как заблокировавшего бота."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE development.user_state
               SET bot_blocked = TRUE, bot_blocked_at = (NOW() AT TIME ZONE 'utc')
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
               SET bot_blocked = FALSE, bot_blocked_at = NULL
               WHERE chat_id = $1 AND bot_blocked = TRUE""",
            chat_id,
        )
    if result and result != "UPDATE 0":
        logger.info(f"[BlockedUser] Cleared bot_blocked for {chat_id}")


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
    return [row['chat_id'] for row in rows]


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
        marathon_ids = {row['chat_id'] for row in marathon_rows}

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
