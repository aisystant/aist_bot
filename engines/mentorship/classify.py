"""
WP-578 Ф2 — детерминированная классификация сообщений архива переписки
наставник↔участник.

Правила закреплены консенсусом пир-сессии Claude+Kimi (17.09.2026,
MC-sessions:2026-09/17/2026-09-17-06-wp578-f2-telegram-bridge/) и разобраны в
DS-my-strategy/inbox/WP-578/DRR-f2-telegram-bridge.md §4. Только структурные
метаданные Telegram — никаких эвристик по тексту сообщения. Чистые функции,
без побочных эффектов и без I/O — вся сборка входных сигналов из живого
сообщения и БД делается в archive_tap.py.
"""

from dataclasses import dataclass
from typing import Optional


def is_bot_command(text: Optional[str]) -> bool:
    """Служебная команда боту (начинается с `/`) — не переписка, в архив не пишется вообще."""
    return bool(text) and text.startswith("/")


@dataclass(frozen=True)
class MentionSignals:
    """Структурные признаки одного сообщения, нужные для классификации.

    author_is_mentor — автор сообщения сам наставник/пилот потока.
    channel_is_dm — личный чат участника с ботом (двусторонний по построению).
    reply_to_is_mentor — сообщение отвечает наставнику (reply_to_message),
        независимо от того, внутри темы форума это или нет.
    mentions_mentor — сообщение упоминает наставника (entity text_mention;
        известное ограничение — простое @username-упоминание не резолвится,
        см. archive_tap.py).
    is_reply_in_topic — сообщение одновременно (а) reply на другое сообщение
        и (б) находится внутри темы форума (message_thread_id задан).
    topic_creator_is_mentor — автор первого сообщения этой темы — наставник;
        None, если создатель темы неизвестен (кэш пуст, например после
        рестарта бота до появления первого сообщения этой темы после старта).
    """

    author_is_mentor: bool
    channel_is_dm: bool
    reply_to_is_mentor: bool
    mentions_mentor: bool
    is_reply_in_topic: bool
    topic_creator_is_mentor: Optional[bool]


def compute_addressed_to_mentor(signals: MentionSignals) -> bool:
    """DRR-f2 §4, правило v3.

    true, если хотя бы одно из:
      - author = mentor
      - channel = dm
      - author = participant И reply на наставника
      - author = participant И упоминание наставника
      - author = participant И reply внутри темы форума, чьё первое
        сообщение — наставника (узкое правило Кими, ход 3: новое, не-reply
        сообщение в чужой теме форума НЕ считается адресным, даже если тему
        когда-то открыл наставник — иначе ложное срабатывание на переписке
        участников между собой внутри темы наставника).
    Неизвестный создатель темы (topic_creator_is_mentor is None) не
    засчитывает reply-в-теме как адресный — сообщение всё равно пишется в
    архив (fail-closed только на переоценку охвата "адресовано наставнику",
    не на потерю самого сообщения).
    """
    if signals.author_is_mentor:
        return True
    if signals.channel_is_dm:
        return True
    if signals.reply_to_is_mentor:
        return True
    if signals.mentions_mentor:
        return True
    if signals.is_reply_in_topic and signals.topic_creator_is_mentor:
        return True
    return False
