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
from states.common import StartState, ErrorState, ModeSelectState, SettingsState, ConsultationState

# Марафон (полностью реализовано)
from states.workshops.marathon import (
    MarathonLessonState,
    MarathonQuestionState,
    MarathonBonusState,
    MarathonTaskState,
)

# Оценка систематичности (реализовано)
from states.workshops.assessment import (
    AssessmentFlowState,
    AssessmentResultState,
)

# Лента (реализовано)
from states.feed import FeedTopicsState, FeedDigestState

# Utilities (частично реализовано)
from states.utilities import ProgressState
# TODO: Неделя 8 — раскомментировать после создания
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
        # Common стейты
        StartState(*args),
        ErrorState(*args),
        ModeSelectState(*args),
        SettingsState(*args),      # Настройки пользователя
        ConsultationState(*args),  # Глобальный стейт консультации

        # Marathon стейты (полностью реализовано)
        MarathonLessonState(*args),
        MarathonQuestionState(*args),
        MarathonBonusState(*args),
        MarathonTaskState(*args),

        # Assessment стейты (оценка систематичности)
        AssessmentFlowState(*args),
        AssessmentResultState(*args),

        # Feed стейты (Лента)
        FeedTopicsState(*args),
        FeedDigestState(*args),

        # Utility стейты
        ProgressState(*args),
        # TODO: Неделя 8 — раскомментировать после создания
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
        "common.consultation",  # Консультация

        # Marathon
        "workshop.marathon.lesson",
        "workshop.marathon.question",
        "workshop.marathon.bonus",
        "workshop.marathon.task",

        # Assessment (Оценка)
        "workshop.assessment.flow",
        "workshop.assessment.result",

        # Feed (Лента)
        "feed.topics",
        "feed.digest",

        # Utilities
        "utility.progress",
        "utility.notes",
        "utility.export",
    ]
