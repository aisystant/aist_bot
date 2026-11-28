"""WP-384: fleeting notes from Siri / iOS Shortcuts.

POST /api/notes with the personal notes key from /siri saves the text like a typed ".text"
note (marked «Siri») and returns a phrase the shortcut speaks back. Covered:
- the endpoint: key check, wrong keys limited per client address, rate limit, empty and
  oversized text, unlinked account, voice access, the spoken phrase (names the notes repo),
  no proactive chat message, the Siri source;
- the key: only a fingerprint is stored, one lookup checks a request, a replaced or
  switched-off key stops working, a key issued before hashing is not valid;
- /siri: the key is shown once, "/siri new" replaces it, "/siri off" switches it off.
"""

import hashlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import oauth_server
from core.fleeting_notes import NoteOutcome, NoteSource, NoteStatus
from db.queries import iwe_extension
from handlers.siri import cmd_siri, setup_text
from oauth_server import notes_shortcut_handler

TOKEN = "a" * 64
URL = "https://github.com/owner/notes/blob/main/fleeting-notes.md"
SAVED = NoteOutcome(NoteStatus.SAVED, url=URL, repo="owner/notes")


@pytest.fixture(autouse=True)
def _fresh_limits():
    oauth_server._notes_rate_limit.clear()
    oauth_server._notes_bad_keys = oauth_server._make_notes_failure_limiter()
    yield
    oauth_server._notes_rate_limit.clear()


@pytest.fixture(autouse=True)
def _voice_enabled():
    with (
        patch(
            "core.voice_access.voice_enabled_for", return_value=True
        ) as endpoint_gate,
        patch("handlers.siri.voice_enabled_for", return_value=True) as command_gate,
    ):
        yield endpoint_gate, command_gate


def _request(body, token=TOKEN, forwarded="203.0.113.7", remote="10.0.0.1"):
    request = MagicMock()
    headers = {"X-Forwarded-For": forwarded} if forwarded is not None else {}
    if token is not None:
        headers["X-IWE-Api-Key"] = token
    request.headers = headers
    request.remote = remote
    request.json = AsyncMock(return_value=body)
    return request


def _payload(response):
    return json.loads(response.text)


async def _post(
    body, token=TOKEN, account="acc-1", chat_id=777, outcome=SAVED, **request_kwargs
):
    bot = MagicMock(send_message=AsyncMock())
    with (
        patch(
            "db.queries.iwe_extension.validate_notes_key",
            new_callable=AsyncMock,
            return_value=account,
        ) as validate,
        patch(
            "db.queries.iwe_extension.get_telegram_id_for_account",
            new_callable=AsyncMock,
            return_value=chat_id,
        ),
        patch(
            "core.fleeting_notes.save_fleeting_note",
            new_callable=AsyncMock,
            return_value=outcome,
        ) as save,
        patch.object(oauth_server, "_bot_instance", bot),
    ):
        response = await notes_shortcut_handler(_request(body, token, **request_kwargs))
    return response, validate, save, bot


# --- POST /api/notes -----------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_saves_a_siri_note_and_names_the_repo():
    response, validate, save, bot = await _post({"text": "  идея про экзоскелет  "})

    assert response.status == 200
    assert _payload(response) == {
        "ok": True,
        "say": "Записано в быстрые заметки, репозиторий notes.",
        "url": URL,
    }
    validate.assert_awaited_once_with(TOKEN)
    save.assert_awaited_once_with(777, "идея про экзоскелет", NoteSource.SIRI)
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
async def test_repeated_wrong_keys_from_one_address_are_blocked_before_any_lookup():
    for _ in range(oauth_server._NOTES_MAX_BAD_KEYS):
        response, *_rest = await _post({"text": "x"}, account=None)
        assert response.status == 401
    response, validate, save, _bot = await _post({"text": "x"}, account=None)
    assert response.status == 429
    validate.assert_not_awaited()
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_blocked_address_does_not_block_other_addresses():
    for _ in range(oauth_server._NOTES_MAX_BAD_KEYS):
        await _post({"text": "x"}, account=None)
    response, *_rest = await _post({"text": "x"}, forwarded="198.51.100.9")
    assert response.status == 200


@pytest.mark.asyncio
async def test_a_valid_key_does_not_count_as_a_failure():
    for i in range(oauth_server._NOTES_MAX_BAD_KEYS + 3):
        oauth_server._notes_rate_limit.clear()
        response, *_rest = await _post({"text": f"заметка {i}"})
        assert response.status == 200


