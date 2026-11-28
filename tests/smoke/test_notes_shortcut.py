"""WP-384: fleeting notes from Siri / iOS Shortcuts.

POST /api/notes with the personal notes key from /siri saves the text exactly like
a typed ".text" note and returns a phrase the shortcut speaks back. Covered:
- core.fleeting_notes.save_fleeting_note outcomes (access, GitHub, repo, auth, failure, saved);
- the endpoint: key checks, rate limit, empty and oversized text, unlinked account,
  the spoken phrase (names the notes repo), no proactive chat message;
- personal key storage separates the notes key from the browser extension key;
- /siri: reuse, rotation with "/siri new", account not linked.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import oauth_server
from core.fleeting_notes import NoteOutcome, NoteStatus, save_fleeting_note
from db.queries import iwe_extension
from handlers.siri import cmd_siri, setup_text
from oauth_server import notes_shortcut_handler

TOKEN = "a" * 64
URL = "https://github.com/owner/notes/blob/main/fleeting-notes.md"


@pytest.fixture(autouse=True)
def _clear_rate_limit():
    oauth_server._notes_rate_limit.clear()
    yield
    oauth_server._notes_rate_limit.clear()


@pytest.fixture(autouse=True)
def _voice_enabled():
    with patch("core.voice_access.voice_enabled_for", return_value=True) as endpoint_gate, \
         patch("handlers.siri.voice_enabled_for", return_value=True) as command_gate:
        yield endpoint_gate, command_gate


def _request(body, token=TOKEN):
    request = MagicMock()
    request.headers = {"X-IWE-Api-Key": token} if token is not None else {}
    request.json = AsyncMock(return_value=body)
    return request


def _payload(response):
    return json.loads(response.text)


async def _post(body, token=TOKEN, account="acc-1", chat_id=777,
                outcome=NoteOutcome(NoteStatus.SAVED, url=URL, repo="owner/notes")):
    bot = MagicMock(send_message=AsyncMock())
    with patch("db.queries.iwe_extension.validate_extension_token", new_callable=AsyncMock, return_value=account) as validate, \
         patch("db.queries.iwe_extension.get_telegram_id_for_account", new_callable=AsyncMock, return_value=chat_id), \
         patch("core.fleeting_notes.save_fleeting_note", new_callable=AsyncMock, return_value=outcome) as save, \
         patch.object(oauth_server, "_bot_instance", bot):
        response = await notes_shortcut_handler(_request(body, token))
    return response, validate, save, bot


# ── save_fleeting_note ────────────────────────────────────────────────────────

async def _save(has_access=True, connected=True, repo="owner/notes", append=None, append_error=None):
    from clients.github_auth import GitHubAuthUnavailable

    append_mock = AsyncMock(return_value=append, side_effect=GitHubAuthUnavailable("gone") if append_error else None)
    with patch("core.access.access_layer.has_access", new_callable=AsyncMock, return_value=has_access), \
         patch("clients.github_oauth.github_oauth.is_connected", new_callable=AsyncMock, return_value=connected), \
         patch("clients.github_oauth.github_oauth.get_target_repo", new_callable=AsyncMock, return_value=repo), \
         patch("clients.github_api.github_notes.append_note", append_mock):
        outcome = await save_fleeting_note(777, "купить молоко")
    return outcome, append_mock


@pytest.mark.asyncio
async def test_save_note_appends_and_returns_url():
    outcome, append = await _save(append={"repo": "owner/notes", "path": "fleeting-notes.md", "branch": "main"})
    append.assert_awaited_once_with(777, "купить молоко")
    assert outcome == NoteOutcome(NoteStatus.SAVED, url=URL, repo="owner/notes")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "status"),
    [
        ({"has_access": False}, NoteStatus.NO_ACCESS),
        ({"connected": False}, NoteStatus.NO_GITHUB),
        ({"repo": None}, NoteStatus.NO_REPO),
        ({"append_error": True}, NoteStatus.RECONNECT),
        ({"append": None}, NoteStatus.FAILED),
    ],
)
async def test_save_note_refusals(kwargs, status):
    outcome, append = await _save(**kwargs)
    assert outcome.status is status
    assert outcome.url is None
    if status in (NoteStatus.NO_ACCESS, NoteStatus.NO_GITHUB, NoteStatus.NO_REPO):
        append.assert_not_awaited()


# ── POST /api/notes ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_endpoint_saves_note_and_names_the_repo():
    response, validate, save, bot = await _post({"text": "  идея про экзоскелет  "})

    assert response.status == 200
    assert _payload(response) == {
        "ok": True,
        "say": "Записано в быстрые заметки, репозиторий notes.",
        "url": URL,
    }
    validate.assert_awaited_once_with(TOKEN, iwe_extension.NOTES_SHORTCUT_SERVICE)
    save.assert_awaited_once_with(777, "идея про экзоскелет")
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, ""])
async def test_endpoint_without_key_is_rejected(token):
    response, validate, save, _bot = await _post({"text": "x"}, token=token)
    assert response.status == 401
    assert _payload(response)["ok"] is False
    validate.assert_not_awaited()
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_endpoint_with_unknown_key_is_rejected():
    response, _validate, save, _bot = await _post({"text": "x"}, account=None)
    assert response.status == 401
    assert "сири" in _payload(response)["say"]
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_endpoint_rate_limits_the_same_key():
    first, *_ = await _post({"text": "первая"})
    second, _validate, save, _bot = await _post({"text": "вторая"})
    assert first.status == 200
    assert second.status == 429
    save.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("body", "status"), [({"text": "   "}, 400), ({}, 400), ([], 400), ({"text": "x" * 4001}, 413)])
async def test_endpoint_rejects_bad_text(body, status):
    response, _validate, save, _bot = await _post(body)
    assert response.status == status
    assert _payload(response)["say"]
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_endpoint_refuses_chat_without_voice_access(_voice_enabled):
    endpoint_gate, _command_gate = _voice_enabled
    endpoint_gate.return_value = False
    response, _validate, save, _bot = await _post({"text": "x"})
    assert response.status == 403
    assert _payload(response)["ok"] is False
    endpoint_gate.assert_called_once_with(777)
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_endpoint_unlinked_account():
    response, _validate, save, _bot = await _post({"text": "x"}, chat_id=None)
    assert response.status == 404
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_endpoint_reports_refusal_without_chat_echo():
    response, _validate, _save_mock, bot = await _post({"text": "x"}, outcome=NoteOutcome(NoteStatus.NO_GITHUB))
    assert response.status == 200
    payload = _payload(response)
    assert payload["ok"] is False
    assert "гитхаб" in payload["say"]
    bot.send_message.assert_not_awaited()


# ── personal key storage ──────────────────────────────────────────────────────

def _persona_pool(conn):
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


@pytest.mark.asyncio
async def test_notes_key_is_looked_up_only_among_notes_keys():
    conn = MagicMock(fetch=AsyncMock(return_value=[{"account_id": "acc-1", "access_token": TOKEN}]))
    with patch("db.queries.iwe_extension.get_persona_pool", new_callable=AsyncMock, return_value=_persona_pool(conn)):
        account = await iwe_extension.validate_extension_token(TOKEN, iwe_extension.NOTES_SHORTCUT_SERVICE)
    assert account == "acc-1"
    assert conn.fetch.call_args[0][1] == "iwe_notes_shortcut"


@pytest.mark.asyncio
async def test_extension_key_lookup_keeps_its_service_by_default():
    conn = MagicMock(fetch=AsyncMock(return_value=[]))
    with patch("db.queries.iwe_extension.get_persona_pool", new_callable=AsyncMock, return_value=_persona_pool(conn)):
        assert await iwe_extension.validate_extension_token(TOKEN) is None
    assert conn.fetch.call_args[0][1] == "iwe_extension"


@pytest.mark.asyncio
async def test_notes_key_is_issued_with_notes_service_and_scope():
    conn = MagicMock(execute=AsyncMock())
    with patch("db.queries.iwe_extension.get_persona_pool", new_callable=AsyncMock, return_value=_persona_pool(conn)), \
         patch("db.queries.iwe_extension.encrypt_text_token", new_callable=AsyncMock, return_value="pgp:cipher"):
        token = await iwe_extension.generate_extension_token("acc-1", iwe_extension.NOTES_SHORTCUT_SERVICE, scope="notes")
    assert len(token) == 64
    args = conn.execute.call_args[0]
    assert args[1:] == ("acc-1", "pgp:cipher", "iwe_notes_shortcut", "notes")


# ── /siri ─────────────────────────────────────────────────────────────────────

def _siri_message():
    message = MagicMock()
    message.from_user.id = 777
    message.answer = AsyncMock()
    return message


async def _siri(args=None, ory_id="acc-1", existing="old-key", issued="new-key"):
    message = _siri_message()
    with patch("handlers.siri.resolve_ory_id_from_chat", new_callable=AsyncMock, return_value=ory_id), \
         patch("handlers.siri.get_extension_token", new_callable=AsyncMock, return_value=existing) as get_key, \
         patch("handlers.siri.generate_extension_token", new_callable=AsyncMock, return_value=issued) as new_key, \
         patch("handlers.siri.WEBHOOK_URL", "https://bot.example/"):
        await cmd_siri(message, MagicMock(args=args))
    return message, get_key, new_key


@pytest.mark.asyncio
async def test_siri_shows_existing_key_and_steps():
    message, get_key, new_key = await _siri()
    get_key.assert_awaited_once_with("acc-1", "iwe_notes_shortcut")
    new_key.assert_not_awaited()
    text = message.answer.call_args[0][0]
    assert "old-key" in text
    assert "https://bot.example/api/notes" in text
    assert "X-IWE-Api-Key" in text and "Заметка в IWE" in text


@pytest.mark.asyncio
async def test_siri_new_rotates_key():
    message, get_key, new_key = await _siri(args="new")
    get_key.assert_not_awaited()
    new_key.assert_awaited_once_with("acc-1", "iwe_notes_shortcut", scope="notes")
    assert "new-key" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_siri_issues_key_when_none_exists():
    message, _get_key, new_key = await _siri(existing=None)
    new_key.assert_awaited_once()
    assert "new-key" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_siri_unavailable_without_voice_access(_voice_enabled):
    _endpoint_gate, command_gate = _voice_enabled
    command_gate.return_value = False
    message, get_key, new_key = await _siri()
    assert "Siri" in message.answer.call_args[0][0]
    get_key.assert_not_awaited()
    new_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_siri_requires_linked_account():
    message, get_key, new_key = await _siri(ory_id=None)
    assert "/link" in message.answer.call_args[0][0]
    get_key.assert_not_awaited()
    new_key.assert_not_awaited()


def test_setup_text_escapes_html():
    text = setup_text("<k>", "https://x/api/notes?a=1&b=2")
    assert "&lt;k&gt;" in text
    assert "a=1&amp;b=2" in text
