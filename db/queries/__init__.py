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

from .assessment import (
    save_assessment,
    get_latest_assessment,
    get_assessment_history,
)

from .github import (
    get_github_connection,
    save_github_connection,
    update_github_repo,
    update_github_notes_path,
    update_github_strategy_repo,
    delete_github_connection,
)

from .profile import (
    get_knowledge_profile,
)

from .marathon import (
    save_marathon_content,
    get_marathon_content,
    mark_content_delivered,
    cleanup_expired_content,
)

from .subscription import (
    get_active_subscription,
    is_subscribed,
    save_subscription,
    cancel_subscription,
    get_subscription_history,
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

    # assessment
    'save_assessment',
    'get_latest_assessment',
    'get_assessment_history',

    # github
    'get_github_connection',
    'save_github_connection',
    'update_github_repo',
    'update_github_notes_path',
    'update_github_strategy_repo',
    'delete_github_connection',

    # profile
    'get_knowledge_profile',

    # marathon content
    'save_marathon_content',
    'get_marathon_content',
    'mark_content_delivered',
    'cleanup_expired_content',

    # subscription
    'get_active_subscription',
    'is_subscribed',
    'save_subscription',
    'cancel_subscription',
    'get_subscription_history',
]