def test_client_address_is_the_last_forwarded_entry_not_a_forged_first_one():
    assert (
        oauth_server._client_address(_request({}, forwarded="6.6.6.6, 203.0.113.7"))
        == "203.0.113.7"
    )
    assert (
        oauth_server._client_address(_request({}, forwarded=None, remote="10.0.0.1"))
        == "10.0.0.1"
    )
    assert (
        oauth_server._client_address(_request({}, forwarded="", remote=None))
        == "unknown"
    )


@pytest.mark.asyncio
async def test_endpoint_rate_limits_the_same_account():
    first, *_ = await _post({"text": "первая"})
    second, _validate, save, _bot = await _post({"text": "вторая"})
    assert first.status == 200
    assert second.status == 429
    save.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "status"),
    [({"text": "   "}, 400), ({}, 400), ([], 400), ({"text": "x" * 4001}, 413)],
)
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
@pytest.mark.parametrize(
    ("status", "words"),
    [
        (NoteStatus.NO_GITHUB, "гитхаб"),
        (NoteStatus.NO_REPO, "репозиторий"),
        (NoteStatus.NO_ACCESS, "подписке"),
        (NoteStatus.RECONNECT, "переподключите"),
        (NoteStatus.FAILED, "Попробуйте"),
    ],
)
async def test_endpoint_speaks_the_reason_a_note_was_not_saved(status, words):
    response, _validate, _save, bot = await _post(
        {"text": "x"}, outcome=NoteOutcome(status)
    )
    assert response.status == 200
    payload = _payload(response)
    assert payload["ok"] is False
    assert words in payload["say"]
    bot.send_message.assert_not_awaited()


# --- the key: only a fingerprint is stored -------------------------------------


def _persona_pool(conn):
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


def test_fingerprint_is_a_prefixed_sha256_and_never_the_key():
    fingerprint = iwe_extension.notes_key_fingerprint(TOKEN)
    assert fingerprint == "sha256:" + hashlib.sha256(TOKEN.encode()).hexdigest()
    assert TOKEN not in fingerprint


@pytest.mark.asyncio
async def test_issue_stores_only_the_fingerprint_and_returns_the_key_once():
    conn = MagicMock(execute=AsyncMock())
    with patch(
        "db.queries.iwe_extension.get_persona_pool",
        new_callable=AsyncMock,
        return_value=_persona_pool(conn),
    ):
        token = await iwe_extension.issue_notes_key("acc-1")
    assert len(token) == 64
    _sql, account, service, stored = conn.execute.call_args[0]
    assert (account, service) == ("acc-1", "iwe_notes_shortcut")
    assert stored == iwe_extension.notes_key_fingerprint(token)
    assert token not in stored
    assert "notes" in conn.execute.call_args[0][0]


@pytest.mark.asyncio
async def test_issue_replaces_the_previous_key_for_the_account():
    conn = MagicMock(execute=AsyncMock())
    with patch(
        "db.queries.iwe_extension.get_persona_pool",
        new_callable=AsyncMock,
        return_value=_persona_pool(conn),
    ):
        first = await iwe_extension.issue_notes_key("acc-1")
        second = await iwe_extension.issue_notes_key("acc-1")
    assert first != second
    assert "ON CONFLICT (account_id, service) DO UPDATE" in conn.execute.call_args[0][0]


@pytest.mark.asyncio
async def test_validate_is_one_lookup_by_the_fingerprint():
    conn = MagicMock(fetchval=AsyncMock(return_value="acc-1"))
    with patch(
        "db.queries.iwe_extension.get_persona_pool",
        new_callable=AsyncMock,
        return_value=_persona_pool(conn),
    ):
        account = await iwe_extension.validate_notes_key(TOKEN)
    assert account == "acc-1"
    conn.fetchval.assert_awaited_once()
    _sql, service, fingerprint = conn.fetchval.call_args[0]
    assert (service, fingerprint) == (
        "iwe_notes_shortcut",
        iwe_extension.notes_key_fingerprint(TOKEN),
    )
    assert "active = TRUE" in conn.fetchval.call_args[0][0]


@pytest.mark.asyncio
async def test_has_key_counts_only_hashed_keys_so_an_old_encrypted_key_gets_replaced():
    conn = MagicMock(fetchval=AsyncMock(return_value=None))
    with patch(
        "db.queries.iwe_extension.get_persona_pool",
        new_callable=AsyncMock,
        return_value=_persona_pool(conn),
    ):
        assert await iwe_extension.has_notes_key("acc-1") is False
    assert conn.fetchval.call_args[0][3] == "sha256:%"


@pytest.mark.asyncio
@pytest.mark.parametrize(("updated", "expected"), [(1, True), (None, False)])
async def test_revoke_switches_the_key_off(updated, expected):
    conn = MagicMock(fetchval=AsyncMock(return_value=updated))
    with patch(
        "db.queries.iwe_extension.get_persona_pool",
        new_callable=AsyncMock,
        return_value=_persona_pool(conn),
    ):
        assert await iwe_extension.revoke_notes_key("acc-1") is expected
    assert "active = FALSE" in conn.fetchval.call_args[0][0]


