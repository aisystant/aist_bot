"""
Запросы для работы с пользователями (таблица interns).
"""

import json
from datetime import datetime, date, timedelta
from typing import Optional, List

from config import get_logger, MOSCOW_TZ
from db.connection import get_pool

logger = get_logger(__name__)


def moscow_now() -> datetime:
    """Получить текущее время по Москве"""
    return datetime.now(MOSCOW_TZ)


def moscow_today() -> date:
    """Получить текущую дату по Москве"""
    return moscow_now().date()


async def get_intern(chat_id: int) -> dict:
    """Получить профиль пользователя из БД"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM interns WHERE chat_id = $1', chat_id
        )
        
        if row:
            return _row_to_dict(row)
        else:
            # Создаём нового пользователя
            await conn.execute(
                'INSERT INTO interns (chat_id) VALUES ($1) ON CONFLICT DO NOTHING',
                chat_id
            )
            return _get_default_intern(chat_id)


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
        except:
            return default

    return {
        'chat_id': row['chat_id'],
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
        
        # Сложность (используем coalesce-логику, но с учётом 0 как валидного значения)
        'complexity_level': safe_get('complexity_level', None) if safe_get('complexity_level', None) is not None else (safe_get('bloom_level', 1) or 1),
        'topics_at_current_complexity': safe_get('topics_at_current_complexity', None) if safe_get('topics_at_current_complexity', None) is not None else (safe_get('topics_at_current_bloom', 0) or 0),
        # Для обратной совместимости (синхронизируем значения)
        'bloom_level': safe_get('complexity_level', None) if safe_get('complexity_level', None) is not None else (safe_get('bloom_level', 1) or 1),
        'topics_at_current_bloom': safe_get('topics_at_current_complexity', None) if safe_get('topics_at_current_complexity', None) is not None else (safe_get('topics_at_current_bloom', 0) or 0),
        
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

        # Подписка
        'trial_started_at': safe_get('trial_started_at', None),
        'created_at': safe_get('created_at', None),

        # Статусы
        'onboarding_completed': safe_get('onboarding_completed', False),
        'language': safe_get('language', 'ru'),
    }


def _get_default_intern(chat_id: int) -> dict:
    """Получить дефолтные значения для нового пользователя"""
    return {
        'chat_id': chat_id,
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
        'schedule_time': '09:00',
        'schedule_time_2': None,
        'topic_order': 'default',
        
        'mode': 'marathon',
        'current_context': {},

        # State Machine
        'current_state': None,

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
        'feed_schedule_time': None,

        'active_days_total': 0,
        'active_days_streak': 0,
        'longest_streak': 0,
        'last_active_date': None,

        'assessment_state': None,
        'assessment_date': None,

        'stats_reset_date': None,

        'trial_started_at': None,
        'created_at': None,

        'onboarding_completed': False,
        'language': 'ru',
    }


async def update_intern(chat_id: int, **kwargs):
    """Обновить данные пользователя"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        for key, value in kwargs.items():
            # JSON-поля
            if key in ['interests', 'completed_topics', 'current_context']:
                value = json.dumps(value) if not isinstance(value, str) else value
            
            # Синхронизация bloom <-> complexity
            if key == 'complexity_level':
                await conn.execute(
                    'UPDATE interns SET complexity_level = $1, bloom_level = $1, updated_at = NOW() WHERE chat_id = $2',
                    value, chat_id
                )
                continue
            if key == 'topics_at_current_complexity':
                await conn.execute(
                    'UPDATE interns SET topics_at_current_complexity = $1, topics_at_current_bloom = $1, updated_at = NOW() WHERE chat_id = $2',
                    value, chat_id
                )
                continue
            if key == 'topics_at_current_bloom':
                # Синхронизируем оба поля для обратной совместимости с legacy кодом
                await conn.execute(
                    'UPDATE interns SET topics_at_current_complexity = $1, topics_at_current_bloom = $1, updated_at = NOW() WHERE chat_id = $2',
                    value, chat_id
                )
                continue
            
            await conn.execute(
                f'UPDATE interns SET {key} = $1, updated_at = NOW() WHERE chat_id = $2',
                value, chat_id
            )


async def update_user_state(chat_id: int, state_name: str) -> None:
    """
    Обновить текущее состояние пользователя (для State Machine).

    Args:
        chat_id: ID чата пользователя
        state_name: Имя нового состояния (например, "common.start")
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            'UPDATE interns SET current_state = $1, updated_at = NOW() WHERE chat_id = $2',
            state_name, chat_id
        )
    logger.debug(f"[SM] User {chat_id} state updated to: {state_name}")


def derive_mode(marathon_status: str, feed_status: str) -> str:
    """Вычислить эффективный режим из независимых статусов.

    Returns 'both', 'feed' или 'marathon' (по умолчанию).
    """
    m_active = marathon_status in ('active', 'completed')
    f_active = feed_status == 'active'
    if m_active and f_active:
        return 'both'
    elif f_active:
        return 'feed'
    return 'marathon'


async def get_all_scheduled_interns(hour: int, minute: int) -> List[tuple]:
    """Получить пользователей для отправки по расписанию.

    Returns:
        list of (chat_id, send_type) где send_type = 'marathon' | 'feed' | 'both'
    """
    pool = await get_pool()
    time_str = f"{hour:02d}:{minute:02d}"
    async with pool.acquire() as conn:
        # Марафон: schedule_time совпадает, марафон активен
        marathon_rows = await conn.fetch(
            '''SELECT chat_id FROM interns
               WHERE schedule_time = $1
                 AND marathon_status = 'active'
                 AND onboarding_completed = TRUE''',
            time_str
        )
        marathon_ids = {row['chat_id'] for row in marathon_rows}

        # Лента: feed_schedule_time совпадает, лента активна
        feed_rows = await conn.fetch(
            '''SELECT chat_id FROM interns
               WHERE feed_schedule_time = $1
                 AND feed_status = 'active'
                 AND onboarding_completed = TRUE''',
            time_str
        )
        feed_ids = {row['chat_id'] for row in feed_rows}

        # Fallback: feed_schedule_time не задан → используем schedule_time
        fallback_rows = await conn.fetch(
            '''SELECT chat_id FROM interns
               WHERE schedule_time = $1
                 AND feed_schedule_time IS NULL
                 AND feed_status = 'active'
                 AND onboarding_completed = TRUE''',
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


def get_topics_today(intern: dict) -> int:
    """Получить количество тем, пройденных сегодня"""
    today = moscow_today()
    last_date = intern.get('last_topic_date')

    if last_date and last_date == today:
        return intern.get('topics_today', 0)
    return 0
