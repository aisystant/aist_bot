"""SM-стейты для режима Тренировка."""

from .dashboard import TrainingDashboardState
from .assignment import TrainingAssignmentState
from .setup import TrainingSetupState
from .child_assignment import TrainingChildAssignmentState

__all__ = [
    'TrainingDashboardState',
    'TrainingAssignmentState',
    'TrainingSetupState',
    'TrainingChildAssignmentState',
]
