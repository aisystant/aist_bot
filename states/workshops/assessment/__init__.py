"""
Мастерская: Оценка систематичности.

Стейты:
- AssessmentFlowState: intro → вопросы → self-check → open question
- AssessmentResultState: показ результата + сохранение в БД
"""

from .flow import AssessmentFlowState
from .result import AssessmentResultState

__all__ = [
    'AssessmentFlowState',
    'AssessmentResultState',
]
