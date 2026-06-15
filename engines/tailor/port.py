"""
Абстракция канала доставки занятий Портного (WP-149, SC.020).

TailorPort — канало-независимый интерфейс.
Принцип (DP.D.042): engine.py = логика (канало-независимая),
port.py = интерфейс + callback-константы канала.
"""

from abc import ABC, abstractmethod
from typing import Optional

# Callback-prefixes для inline-кнопок занятия
CB_TAILOR_ANSWER = "tailor_answer"
CB_TAILOR_SKIP = "tailor_skip"


class LessonDeliveryResult:
    """Результат доставки занятия."""

    __slots__ = ('delivered', 'message_id', 'error')

    def __init__(
        self,
        delivered: bool,
        message_id: Optional[int] = None,
        error: Optional[str] = None,
    ):
        self.delivered = delivered
        self.message_id = message_id
        self.error = error


class LessonResponse:
    """Ответ пользователя на занятие."""

    __slots__ = ('text', 'skipped', 'topic_id', 'bloom_depth', 'direction')

    def __init__(
        self,
        text: str = '',
        skipped: bool = False,
        topic_id: str = '',
        bloom_depth: int = 1,
        direction: int = 1,
    ):
        self.text = text
        self.skipped = skipped
        self.topic_id = topic_id
        self.bloom_depth = bloom_depth
        self.direction = direction


class TailorPort(ABC):
    """Канало-независимый интерфейс доставки занятий Портного.

    Каждый канал (бот, IWE, LMS) реализует свой адаптер.
    engine.py и evaluator.py НЕ знают о канале.
    """

    @abstractmethod
    async def deliver(
        self,
        user_id: int,
        lesson: dict,
        generated: dict,
    ) -> LessonDeliveryResult:
        """Доставить занятие пользователю.

        Args:
            user_id: идентификатор пользователя (chat_id для бота)
            lesson: structured lesson из TailorEngine.assemble()
            generated: результат generate_lesson_text()

        Returns:
            LessonDeliveryResult с результатом доставки.
        """

    @abstractmethod
    async def format_lesson(self, lesson: dict, generated: dict) -> str:
        """Форматировать занятие для конкретного канала.

        Args:
            lesson: structured lesson
            generated: результат generate_lesson_text()

        Returns:
            Форматированная строка (HTML для бота, Markdown для IWE).
        """