@pytest.mark.asyncio
async def test_extension_key_functions_keep_their_own_service():
    conn = MagicMock(fetch=AsyncMock(return_value=[]))
    with patch(
        "db.queries.iwe_extension.get_persona_pool",
        new_callable=AsyncMock,
        return_value=_persona_pool(conn),
    ):
        assert await iwe_extension.validate_extension_token(TOKEN) is None
    assert "iwe_extension" in conn.fetch.call_args[0][0]


# --- /siri ---------------------------------------------------------------------


def _siri_message():
    message = MagicMock()
    message.from_user.id = 777
    message.chat.type = "private"
    message.answer = AsyncMock()
    return message


async def _siri(
    args=None,
    ory_id="acc-1",
    has_key=False,
    issued="new-key",
    revoked=True,
    chat_type="private",
):
    message = _siri_message()
    message.chat.type = chat_type
    with (
        patch(
            "handlers.siri.resolve_ory_id_from_chat",
            new_callable=AsyncMock,
            return_value=ory_id,
        ),
        patch(
            "handlers.siri.has_notes_key", new_callable=AsyncMock, return_value=has_key
        ) as has,
        patch(
            "handlers.siri.issue_notes_key", new_callable=AsyncMock, return_value=issued
        ) as issue,
        patch(
            "handlers.siri.revoke_notes_key",
            new_callable=AsyncMock,
            return_value=revoked,
        ) as revoke,
        patch("handlers.siri.WEBHOOK_URL", "https://bot.example/"),
    ):
        await cmd_siri(message, MagicMock(args=args))
    return message, has, issue, revoke


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
@pytest.mark.parametrize("arg", [None, "new", "off"])
async def test_siri_outside_private_chat_does_not_read_or_mutate_keys(chat_type, arg):
    message, has, issue, revoke = await _siri(args=arg, chat_type=chat_type)
    has.assert_not_awaited()
    issue.assert_not_awaited()
    revoke.assert_not_awaited()
    text = message.answer.call_args[0][0]
    assert "личном диалоге" in text and "https://t.me/" in text
    assert "new-key" not in text


@pytest.mark.asyncio
async def test_first_siri_issues_a_key_and_shows_it_once_with_the_steps():
    message, _has, issue, _revoke = await _siri(has_key=False)
    issue.assert_awaited_once_with("acc-1")
    text = message.answer.call_args[0][0]
    assert "new-key" in text and "показан один раз" in text
    assert "https://bot.example/api/notes" in text
    assert "X-IWE-Api-Key" in text and "Заметка в IWE" in text


@pytest.mark.asyncio
async def test_siri_again_does_not_show_the_key_again():
    message, _has, issue, _revoke = await _siri(has_key=True)
    issue.assert_not_awaited()
    text = message.answer.call_args[0][0]
    assert "new-key" not in text
    assert "/siri new" in text and "/siri off" in text
    assert "https://bot.example/api/notes" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("arg", ["new", "NEW", "новый"])
async def test_siri_new_replaces_the_key_and_shows_the_new_one(arg):
    message, has, issue, _revoke = await _siri(args=arg, has_key=True)
    has.assert_not_awaited()
    issue.assert_awaited_once_with("acc-1")
    assert "new-key" in message.answer.call_args[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("arg", ["off", "Выкл", "отключить"])
async def test_siri_off_switches_the_key_off(arg):
    message, _has, issue, revoke = await _siri(args=arg, has_key=True)
    revoke.assert_awaited_once_with("acc-1")
    issue.assert_not_awaited()
    assert "отключены" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_siri_off_without_a_key_says_so():
    message, _has, _issue, _revoke = await _siri(args="off", revoked=False)
    assert "Активного ключа нет" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_siri_unavailable_without_voice_access_shows_the_telegram_id(
    _voice_enabled,
):
    _endpoint_gate, command_gate = _voice_enabled
    command_gate.return_value = False
    message, has, issue, _revoke = await _siri()
    text = message.answer.call_args[0][0]
    assert "<code>777</code>" in text
    has.assert_not_awaited()
    issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_siri_requires_linked_account():
    message, has, issue, _revoke = await _siri(ory_id=None)
    assert "/link" in message.answer.call_args[0][0]
    has.assert_not_awaited()
    issue.assert_not_awaited()


def test_setup_text_escapes_html():
    text = setup_text("<k>", "https://x/api/notes?a=1&b=2")
    assert "&lt;k&gt;" in text
    assert "a=1&amp;b=2" in text
