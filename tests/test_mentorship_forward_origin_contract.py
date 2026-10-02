"""Contract of the DM resolver with REAL aiogram forward-origin models (WP-578, live defect 02.10).

`_resolve_dm_participant_target` tells "author known" from "author unknown" by the presence of
`forward_origin.sender_user`. Fakes built from `MagicMock(spec=[...])` can drift from the installed aiogram,
so this file pins the assumption on the real classes and runs the resolver against them.

Rule under test: a forward written by somebody else starts a new identification. The previous participant is
dropped first and remembered again only when the identification succeeds, so a failed or refused lookup never
leaves the previous participant active.
"""

from datetime import datetime, timezone
from typing import get_args
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import (
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUnion,
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
_FORWARD_DATE = datetime(2026, 10, 2, tzinfo=timezone.utc)


def test_origin_union_has_exactly_the_four_kinds_the_resolver_classifies():
    """A new origin type added by aiogram fails here until the resolver decision for it is made."""
    assert set(get_args(MessageOriginUnion)) == {
        MessageOriginUser,
        MessageOriginHiddenUser,
        MessageOriginChat,
        MessageOriginChannel,
    }


def test_only_the_user_origin_carries_a_sender_user():
    for origin_cls in get_args(MessageOriginUnion):
        assert ("sender_user" in origin_cls.model_fields) == (origin_cls is MessageOriginUser), origin_cls.__name__


def _prepare(monkeypatch, *, active_set_at=4900.0, now=5000.0):
    """The mentorship module with a controlled clock and, unless `active_set_at` is None, a remembered participant."""
    import handlers.mentorship as mentorship

    monkeypatch.setattr(mentorship, "time", lambda: now)
    state = {}
    if active_set_at is not None:
        state[CALLER_TG_ID] = mentorship._ActiveParticipant(
            stream_id="S1",
            participant_account_id=ACTIVE_PARTICIPANT_ID,
            participant_name="Пётр Петров",
            set_at=active_set_at,
        )
    monkeypatch.setattr(mentorship, "_active_participant", state)
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


def _real_target(**fields):
    """A real aiogram Message, so the resolver reads the genuine model (including the deprecated fields)."""
    return Message.model_validate(
        {
            "message_id": 11,
            "date": 1759400000,
            "chat": {"id": CALLER_TG_ID, "type": "private", "first_name": "Наставник"},
            "from": {"id": CALLER_TG_ID, "is_bot": False, "first_name": "Наставник"},
            "text": "чужой текст",
            **fields,
        }
    )


def _visible_other_user_origin():
    return MessageOriginUser(
        date=_FORWARD_DATE, sender_user=User(id=201, is_bot=False, first_name="Анна", last_name="Видимая")
    )


_MEMORY_STATES = {
    "fresh active participant": dict(active_set_at=4900.0, now=5000.0),
    "no active participant": dict(active_set_at=None, now=5000.0),
    "expired active participant": dict(active_set_at=0.0, now=99999.0),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_state", list(_MEMORY_STATES.values()), ids=list(_MEMORY_STATES))
@pytest.mark.parametrize(
    "make_target, expected_fragment",
    [
        (make_forward_hidden_author, "скрыл аккаунт"),
        (make_forward_from_channel, "не от участника"),
        (make_forward_from_chat, "не от участника"),
    ],
)
async def test_resolver_refuses_every_origin_without_a_user_author_whatever_it_remembers(
    monkeypatch, memory_state, make_target, expected_fragment
):
    """Without a remembered participant the generic advice "forward a participant message" would be wrong
    for a hidden author (a re-forward is hidden again), so the specific refusal must come first."""
    mentorship = _prepare(monkeypatch, **memory_state)
    resolve_mock = AsyncMock()
    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", resolve_mock)

    result = await mentorship._resolve_dm_participant_target(_dm_message(), make_target())

    assert result[:3] == (None, None, None)
    assert expected_fragment in result[3]
    resolve_mock.assert_not_awaited()
    assert CALLER_TG_ID not in mentorship._active_participant


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy_fields, expected_fragment",
    [
        ({"forward_date": 1759390000, "forward_sender_name": "Анна К."}, "«Анна К.»"),
        (
            {"forward_date": 1759390000, "forward_from_chat": {"id": -1001234567890, "type": "channel", "title": "К"}},
            "не от участника",
        ),
    ],
    ids=["deprecated hidden sender name", "deprecated chat"],
)
async def test_resolver_refuses_a_forward_marked_only_by_the_deprecated_fields(
    monkeypatch, legacy_fields, expected_fragment
):
    """Bot API 7.0 replaced these fields with `forward_origin`; should a payload still carry only them, it
    must not be taken for a plain reply and answered with the previous participant."""
    mentorship = _prepare(monkeypatch)
    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock())

    result = await mentorship._resolve_dm_participant_target(_dm_message(), _real_target(**legacy_fields))

    assert result[:3] == (None, None, None)
    assert expected_fragment in result[3]
    assert CALLER_TG_ID not in mentorship._active_participant


