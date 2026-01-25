"""
Стейты мастерской Марафон.

Содержит:
- day.py: показ урока текущего дня
- question.py: вопрос на понимание урока
- bonus.py: бонусный вопрос повышенной сложности
- task.py: практическое задание

Flow:
  day → question → [bonus*] → task → day (следующий)

  * bonus предлагается только на уровнях 2 и 3 (bloom_level >= 2)
"""

from .day import MarathonDayState
from .question import MarathonQuestionState
from .bonus import MarathonBonusState
from .task import MarathonTaskState

__all__ = [
    'MarathonDayState',
    'MarathonQuestionState',
    'MarathonBonusState',
    'MarathonTaskState',
]
