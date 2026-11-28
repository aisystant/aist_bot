"""WP-384: voice notes and Siri notes are off except for the developer and listed chats."""

from unittest.mock import patch

import pytest

from core import voice_access


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", frozenset()),
        ("  ", frozenset()),
        ("111, 222", frozenset({111, 222})),
        ("111,abc,,333", frozenset({111, 333})),
        ("*", None),
        ("111,*", None),
    ],
)
def test_parse_allowed(raw, expected):
    assert voice_access.parse_allowed(raw) == expected


@pytest.mark.parametrize(
    ("allowed", "developer", "chat_id", "expected"),
    [
        (frozenset(), 0, 555, False),
        (frozenset(), 555, 555, True),
        (frozenset(), 555, 777, False),
        (frozenset({777}), 0, 777, True),
        (frozenset({777}), 0, 0, False),
        (None, 0, 999, True),
    ],
)
def test_voice_enabled_for(allowed, developer, chat_id, expected):
    with patch.object(voice_access, "_ALLOWED", allowed), patch.object(voice_access, "DEVELOPER_CHAT_ID", developer):
        assert voice_access.voice_enabled_for(chat_id) is expected
