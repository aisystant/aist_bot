"""Фоновый режим Диагноста (WP-318 Ф9)."""
from .background import (
    cp_status,
    suggest_for_attestator,
    suggest_for_navigator,
    suggest_for_tailor,
)

__all__ = [
    "cp_status",
    "suggest_for_navigator",
    "suggest_for_tailor",
    "suggest_for_attestator",
]
