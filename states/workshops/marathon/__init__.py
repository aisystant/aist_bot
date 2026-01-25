"""
Стейты мастерской Марафон.

Содержит:
- lesson.py: показ урока
- question.py: вопрос на понимание урока
- bonus.py: бонусный вопрос повышенной сложности
- task.py: практическое задание

Flow:
  lesson → question → [bonus*] → task → lesson (следующий)

  * bonus предлагается только на уровнях 2 и 3 (bloom_level >= 2)
"""

from .lesson import MarathonLessonState
from .question import MarathonQuestionState
from .bonus import MarathonBonusState
from .task import MarathonTaskState

__all__ = [
    'MarathonLessonState',
    'MarathonQuestionState',
    'MarathonBonusState',
    'MarathonTaskState',
]