@pytest.mark.asyncio
async def test_resolver_own_forward_uses_the_active_participant(monkeypatch):
    mentorship = _prepare(monkeypatch)
    own = MessageOriginUser(date=_FORWARD_DATE, sender_user=User(id=CALLER_TG_ID, is_bot=False, first_name="Наставник"))

    result = await mentorship._resolve_dm_participant_target(_dm_message(), _target_with_origin(own))

    assert result == ("S1", ACTIVE_PARTICIPANT_ID, "Пётр Петров", None)
    assert mentorship._active_participant[CALLER_TG_ID].participant_account_id == ACTIVE_PARTICIPANT_ID


@pytest.mark.asyncio
async def test_resolver_visible_other_user_is_resolved_and_becomes_the_active_participant(monkeypatch):
    mentorship = _prepare(monkeypatch)
    monkeypatch.setattr(
        mentorship, "resolve_ory_id_from_chat", AsyncMock(side_effect=[OTHER_PARTICIPANT_ID, MENTOR_ID])
    )
    monkeypatch.setattr(
        mentorship,
        "lookup_participant_stream",
        AsyncMock(return_value=StreamChatContext(stream_id="S2", reader_account_id=MENTOR_ID)),
    )
    monkeypatch.setattr(mentorship, "get_stream_reader_role", AsyncMock(return_value="mentor"))

    result = await mentorship._resolve_dm_participant_target(
        _dm_message(), _target_with_origin(_visible_other_user_origin())
    )

    assert result == ("S2", OTHER_PARTICIPANT_ID, "Анна Видимая", None)
    assert mentorship._active_participant[CALLER_TG_ID].participant_account_id == OTHER_PARTICIPANT_ID


async def _resolve_visible_other_user(mentorship, monkeypatch, *, resolve, lookup, role):
    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", resolve)
    monkeypatch.setattr(mentorship, "lookup_participant_stream", lookup)
    monkeypatch.setattr(mentorship, "get_stream_reader_role", role)
    return await mentorship._resolve_dm_participant_target(
        _dm_message(), _target_with_origin(_visible_other_user_origin())
    )


@pytest.mark.asyncio
async def test_resolver_visible_author_without_a_platform_account_drops_the_previous_participant(monkeypatch):
    mentorship = _prepare(monkeypatch)

    result = await _resolve_visible_other_user(
        mentorship, monkeypatch, resolve=AsyncMock(return_value=None), lookup=AsyncMock(), role=AsyncMock()
    )

    assert result[:3] == (None, None, None)
    assert "нет привязанного аккаунта" in result[3]
    assert CALLER_TG_ID not in mentorship._active_participant


@pytest.mark.asyncio
async def test_resolver_visible_author_without_a_stream_drops_the_previous_participant(monkeypatch):
    mentorship = _prepare(monkeypatch)

    result = await _resolve_visible_other_user(
        mentorship,
        monkeypatch,
        resolve=AsyncMock(return_value=OTHER_PARTICIPANT_ID),
        lookup=AsyncMock(return_value=None),
        role=AsyncMock(),
    )

    assert result[:3] == (None, None, None)
    assert "Не нашёл поток" in result[3]
    assert CALLER_TG_ID not in mentorship._active_participant


@pytest.mark.asyncio
async def test_resolver_visible_author_of_a_stream_the_mentor_does_not_read_drops_the_previous_participant(
    monkeypatch,
):
    mentorship = _prepare(monkeypatch)

    result = await _resolve_visible_other_user(
        mentorship,
        monkeypatch,
        resolve=AsyncMock(side_effect=[OTHER_PARTICIPANT_ID, MENTOR_ID]),
        lookup=AsyncMock(return_value=StreamChatContext(stream_id="S2", reader_account_id=MENTOR_ID)),
        role=AsyncMock(return_value=None),
    )

    assert result[:3] == (None, None, None)
    assert "не числишься наставником" in result[3]
    assert CALLER_TG_ID not in mentorship._active_participant


@pytest.mark.asyncio
async def test_after_a_refusal_the_free_text_note_handler_no_longer_matches(monkeypatch):
    """The remembered participant is what makes a typed text a note candidate; a refusal must end that."""
    mentorship = _prepare(monkeypatch)
    monkeypatch.setattr(mentorship, "resolve_ory_id_from_chat", AsyncMock())
    assert mentorship._has_fresh_active_participant(_dm_message()) is True

    await mentorship._resolve_dm_participant_target(_dm_message(), make_forward_hidden_author())

    assert mentorship._has_fresh_active_participant(_dm_message()) is False
