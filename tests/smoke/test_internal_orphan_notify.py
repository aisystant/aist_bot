"""Execute the real HTTP handler without importing unrelated bot startup services.

The handler is called directly with a minimal request object: the release smoke
runner (scripts/run_release_smoke.py) forbids any socket, including 127.0.0.1,
so the tests must not start a local aiohttp server.
"""

import ast
import hashlib
import hmac
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web


@pytest.fixture
async def notify(monkeypatch):
    from core.ops_alert_dedup import reset_ops_alert_dedup

    reset_ops_alert_dedup()
    monkeypatch.setenv("INTERNAL_NOTIFY_SECRET", "synthetic-test-secret")
    monkeypatch.setenv("OPS_ALERT_CHAT_ID", "123")
    source = Path(__file__).resolve().parents[2] / "oauth_server.py"
    tree = ast.parse(source.read_text(), filename=str(source))
    handler = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "internal_notify_handler"
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    namespace = {
        "web": web,
        "logger": logging.getLogger(__name__),
        "_bot_instance": bot,
    }
    exec(
        compile(ast.Module(body=[handler], type_ignores=[]), str(source), "exec"),
        namespace,
    )

    # Resolve the handler at call time so a test can swap `_bot_instance`.
    async def client(request):
        return await namespace["internal_notify_handler"](request)

    yield client, bot, namespace


class Reply:
    """The parts of the handler's web.Response that the tests check."""

    def __init__(self, response):
        self.status = response.status
        self._text = response.text

    async def json(self):
        return json.loads(self._text)


async def post(client, body, signature=None):
    raw = json.dumps(body).encode()
    digest = hmac.new(b"synthetic-test-secret", raw, hashlib.sha256).hexdigest()
    request = SimpleNamespace(
        read=AsyncMock(return_value=raw),
        headers={"X-Notify-Signature": signature or f"sha256={digest}"},
    )
    return Reply(await client(request))


@pytest.mark.asyncio
async def test_acknowledges_telegram_delivery_and_escapes_html(notify):
    client, bot, _ = notify
    response = await post(
        client,
        {
            "type": "installation_orphaned",
            "installation_id": "<123>",
            "github_user_id": "456&789",
        },
    )
    assert response.status == 200
    assert await response.json() == {"ok": True}
    bot.send_message.assert_awaited_once()
    sent = bot.send_message.await_args.kwargs
    assert sent["chat_id"] == 123
    assert sent["parse_mode"] == "HTML"
    assert "&lt;123&gt;" in sent["text"]
    assert "456&amp;789" in sent["text"]
    assert "GitHub-установка не сопоставлена" in sent["text"]
    assert "подключение потерялось" not in sent["text"]


@pytest.mark.asyncio
async def test_invalid_signature_does_not_send(notify):
    client, bot, _ = notify
    response = await post(client, {"type": "installation_orphaned"}, "sha256=invalid")
    assert response.status == 403
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_secret_does_not_send(notify, monkeypatch):
    client, bot, _ = notify
    monkeypatch.delenv("INTERNAL_NOTIFY_SECRET")
    response = await post(client, {"type": "installation_orphaned"})
    assert response.status == 503
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_chat_is_retryable(notify, monkeypatch):
    client, bot, _ = notify
    monkeypatch.delenv("OPS_ALERT_CHAT_ID")
    response = await post(client, {"type": "installation_orphaned"})
    assert response.status == 503
    assert await response.json() == {"ok": False, "reason": "ops_chat_not_configured"}
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_bot_is_retryable(notify):
    client, bot, namespace = notify
    namespace["_bot_instance"] = None
    response = await post(client, {"type": "installation_orphaned"})
    assert response.status == 503
    assert await response.json() == {"ok": False, "reason": "bot_not_ready"}
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_failure_is_not_acknowledged(notify):
    client, bot, _ = notify
    bot.send_message.side_effect = RuntimeError("synthetic delivery failure")
    response = await post(
        client, {"type": "installation_orphaned", "installation_id": 123}
    )
    assert response.status == 500
    assert (await response.json())["ok"] is False
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_type_still_not_acknowledged(notify):
    client, bot, _ = notify
    response = await post(client, {"type": "not_a_notification"})
    assert await response.json() == {"ok": False, "reason": "unknown_type"}
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_is_unknown_and_never_demands_manual_reindex(notify):
    client, bot, _ = notify
    response = await post(
        client,
        {
            "type": "ops_alert",
            "source": "<source>&",
            "failed": [
                {
                    "target": "knowledge",
                    "error": (
                        "timeout 20s: result unknown, job may be queued "
                        "(This operation was aborted)"
                    ),
                }
            ],
        },
    )
    assert response.status == 200
    text = bot.send_message.await_args.kwargs["text"]
    assert "Результат пока неизвестен" in text
    assert "Повторный запуск до проверки не нужен" in text
    assert "&lt;source&gt;&amp;" in text
    assert "Результат пока неизвестен: сервер знаний." in text
    assert "knowledge" not in text
    assert "Провал" not in text
    assert "нужна ручная" not in text
    assert "Запросы завершились с ошибкой" not in text


