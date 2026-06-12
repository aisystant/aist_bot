"""
Pydantic-схемы контракта TailorEngine service (WP-262 В1.1, DP.SC.NNN).

Shared между ботом (client) и tailor-service (server).
"""

from typing import Optional

from pydantic import BaseModel


class LessonRequest(BaseModel):
    """Запрос на сборку занятия.

    user_profile: профиль из ЦД (student_stage, it_level, assessment_state, energy, ...)
    learning_history: [{element_id, element_type, passed, area, depth, ...}]
    last_area: область вчерашнего занятия (для ротации, 1-5)
    """

    user_id: int
    mode: str = "worldview"
    domain_hint: str = ""
    user_profile: Optional[dict] = None
    learning_history: Optional[list] = None
    last_area: Optional[int] = None


class LessonPacket(BaseModel):
    """Собранное занятие: structured lesson + generated text.

    lesson: dict из TailorEngine.assemble()
    generated: dict из planner.generate_lesson_text()
    error: текст ошибки если сервис не смог собрать занятие
    """

    lesson: dict
    generated: dict
    error: Optional[str] = None
