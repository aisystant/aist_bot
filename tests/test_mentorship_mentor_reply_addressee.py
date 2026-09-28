"""
WP-578 — the mentor branch of archive_tap._process_one must SEARCH the addressee
of a reply, never create a participant_core row for them (docs/processes/
process-19: "reply/forward targets are looked up, not created"). Before this
fix a mentor's reply to another reader or an outsider with a platform account
silently enrolled that person as a stream participant.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path or sys.path.index(_PROJECT_ROOT) > 0:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

import pytest
from unittest.mock import AsyncMock

MENTOR_TG_ID = 100
PARTICIPANT_TG_ID = 200

ACCOUNTS = {
    MENTOR_TG_ID: "mentor-account",
    PARTICIPANT_TG_ID: "participant-account",
    300: "other-mentor-account",
    400: "outsider-account",
}
READERS = {"mentor-account": "mentor", "other-mentor-account": "mentor"}


@pytest.fixture(autouse=True)
def _reset_counters():
    import engines.mentorship.archive_tap as archive_tap

    original = dict(archive_tap._dropped_counters)
    archive_tap._dropped_counters.update({k: 0 for k in original})
    yield
    archive_tap._dropped_counters.clear()
    archive_tap._dropped_counters.update(original)


def _event(*, author_tg_id, reply_to_user_id):
    from engines.mentorship.archive_tap import RawMessageEvent

    return RawMessageEvent(
        telegram_chat_id=-1001,
        chat_type="group",
        telegram_message_id=1,
        telegram_user_id=author_tg_id,
        text="reply text",
        message_at=datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc),
        is_edit=False,
        reply_to_user_id=reply_to_user_id,
        forward_from_user_id=None,
        message_thread_id=None,
        is_reply=reply_to_user_id is not None,
        mentioned_user_ids=(),
    )


@pytest.fixture
def db(monkeypatch):
    """Fake DB layer of _process_one; the recorders are the observable effects."""
    import engines.mentorship.archive_tap as archive_tap
    import db.queries.consent as consent_queries
    import db.queries.mentorship as mentorship_queries
    from db.queries.mentorship import StreamChatContext

    class Fakes:
        find = AsyncMock(return_value=None)
        create = AsyncMock(return_value=99)
        write = AsyncMock()
        consent = AsyncMock(return_value=True)
        related = AsyncMock(return_value=None)

    monkeypatch.setattr(archive_tap, "resolve_ory_id_from_chat", AsyncMock(side_effect=ACCOUNTS.get))
    monkeypatch.setattr(
        mentorship_queries,
        "lookup_stream_chat",
        AsyncMock(return_value=StreamChatContext(stream_id="S1", reader_account_id="reader-1")),
    )
    monkeypatch.setattr(mentorship_queries, "get_stream_reader_role", AsyncMock(side_effect=lambda account_id, _stream: READERS.get(account_id)))
    monkeypatch.setattr(mentorship_queries, "find_participant_id", Fakes.find)
    monkeypatch.setattr(mentorship_queries, "get_or_create_participant", Fakes.create)
    monkeypatch.setattr(mentorship_queries, "write_archive_entry", Fakes.write)
    monkeypatch.setattr(consent_queries, "get_consent_grant", Fakes.consent)
    # the lookup itself is covered by test_mentorship_resolve_related.py; here only its call seam
    monkeypatch.setattr(archive_tap, "_resolve_related_participant", Fakes.related)
    return Fakes


@pytest.mark.asyncio
async def test_mentor_reply_to_existing_participant_is_archived_under_found_row(db):
    import engines.mentorship.archive_tap as archive_tap

    db.find.return_value = 7

    await archive_tap._process_one(_event(author_tg_id=MENTOR_TG_ID, reply_to_user_id=PARTICIPANT_TG_ID))

    db.find.assert_awaited_once_with("mentor-account", "S1", "participant-account")
    db.create.assert_not_called()
    db.write.assert_awaited_once()
    written = db.write.await_args.kwargs
    assert written["participant_id"] == 7
    assert written["author"] == "mentor"
    assert written["reader_account_id"] == "mentor-account"
    assert archive_tap.queue_stats()["skipped_recipient_not_participant"] == 0
    # the consent that gates text storage is the addressee's, not the mentor's
    db.consent.assert_awaited_once_with("participant-account", "mentor_archive_group")
    assert written["text"] == "reply text"
    # reply_to / forward_from links keep resolving under the mentor's RLS context,
    # excluding the addressee (they are the row's own participant)
    assert db.related.await_count == 2
    for call in db.related.await_args_list:
        assert call.kwargs == {
            "reader_account_id": "mentor-account",
            "stream_id": "S1",
            "exclude_account_id": "participant-account",
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("addressee_tg_id", [300, 400], ids=["other-mentor", "outsider-with-account"])
async def test_mentor_reply_to_unknown_addressee_is_skipped_and_never_creates_participant(db, addressee_tg_id):
    import engines.mentorship.archive_tap as archive_tap

    db.find.return_value = None

    await archive_tap._process_one(_event(author_tg_id=MENTOR_TG_ID, reply_to_user_id=addressee_tg_id))

    db.find.assert_awaited_once_with("mentor-account", "S1", ACCOUNTS[addressee_tg_id])
    db.create.assert_not_called()
    db.write.assert_not_called()
    stats = archive_tap.queue_stats()
    assert stats["skipped_recipient_not_participant"] == 1
    assert stats["skipped_no_reply"] == 0
    assert stats["skipped_recipient_unresolved"] == 0


@pytest.mark.asyncio
async def test_mentor_message_without_reply_is_skipped_and_counted(db):
    import engines.mentorship.archive_tap as archive_tap

    await archive_tap._process_one(_event(author_tg_id=MENTOR_TG_ID, reply_to_user_id=None))

    db.find.assert_not_called()
    db.create.assert_not_called()
    db.write.assert_not_called()
    assert archive_tap.queue_stats()["skipped_no_reply"] == 1


@pytest.mark.asyncio
async def test_mentor_reply_to_unlinked_account_is_skipped_and_counted(db):
    import engines.mentorship.archive_tap as archive_tap

    unlinked_tg_id = 999  # not in ACCOUNTS: no platform account behind this Telegram user
    await archive_tap._process_one(_event(author_tg_id=MENTOR_TG_ID, reply_to_user_id=unlinked_tg_id))

    db.find.assert_not_called()
    db.create.assert_not_called()
    db.write.assert_not_called()
    assert archive_tap.queue_stats()["skipped_recipient_unresolved"] == 1


@pytest.mark.asyncio
async def test_participant_author_still_gets_a_row_created_on_first_message(db):
    import engines.mentorship.archive_tap as archive_tap

    await archive_tap._process_one(_event(author_tg_id=PARTICIPANT_TG_ID, reply_to_user_id=None))

    db.create.assert_awaited_once_with("reader-1", "S1", "participant-account")
    db.find.assert_not_called()
    written = db.write.await_args.kwargs
    assert written["participant_id"] == 99
    assert written["author"] == "participant"
    assert written["reader_account_id"] == "reader-1"
