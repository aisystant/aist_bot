"""
Legacy хендлеры — используются при USE_STATE_MACHINE=false.

При включённом SM эти хендлеры делают bypass в SM.
"""

from .learning import legacy_learning_router

__all__ = ['legacy_learning_router']
