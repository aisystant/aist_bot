"""
Pydantic-схемы контракта TailorEngine service (WP-262 В1.1, DP.SC.NNN).

Shared между ботом (client) и tailor-service (server).
"""

from typing import Optional

from pydantic import BaseModel


class LessonRequest(BaseModel):
    """Запрос на сборку занятия."""

    user_id: int
    mode: str = "worldview"
    domain_hint: str = ""


class LessonPacket(BaseModel):
    """Собранное занятие: structured lesson + generated text.

    lesson: dict из TailorEngine.assemble()
    generated: dict из planner.generate_lesson_text()
    error: текст ошибки если сервис не смог собрать занятие
    """

    lesson: dict
    generated: dict
    error: Optional[str] = None
