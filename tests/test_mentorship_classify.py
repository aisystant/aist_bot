"""
WP-578 Ф2 — тесты классификации архива переписки (engines/mentorship/classify.py).

Чистые функции, без I/O и без aiogram — быстрый unit-слой поверх smoke-теста
middleware (tests/smoke/test_mentorship_middleware.py).
"""

from engines.mentorship.classify import MentionSignals, compute_addressed_to_mentor, is_bot_command


def _signals(**overrides) -> MentionSignals:
    base = dict(
        author_is_mentor=False,
        channel_is_dm=False,
        reply_to_is_mentor=False,
        mentions_mentor=False,
        is_reply_in_topic=False,
        topic_creator_is_mentor=None,
    )
    base.update(overrides)
    return MentionSignals(**base)


class TestIsBotCommand:
    def test_command_detected(self):
        assert is_bot_command("/mentor_stream S1") is True

    def test_plain_text_is_not_command(self):
        assert is_bot_command("привет, как дела") is False

    def test_none_is_not_command(self):
        assert is_bot_command(None) is False

    def test_empty_string_is_not_command(self):
        assert is_bot_command("") is False


class TestComputeAddressedToMentor:
    def test_mentor_author_always_addressed(self):
        assert compute_addressed_to_mentor(_signals(author_is_mentor=True)) is True

    def test_dm_always_addressed(self):
        assert compute_addressed_to_mentor(_signals(channel_is_dm=True)) is True

    def test_reply_to_mentor_addressed(self):
        assert compute_addressed_to_mentor(_signals(reply_to_is_mentor=True)) is True

    def test_mention_addressed(self):
        assert compute_addressed_to_mentor(_signals(mentions_mentor=True)) is True

    def test_ambient_group_message_not_addressed(self):
        """Фоновое сообщение участника в группе без reply/упоминания/темы — false."""
        assert compute_addressed_to_mentor(_signals()) is False

    def test_reply_in_mentor_topic_addressed(self):
        assert (
            compute_addressed_to_mentor(
                _signals(is_reply_in_topic=True, topic_creator_is_mentor=True)
            )
            is True
        )

    def test_reply_in_non_mentor_topic_not_addressed(self):
        """Тему открыл не наставник — reply внутри неё не адресный (Кими, ход 3)."""
        assert (
            compute_addressed_to_mentor(
                _signals(is_reply_in_topic=True, topic_creator_is_mentor=False)
            )
            is False
        )

    def test_new_message_in_mentor_topic_not_addressed(self):
        """Ключевое узкое правило: НЕ-reply сообщение в теме наставника — не
        адресное, даже если тему открыл наставник (ложное срабатывание,
        закрытое Кими в ходе 3 — участники переписываются между собой
        внутри темы наставника)."""
        assert (
            compute_addressed_to_mentor(
                _signals(is_reply_in_topic=False, topic_creator_is_mentor=True)
            )
            is False
        )

    def test_unknown_topic_creator_does_not_count_as_addressed(self):
        """Кэш создателя темы пуст (бот перезапущен) — fail-closed на
        переоценку охвата, не отказ обработки сообщения целиком."""
        assert (
            compute_addressed_to_mentor(
                _signals(is_reply_in_topic=True, topic_creator_is_mentor=None)
            )
            is False
        )
