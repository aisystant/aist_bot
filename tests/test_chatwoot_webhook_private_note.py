"""Only public Chatwoot replies may be forwarded to a Telegram user."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import oauth_server


def _request(private=...):
    payload = {
        "event": "message_created",
        "message_type": 1,
        "conversation": {"id": 17},
        "content": "Ответ оператора",
    }
    if private is not ...:
        payload["private"] = private
    return SimpleNamespace(
        read=AsyncMock(return_value=json.dumps(payload).encode()),
        headers={"X-Chatwoot-Signature": "signed"},
    )


@pytest.mark.parametrize("private", [True, "false", None, ...])
async def test_internal_or_unmarked_message_is_not_forwarded(private):
    bot = SimpleNamespace(send_message=AsyncMock())
    lookup = AsyncMock(return_value=42)
    with patch("clients.chatwoot.verify_signature", return_value=True), \
         patch("db.queries.helpdesk.get_chat_id_by_conversation", lookup), \
         patch.object(oauth_server, "_bot_instance", bot):
        response = await oauth_server.chatwoot_webhook_handler(_request(private))

    assert response.status == 200
    lookup.assert_not_awaited()
    bot.send_message.assert_not_awaited()


async def test_explicit_public_reply_is_forwarded():
    bot = SimpleNamespace(send_message=AsyncMock())
    lookup = AsyncMock(return_value=42)
    with patch("clients.chatwoot.verify_signature", return_value=True), \
         patch("db.queries.helpdesk.get_chat_id_by_conversation", lookup), \
         patch.object(oauth_server, "_bot_instance", bot):
        response = await oauth_server.chatwoot_webhook_handler(_request(False))

    assert response.status == 200
    lookup.assert_awaited_once_with(17)
    bot.send_message.assert_awaited_once_with(
        42,
        "🛟 *Агент поддержки:*\n\nОтвет оператора",
        parse_mode="Markdown",
    )
