"""SM-стейты для режима Тренировка."""

from .dashboard import TrainingDashboardState
from .assignment import TrainingAssignmentState
from .setup import TrainingSetupState

__all__ = [
    'TrainingDashboardState',
    'TrainingAssignmentState',
    'TrainingSetupState',
]
