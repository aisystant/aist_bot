"""Contract of the DM resolver with REAL aiogram forward-origin models (WP-578, live defect 02.10).

`_resolve_dm_participant_target` tells "author known" from "author unknown" by the presence of
`forward_origin.sender_user`. Fakes built from `MagicMock(spec=[...])` can drift from the installed aiogram,
so this file pins the assumption on the real classes and runs the resolver against them.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import (
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
    User,
)

from db.queries.mentorship import StreamChatContext
from tests.mentorship_helpers import (
    make_forward_from_channel,
    make_forward_from_chat,
    make_forward_hidden_author,
)

CALLER_TG_ID = 100
MENTOR_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ACTIVE_PARTICIPANT_ID = "11111111-1111-1111-1111-111111111111"
OTHER_PARTICIPANT_ID = "22222222-2222-2222-2222-222222222222"


def test_only_the_user_origin_carries_a_sender_user():
    assert "sender_user" in MessageOriginUser.model_fields
    for origin_cls in (MessageOriginHiddenUser, MessageOriginChat, MessageOriginChannel):
        assert "sender_user" not in origin_cls.model_fields, origin_cls.__name__


@pytest.fixture
def mentorship_with_active_participant(monkeypatch):
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "time", lambda: 5000.0)
    monkeypatch.setattr(
        mentorship,
        "_active_participant",
        {
            CALLER_TG_ID: mentorship._ActiveParticipant(
                stream_id="S1",
                participant_account_id=ACTIVE_PARTICIPANT_ID,
                participant_name="Пётр Петров",
                set_at=4900.0,
            )
        },
    )
    return mentorship


def _dm_message():
    message = MagicMock(spec=Message)
    message.from_user = MagicMock(spec=User)
    message.from_user.id = CALLER_TG_ID
    return message


def _target_with_origin(origin):
    target = MagicMock(spec=Message)
    target.forward_origin = origin
    return target


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_target, expected_fragment",
    [
        (make_forward_hidden_author, "скрыл аккаунт"),
        (make_forward_from_channel, "не от участника"),
        (make_forward_from_chat, "не от участника"),
    ],
)
async def test_resolver_refuses_every_origin_without_a_user_author(
    monkeypatch, mentorship_with_active_participant, make_target, expected_fragment
):
    mentorship = mentorship_with_active_participant
    resolve_mock = AsyncMock()
    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", resolve_mock)

    result = await mentorship._resolve_dm_participant_target(_dm_message(), make_target())

    assert result[:3] == (None, None, None)
    assert expected_fragment in result[3]
    resolve_mock.assert_not_awaited()
    assert mentorship._active_participant[CALLER_TG_ID].participant_account_id == ACTIVE_PARTICIPANT_ID


@pytest.mark.asyncio
async def test_resolver_own_forward_uses_the_active_participant(mentorship_with_active_participant):
    mentorship = mentorship_with_active_participant
    own = MessageOriginUser(
        date=datetime(2026, 10, 2, tzinfo=timezone.utc),
        sender_user=User(id=CALLER_TG_ID, is_bot=False, first_name="Наставник"),
    )

    result = await mentorship._resolve_dm_participant_target(_dm_message(), _target_with_origin(own))

    assert result == ("S1", ACTIVE_PARTICIPANT_ID, "Пётр Петров", None)


@pytest.mark.asyncio
async def test_resolver_visible_other_user_is_resolved_and_becomes_the_active_participant(
    monkeypatch, mentorship_with_active_participant
):
    mentorship = mentorship_with_active_participant
    monkeypatch.setattr(
        mentorship, "resolve_ory_id_from_chat", AsyncMock(side_effect=[OTHER_PARTICIPANT_ID, MENTOR_ID])
    )
    monkeypatch.setattr(
        mentorship,
        "lookup_participant_stream",
        AsyncMock(return_value=StreamChatContext(stream_id="S2", reader_account_id=MENTOR_ID)),
    )
    monkeypatch.setattr(mentorship, "get_stream_reader_role", AsyncMock(return_value="mentor"))
    other = MessageOriginUser(
        date=datetime(2026, 10, 2, tzinfo=timezone.utc),
        sender_user=User(id=201, is_bot=False, first_name="Анна", last_name="Видимая"),
    )

    result = await mentorship._resolve_dm_participant_target(_dm_message(), _target_with_origin(other))

    assert result == ("S2", OTHER_PARTICIPANT_ID, "Анна Видимая", None)
    assert mentorship._active_participant[CALLER_TG_ID].participant_account_id == OTHER_PARTICIPANT_ID
