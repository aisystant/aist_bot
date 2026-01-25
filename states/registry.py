"""
Реестр стейтов State Machine.

Содержит функцию регистрации всех стейтов в StateMachine.
При добавлении нового стейта нужно:
1. Импортировать его здесь
2. Добавить в список states в функции register_all_states
"""

import logging

from aiogram import Bot

from core.machine import StateMachine
from i18n import I18n

# Импортируем стейты
from states.common import StartState, ErrorState, ModeSelectState

# Марафон (полностью реализовано)
from states.workshops.marathon import (
    MarathonLessonState,
    MarathonQuestionState,
    MarathonBonusState,
    MarathonTaskState,
)

# TODO: Неделя 3 — раскомментировать после создания
# from states.consultants import MainConsultantState

# TODO: Неделя 4 — раскомментировать после создания
# from states.consultants import FeedTopicsState, FeedDigestState

# TODO: Неделя 6 — раскомментировать после создания
# from states.utilities import NotesState, ExportState

logger = logging.getLogger(__name__)


def register_all_states(
    machine: StateMachine,
    bot: Bot,
    db,
    llm,
    i18n: I18n
) -> None:
    """
    Регистрирует все стейты в StateMachine.

    Args:
        machine: Экземпляр StateMachine
        bot: Telegram Bot instance
        db: Database repository
        llm: LLM client (Claude)
        i18n: Локализация
    """
    # Общие аргументы для всех стейтов
    args = (bot, db, llm, i18n)

    states = [
        # Common стейты (Неделя 1)
        StartState(*args),
        ErrorState(*args),
        ModeSelectState(*args),

        # Marathon стейты (полностью реализовано)
        MarathonLessonState(*args),
        MarathonQuestionState(*args),
        MarathonBonusState(*args),
        MarathonTaskState(*args),

        # TODO: Consultant стейты (Неделя 3)
        # MainConsultantState(*args),

        # TODO: Feed стейты (Неделя 4)
        # FeedTopicsState(*args),
        # FeedDigestState(*args),

        # TODO: Utility стейты (Неделя 6)
        # NotesState(*args),
        # ExportState(*args),
    ]

    machine.register_all(states)
    logger.info(f"Registered {len(states)} states")


def get_available_states() -> list[str]:
    """
    Возвращает список всех доступных стейтов.

    Используется для документации и отладки.
    """
    return [
        # Common
        "common.start",
        "common.error",
        "common.mode_select",
        "common.settings",

        # Marathon (Неделя 2)
        "workshop.marathon.lesson",
        "workshop.marathon.question",
        "workshop.marathon.bonus",
        "workshop.marathon.task",

        # Consultant (Неделя 3-4)
        "consultant.main",
        "consultant.feed_topics",
        "consultant.feed_digest",
        "consultant.assessment_test",
        "consultant.assessment_result",

        # Utilities (Неделя 6)
        "utility.notes",
        "utility.export",
    ]
