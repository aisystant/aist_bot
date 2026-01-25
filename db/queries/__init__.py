"""
Функции для работы с базой данных.

Модули:
- users.py: работа с таблицей interns
- answers.py: работа с таблицей answers
- feed.py: работа с Лентой (feed_weeks, feed_sessions)
- activity.py: отслеживание активности и систематичности
- qa.py: история вопросов и ответов
"""

from .users import (
    get_intern,
    update_intern,
    update_user_state,
    get_all_scheduled_interns,
    get_topics_today,
    moscow_now,
    moscow_today,
)

from .answers import (
    save_answer,
    get_answers,
    get_weekly_work_products,
    get_answers_count_by_type,
)

from .activity import (
    record_active_day,
    get_activity_stats,
    get_activity_calendar,
)

from .feed import (
    create_feed_week,
    get_current_feed_week,
    update_feed_week,
    create_feed_session,
    update_feed_session,
    get_feed_session,
    get_feed_history,
)

from .qa import (
    save_qa,
    get_qa_history,
    get_qa_count,
)

__all__ = [
    # users
    'get_intern',
    'update_intern',
    'update_user_state',
    'get_all_scheduled_interns',
    'get_topics_today',
    'moscow_now',
    'moscow_today',

    # answers
    'save_answer',
    'get_answers',
    'get_weekly_work_products',
    'get_answers_count_by_type',

    # activity
    'record_active_day',
    'get_activity_stats',
    'get_activity_calendar',

    # feed
    'create_feed_week',
    'get_current_feed_week',
    'update_feed_week',
    'create_feed_session',
    'update_feed_session',
    'get_feed_session',
    'get_feed_history',

    # qa
    'save_qa',
    'get_qa_history',
    'get_qa_count',
]
