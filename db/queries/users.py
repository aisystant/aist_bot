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
        
        # Марафон
        'marathon_status': safe_get('marathon_status', 'not_started'),
        'marathon_start_date': safe_get('marathon_start_date', None),
        'marathon_paused_at': safe_get('marathon_paused_at', None),
        'current_topic_index': safe_get('current_topic_index', 0),
        'completed_topics': safe_json('completed_topics', []),
        'topics_today': safe_get('topics_today', 0),
        'last_topic_date': safe_get('last_topic_date', None),
        
        # Сложность
        'complexity_level': safe_get('complexity_level', 1) or safe_get('bloom_level', 1) or 1,
        'topics_at_current_complexity': safe_get('topics_at_current_complexity', 0) or safe_get('topics_at_current_bloom', 0) or 0,
        # Для обратной совместимости
        'bloom_level': safe_get('complexity_level', 1) or safe_get('bloom_level', 1) or 1,
        'topics_at_current_bloom': safe_get('topics_at_current_complexity', 0) or safe_get('topics_at_current_bloom', 0) or 0,
        
        # Лента
        'feed_status': safe_get('feed_status', 'not_started'),
        'feed_started_at': safe_get('feed_started_at', None),
        
        # Систематичность
        'active_days_total': safe_get('active_days_total', 0),
        'active_days_streak': safe_get('active_days_streak', 0),
        'longest_streak': safe_get('longest_streak', 0),
        'last_active_date': safe_get('last_active_date', None),
        
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
            
            await conn.execute(
                f'UPDATE interns SET {key} = $1, updated_at = NOW() WHERE chat_id = $2',
                value, chat_id
            )


async def get_all_scheduled_interns(hour: int, minute: int) -> List[int]:
    """Получить всех пользователей с заданным временем обучения"""
    pool = await get_pool()
    time_str = f"{hour:02d}:{minute:02d}"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT chat_id FROM interns WHERE schedule_time = $1 AND onboarding_completed = TRUE',
            time_str
        )
        return [row['chat_id'] for row in rows]


def get_topics_today(intern: dict) -> int:
    """Получить количество тем, пройденных сегодня"""
    today = moscow_today()
    last_date = intern.get('last_topic_date')

    if last_date and last_date == today:
        return intern.get('topics_today', 0)
    return 0