@pytest.mark.asyncio
async def test_mixed_outcomes_keep_confirmed_error_visible(notify):
    client, bot, _ = notify
    response = await post(
        client,
        {
            "type": "ops_alert",
            "source": "sample",
            "failed": [
                {"target": "slow", "error": "timeout 20000ms: result unknown"},
                {"target": "denied", "error": "HTTP 403 <denied>"},
            ],
        },
    )
    assert response.status == 200
    text = bot.send_message.await_args.kwargs["text"]
    assert "Результат пока неизвестен: slow" in text
    assert "Запросы завершились с ошибкой: denied: HTTP 403 &lt;denied&gt;" in text
    assert "нужна ручная" not in text


@pytest.mark.asyncio
async def test_ops_alert_delivery_failure_is_retryable(notify):
    client, bot, _ = notify
    bot.send_message.side_effect = RuntimeError("synthetic failure")
    response = await post(client, {"type": "ops_alert", "failed": []})
    assert response.status == 500
    assert (await response.json())["ok"] is False


def test_reindex_alert_bounds_text_before_html_escaping():
    from core.reindex_alert import render_reindex_alert

    text = render_reindex_alert(
        {
            "source": "<&" * 500,
            "failed": [{"target": "x" * 500, "error": "<&" * 500} for _ in range(10)],
        }
    )
    # Telegram limits parsed text; the source HTML may be longer after escaping.
    import html

    assert len(html.unescape(text).encode("utf-16-le")) // 2 < 4096
    assert text.count("ещё 7") == 1
    assert "&lt" not in text.replace("&lt;", "")
    assert "&amp" not in text.replace("&amp;", "")


@pytest.mark.parametrize("failed", [None, {}, "unstructured", [None, 42]])
def test_missing_outcome_is_unknown(failed):
    from core.reindex_alert import render_reindex_alert

    text = render_reindex_alert({"failed": failed})
    assert "Результат пока неизвестен" in text
    assert "Запросы завершились с ошибкой" not in text


@pytest.mark.parametrize(
    "error",
    [
        "timeout unknown",
        "timeout 20 sec: result unknown",
        "some text timeout 20s: result unknown",
        "HTTP 504",
    ],
)
def test_timeout_classification_does_not_guess_other_formats(error):
    from core.reindex_alert import render_reindex_alert

    text = render_reindex_alert({"failed": [{"target": "example", "error": error}]})
    assert "Запросы завершились с ошибкой" in text
    assert "Результат пока неизвестен" not in text



