"""
Port-интерфейс над AI-генерацией заданий Тренировки (WP-262 В1, Ф1-seam).

TrainingPlannerPort — канало-независимый интерфейс двух planner-функций
(generate_assignment_text, generate_child_assignment_text). Единственная
реализация в этой фазе — LocalTrainingPlanner, вызывающая текущий planner.py
без изменения поведения.

Платформенная/HTTP-реализация (вызов через прокси/платформу) — Ф2/Ф4,
её здесь нет: только порт + локальная реализация.

Принцип (DP.D.042, по образцу engines/tailor/port.py): planner.py = логика
генерации, port.py = интерфейс, локальная реализация = адаптер к planner.py.
"""

from abc import ABC, abstractmethod
from typing import Optional

from config import get_logger

from . import planner

logger = get_logger(__name__)


class TrainingPlannerPort(ABC):
    """Порт над AI-генерацией заданий Тренировки.

    Абстрагирует вызывающий код (engines/training/engine.py) от конкретной
    реализации planner-функций — локальной (сейчас) или платформенной (Ф2/Ф4).
    Контракт (сигнатуры, результаты, ошибки) идентичен planner.py.
    """

    @abstractmethod
    async def generate_assignment_text(
        self,
        cell_data: dict,
        cognitive_level: str,
        intern: Optional[dict],
        principle_name: str,
        depth: int,
    ) -> str:
        """Тот же контракт, что planner.generate_assignment_text()."""

    @abstractmethod
    async def generate_child_assignment_text(
        self,
        cell_data: dict,
        cognitive_level: str,
        child_name: str,
        principle_name: str,
        depth: int,
        lang: str = 'ru',
        account_id: Optional[str] = None,
    ) -> str:
        """Тот же контракт, что planner.generate_child_assignment_text()."""


class LocalTrainingPlanner(TrainingPlannerPort):
    """Локальная реализация порта — вызывает planner.py напрямую.

    Единственная реализация в Ф1. Поведение (сигнатуры, результаты,
    ошибки) не меняется — это тонкая обёртка для strangler-seam.
    """

    async def generate_assignment_text(
        self,
        cell_data: dict,
        cognitive_level: str,
        intern: Optional[dict],
        principle_name: str,
        depth: int,
    ) -> str:
        logger.info("training planner via seam: generate_assignment_text")
        return await planner.generate_assignment_text(
            cell_data, cognitive_level, intern, principle_name, depth,
        )

    async def generate_child_assignment_text(
        self,
        cell_data: dict,
        cognitive_level: str,
        child_name: str,
        principle_name: str,
        depth: int,
        lang: str = 'ru',
        account_id: Optional[str] = None,
    ) -> str:
        logger.info("training planner via seam: generate_child_assignment_text")
        return await planner.generate_child_assignment_text(
            cell_data, cognitive_level, child_name, principle_name, depth,
            lang=lang, account_id=account_id,
        )


# Единственный экземпляр порта на Ф1 (локальная реализация).
# engine.py импортирует именно этот объект — вызывающему коду не нужно
# знать о существовании LocalTrainingPlanner.
default_training_planner_port: TrainingPlannerPort = LocalTrainingPlanner()
