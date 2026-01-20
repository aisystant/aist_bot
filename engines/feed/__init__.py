"""
Движок режима Лента.

Бесконечный режим расширения кругозора.

Содержит:
- engine.py: FeedEngine - основная логика
- handlers.py: обработчики Telegram + FSM
- planner.py: планирование недельных тем
"""

from .engine import FeedEngine
from .handlers import feed_router, FeedStates
from .planner import suggest_weekly_topics, generate_topic_content

__all__ = [
    'FeedEngine',
    'feed_router',
    'FeedStates',
    'suggest_weekly_topics',
    'generate_topic_content',
]
