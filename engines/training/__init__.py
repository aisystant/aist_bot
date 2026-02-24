"""
Движок режима Тренировка (WP-55).

Тренировка принципов мышления (ZP, FPF) через задания с AI-оценкой.
BC: Training (skill practice) — отдельно от Feed (content consumption).
"""

from .engine import TrainingEngine
from .handlers import training_router, TrainingStates

__all__ = [
    'TrainingEngine',
    'training_router',
    'TrainingStates',
]
