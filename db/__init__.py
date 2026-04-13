"""
Модуль работы с базой данных.

Содержит:
- connection.py: пул соединений PostgreSQL
- models.py: описание таблиц (SQL schemas)
- queries/: функции для работы с данными
    - users.py: get_intern, update_intern
    - answers.py: save_answer, get_answers
    - feed.py: feed_weeks, feed_sessions
    - activity.py: activity_log, систематичность
    - qa.py: qa_history
"""

from .connection import (
    get_pool,
    close_pool,
    acquire,
    init_db,
    db_pool,
)

from .models import create_tables
from .rls import with_user_context

__all__ = [
    'get_pool',
    'close_pool',
    'acquire',
    'init_db',
    'db_pool',
    'create_tables',
    'with_user_context',
]
