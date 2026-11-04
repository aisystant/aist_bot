"""WakaTime authorization uses shared, one-use states across bot restarts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

import pytest
from aiohttp.test_utils import make_mocked_request

import oauth_server
from clients.wakatime_oauth import WakaTimeOAuthClient
from db.queries import oauth_states
from handlers import wakatime
from states.common.settings import SettingsState


@pytest.fixture
def client():
    client = WakaTimeOAuthClient()
    client.client_id = "test-client"
    client.redirect_uri = "https://example.com/auth/wakatime/callback"
    return client


async def test_authorization_link_persists_state_before_returning(client, monkeypatch):
    save = AsyncMock()
    monkeypatch.setattr(oauth_states, "save_oauth_state", save)

    url, state = await client.get_authorization_url(123)

    query = parse_qs(urlparse(url).query)
    assert query["state"] == [state]
    assert query["redirect_uri"] == [client.redirect_uri]
    assert query["scope"] == ["read_stats,read_logged_time"]
    save.assert_awaited_once_with(state, "wakatime", 123)


async def test_new_client_resolves_saved_state_without_process_memory(client, monkeypatch):
    save = AsyncMock()
    monkeypatch.setattr(oauth_states, "save_oauth_state", save)
    _, state = await client.get_authorization_url(123)
    validate = AsyncMock(return_value=123)
    monkeypatch.setattr(oauth_states, "validate_oauth_state", validate)

    restarted_client = WakaTimeOAuthClient()

    assert await restarted_client.validate_state(state) == 123
    validate.assert_awaited_once_with(state)


async def test_storage_failure_does_not_issue_an_unusable_link(client, monkeypatch):
    save = AsyncMock(side_effect=ConnectionError("storage unavailable"))
    monkeypatch.setattr(oauth_states, "save_oauth_state", save)

    with pytest.raises(ConnectionError, match="storage unavailable"):
        await client.get_authorization_url(123)


@pytest.mark.parametrize("state", ["unknown", "expired", "already-used"])
async def test_rejected_state_does_not_exchange_code(client, monkeypatch, state):
    validate = AsyncMock(return_value=None)
    exchange = AsyncMock()
    monkeypatch.setattr(oauth_states, "validate_oauth_state", validate)
    monkeypatch.setattr(client, "exchange_code", exchange)
    monkeypatch.setattr(oauth_server, "wakatime_oauth", client)
    request = make_mocked_request(
        "GET", f"/auth/wakatime/callback?code=test-code&state={state}"
    )

    response = await oauth_server.wakatime_callback_handler(request)

    assert response.status == 400
    assert "Сессия авторизации истекла" in response.text
    validate.assert_awaited_once_with(state)
    exchange.assert_not_called()


async def test_callback_consumes_state_before_exchange_and_rejects_replay(client, monkeypatch):
    validate = AsyncMock(side_effect=[123, None])

    async def exchange(code, telegram_user_id):
        validate.assert_awaited_once_with("saved-state")
        assert (code, telegram_user_id) == ("test-code", 123)
        return {"telegram_user_id": telegram_user_id}

    exchange_mock = AsyncMock(side_effect=exchange)
    monkeypatch.setattr(oauth_states, "validate_oauth_state", validate)
    monkeypatch.setattr(client, "exchange_code", exchange_mock)
    monkeypatch.setattr(oauth_server, "wakatime_oauth", client)
    monkeypatch.setattr(oauth_server, "_bot_instance", None)
    request = make_mocked_request(
        "GET", "/auth/wakatime/callback?code=test-code&state=saved-state"
    )

    response = await oauth_server.wakatime_callback_handler(request)
    repeated_response = await oauth_server.wakatime_callback_handler(request)

    assert response.status == 200
    assert "WakaTime подключён!" in response.text
    assert repeated_response.status == 400
    exchange_mock.assert_awaited_once_with("test-code", 123)


@pytest.fixture
def authorization_link(monkeypatch):
    from clients.wakatime_oauth import wakatime_oauth

    link = AsyncMock(return_value=("https://wakatime.com/oauth/authorize?state=test", "test"))
    monkeypatch.setattr(wakatime_oauth, "get_authorization_url", link)
    monkeypatch.setattr(
        "db.queries.wakatime.get_wakatime_connection", AsyncMock(return_value=None)
    )
    return link


async def test_command_shows_button_after_async_state_save(authorization_link, monkeypatch):
    monkeypatch.setattr(wakatime, "get_intern", AsyncMock(return_value={"language": "ru"}))
    message = SimpleNamespace(chat=SimpleNamespace(id=123), text="/wakatime", answer=AsyncMock())

    await wakatime.cmd_wakatime(message)

    authorization_link.assert_awaited_once_with(123)
    keyboard = message.answer.call_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].url == authorization_link.return_value[0]


async def test_settings_shows_button_after_async_state_save(authorization_link):
    settings = SimpleNamespace(
        _get_lang=Mock(return_value="ru"),
        _get_chat_id=Mock(return_value=123),
        _set_waiting=AsyncMock(),
    )
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()))

    await SettingsState._handle_waka_connection(settings, {}, callback)

    authorization_link.assert_awaited_once_with(123)
    keyboard = callback.message.edit_text.call_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].url == authorization_link.return_value[0]
