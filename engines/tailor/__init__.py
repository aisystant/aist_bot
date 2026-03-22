"""
Портной (Tailor) — сборка персональных занятий (WP-149, SC.020).

Архитектура (DP.D.042):
  engine.py     — канало-независимая логика (7 шагов SOP.001)
  cells.py      — загрузчик ячеек контента
  planner.py    — генерация текста через Claude (канало-независимый)
  evaluator.py  — оценка ответа по can-do (канало-независимый)
  port.py       — абстракция канала доставки (TailorPort)
  bot_adapter.py — реализация для Telegram (BotTailorAdapter)
  delivery.py   — интеграция со scheduler
"""

from .engine import TailorEngine
from .port import TailorPort

__all__ = ['TailorEngine', 'TailorPort']