@pytest.mark.asyncio
async def test_orphan_is_deduped_by_installation_and_names_added_action(notify):
    client, bot, _ = notify
    body = {
        "type": "installation_orphaned",
        "installation_id": 123542883,
        "github_user_id": 46213661,
        "action": "added",
    }
    first = await post(client, body)
    second = await post(client, body)
    assert first.status == 200
    assert await second.json() == {"ok": True, "deduped": True}
    bot.send_message.assert_awaited_once()
    assert "Событие: добавление репозитория" in bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_openrouter_401_is_one_alert_across_sources(notify):
    client, bot, _ = notify
    for source in ("PACK-one", "PACK-two"):
        response = await post(
            client,
            {
                "type": "ops_alert",
                "source": source,
                "failed": [{"target": "knowledge", "error": "OpenRouter embeddings error: 401"}],
            },
        )
        assert response.status == 200
    bot.send_message.assert_awaited_once()
    text = bot.send_message.await_args.kwargs["text"]
    assert "OpenRouter отклонил авторизацию (401)" in text
    assert "Повторная переиндексация до восстановления доступа не нужна" in text


@pytest.mark.asyncio
async def test_telegram_failure_releases_alert_reservation(notify):
    client, bot, _ = notify
    body = {
        "type": "ops_alert",
        "source": "PACK-one",
        "failed": [{"target": "knowledge", "error": "OpenRouter embeddings error: 401"}],
    }
    bot.send_message.side_effect = [RuntimeError("temporary"), None]
    failed = await post(client, body)
    retried = await post(client, body)
    assert failed.status == 500
    assert retried.status == 200
    assert bot.send_message.await_count == 2



@pytest.mark.asyncio
async def test_concurrent_duplicate_waits_for_confirmed_delivery(notify):
    import asyncio

    client, bot, _ = notify
    started = asyncio.Event()
    finish = asyncio.Event()

    async def gated_send(**kwargs):
        started.set()
        await finish.wait()

    bot.send_message.side_effect = gated_send
    body = {
        "type": "ops_alert",
        "source": "PACK-one",
        "failed": [{"target": "knowledge", "error": "OpenRouter embeddings error: 401"}],
    }
    first = asyncio.create_task(post(client, body))
    await started.wait()
    second = asyncio.create_task(post(client, body))
    await asyncio.sleep(0)
    assert not second.done()
    finish.set()
    assert (await first).status == 200
    assert await (await second).json() == {"ok": True, "deduped": True}
    assert bot.send_message.await_count == 1



@pytest.mark.asyncio
async def test_second_request_sends_after_first_delivery_fails(notify):
    import asyncio

    client, bot, _ = notify
    bot.send_message.side_effect = [RuntimeError("temporary"), None]
    body = {
        "type": "ops_alert",
        "source": "PACK-one",
        "failed": [{"target": "knowledge", "error": "OpenRouter embeddings error: 401"}],
    }
    first, second = await asyncio.gather(post(client, body), post(client, body))
    assert {first.status, second.status} == {200, 500}
    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_cancellation_releases_single_flight(notify):
    import asyncio

    client, bot, _ = notify
    started = asyncio.Event()

    async def blocked_send(**kwargs):
        started.set()
        await asyncio.Event().wait()

    bot.send_message.side_effect = blocked_send
    body = {
        "type": "ops_alert",
        "source": "PACK-one",
        "failed": [{"target": "knowledge", "error": "OpenRouter embeddings error: 401"}],
    }
    cancelled = asyncio.create_task(post(client, body))
    await started.wait()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    bot.send_message.side_effect = None
    response = await post(client, body)
    assert response.status == 200


@pytest.mark.asyncio
async def test_stale_release_does_not_release_new_reservation():
    from core.ops_alert_dedup import _PENDING, release_reservation, reserve_reindex_alert, reset_ops_alert_dedup

    reset_ops_alert_dedup()
    body = {"source": "PACK-one", "failed": []}
    accepted, key, first = await reserve_reindex_alert(body)
    assert accepted
    release_reservation(key, first)
    accepted, key, second = await reserve_reindex_alert(body)
    assert accepted
    release_reservation(key, first)
    assert _PENDING[key] is second
    release_reservation(key, second)
    assert key not in _PENDING
