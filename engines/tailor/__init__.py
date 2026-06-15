"""
Портной (Tailor) — сборка персональных занятий (WP-149, SC.020).

Архитектура (DP.D.042):
  engine.py     — канало-независимая логика (7 шагов SOP.001 v3)
  catalogs.py   — загрузчик каталогов мемов (CAT.001) и практик (CAT.003)
  cells.py      — загрузчик ячеек контента (legacy, для обратной совместимости)
  planner.py    — генерация текста через Claude (канало-независимый)
  evaluator.py  — оценка ответа по can-do (канало-независимый)
  port.py       — абстракция канала доставки (TailorPort)
"""

from .engine import TailorEngine
from .port import TailorPort

__all__ = ['TailorEngine', 'TailorPort']
