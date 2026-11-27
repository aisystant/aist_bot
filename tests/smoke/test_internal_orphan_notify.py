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
    from core import reindex_alert

    monkeypatch.setattr(
        reindex_alert, "write_reindex_center", AsyncMock(return_value=True)
    )
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
    from core import reindex_alert

    client, bot, _ = notify
    body = {
        "type": "ops_alert",
        "source": "PACK-personal",
        "failed": [{"target": "knowledge", "error": "timeout 20s: result unknown"}],
    }
    response = await post(client, body)
    assert response.status == 200
    assert await response.json() == {"ok": True, "route": "digest"}
    reindex_alert.write_reindex_center.assert_awaited_once_with(body)
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_mixed_outcomes_keep_confirmed_error_visible(notify):
    from core import reindex_alert

    client, bot, _ = notify
    body = {
        "type": "ops_alert",
        "source": "sample",
        "failed": [
            {"target": "slow", "error": "timeout 20s: result unknown"},
            {"target": "server", "error": "HTTP 500"},
        ],
    }
    response = await post(client, body)
    assert await response.json() == {"ok": True, "route": "digest"}
    reindex_alert.write_reindex_center.assert_awaited_once_with(body)
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_ops_alert_delivery_failure_is_retryable(notify):
    from core import reindex_alert

    reindex_alert.write_reindex_center.return_value = False
    client, bot, _ = notify
    response = await post(client, {"type": "ops_alert", "failed": []})
    assert response.status == 503
    assert await response.json() == {"ok": False, "reason": "center_unavailable"}
    bot.send_message.assert_not_awaited()


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
    assert (
        "Событие: добавление репозитория" in bot.send_message.await_args.kwargs["text"]
    )


# --- WP-568, 16.11: an unknown reindex result must not repeat in chat every hour ---

_UNKNOWN = {
    "target": "knowledge",
    "error": "timeout 20s: result unknown, job may be queued",
}


def test_unknown_result_text_names_the_action_and_hides_nothing_new():
    from core.reindex_alert import render_reindex_alert

    text = render_reindex_alert(
        {"source": "PACK-digital-platform", "failed": [_UNKNOWN]}
    )
    assert "Что делать: пока ничего." in text
    assert "скажите агенту «проверь итог переиндексации PACK-digital-platform»" in text
    assert "Повторный запуск до проверки не нужен" in text
    assert "ещё" not in text  # no repeat line when nothing was suppressed


@pytest.mark.parametrize(
    "count, phrase",
    [
        (1, "ещё 1 такое сообщение"),
        (2, "ещё 2 таких сообщения"),
        (5, "ещё 5 таких сообщений"),
        (11, "ещё 11 таких сообщений"),
        (21, "ещё 21 такое сообщение"),
        (24, "ещё 24 таких сообщения"),
    ],
)
def test_repeat_line_agrees_with_the_number(count, phrase):
    from core.reindex_alert import render_reindex_alert

    assert phrase in render_reindex_alert({"source": "S", "failed": [_UNKNOWN]}, count)


def test_mixed_unknown_and_error_never_says_nothing_to_do():
    from core.reindex_alert import render_reindex_alert

    text = render_reindex_alert(
        {
            "source": "S",
            "failed": [_UNKNOWN, {"target": "personal", "error": "HTTP 403 denied"}],
        }
    )
    assert "Запросы завершились с ошибкой" in text
    assert "Что делать: пока ничего" not in text


def test_action_line_without_source_name_has_no_dangling_question_mark():
    from core.reindex_alert import render_reindex_alert

    text = render_reindex_alert({"failed": [_UNKNOWN]})
    assert "«проверь итог переиндексации»" in text
    assert "?" not in text.split("Что делать:")[1]


@pytest.mark.asyncio
async def test_four_today_requests_reach_digest_and_never_chat(notify):
    from core import reindex_alert

    client, bot, _ = notify
    for source in (
        "PACK-digital-platform",
        "FMT-exocortex-template",
        "PACK-personal",
        "FMT-exocortex-template",
    ):
        response = await post(
            client,
            {
                "type": "ops_alert",
                "source": source,
                "failed": [
                    {"target": "knowledge", "error": "timeout 20s: result unknown"}
                ],
            },
        )
        assert await response.json() == {"ok": True, "route": "digest"}
    assert reindex_alert.write_reindex_center.await_count == 4
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_actions_share_a_persistent_daily_queue_key(notify, monkeypatch):
    from core import notification_service

    queue = AsyncMock(return_value={"status": "queued"})
    monkeypatch.setattr(notification_service, "enqueue", queue)
    client, bot, _ = notify
    for source in ("PACK-personal", "FMT-exocortex-template"):
        response = await post(
            client,
            {
                "type": "ops_alert",
                "source": source,
                "failed": [
                    {"target": "knowledge", "error": "OpenRouter embeddings error: 401"}
                ],
            },
        )
        assert response.status == 200
    calls = queue.await_args_list
    assert calls[0].kwargs["dedup_key"] == calls[1].kwargs["dedup_key"]
    assert calls[0].kwargs["dedup_hours"] == 24
    assert calls[0].kwargs["journal_key"] == calls[0].kwargs["dedup_key"]
    assert "Что делать: проверить действительность ключа" in calls[0].args[2]["text"]
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_center_retries_the_same_event_without_losing_request_outcome(
    monkeypatch,
):
    from core.reindex_alert import write_reindex_center
    import aiohttp

    for name in ("URL", "USER", "TOKEN"):
        monkeypatch.setenv(
            "REINDEX_CENTER_LOKI_" + name,
            "https://center.example" if name == "URL" else "synthetic",
        )
    posts = []
    statuses = iter((503, 204))

    class Response:
        async def __aenter__(self):
            self.status = next(statuses)
            return self

        async def __aexit__(self, *args):
            return False

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, **kwargs):
            posts.append(kwargs["json"])
            return Response()

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    assert await write_reindex_center(
        {"source": "FMT", "failed": [{"error": "timeout 20s: result unknown"}]}
    )
    assert len(posts) == 2 and posts[0] == posts[1]
    event = json.loads(posts[0]["streams"][0]["values"][0][1])
    assert event["request_result"] == "unknown"
    assert event["phase"] == "request"


@pytest.mark.asyncio
async def test_installation_single_flight_survives_failed_delivery(notify):
    import asyncio
    from core.ops_alert_dedup import (
        reserve_installation_orphaned,
        release_reservation,
        mark_alert_sent,
    )

    body = {"installation_id": "42"}
    _, key, first = await reserve_installation_orphaned(body)
    waiting = asyncio.create_task(reserve_installation_orphaned(body))
    await asyncio.sleep(0)
    assert not waiting.done()
    release_reservation(key, first)
    accepted, _, second = await waiting
    assert accepted
    mark_alert_sent(key, second)
    accepted, _, _ = await reserve_installation_orphaned(body)
    assert not accepted


def test_unspecified_http_refusal_does_not_invent_a_github_action():
    from core.reindex_alert import reindex_action

    assert not reindex_action({"failed": [{"target": "workbook", "error": "HTTP 403"}]})


@pytest.mark.asyncio
async def test_retry_after_queue_failure_retains_the_producer_event_id(
    notify, monkeypatch
):
    from core import reindex_alert, notification_service

    queue = AsyncMock(
        side_effect=[RuntimeError("queue unavailable"), {"status": "queued"}]
    )
    monkeypatch.setattr(notification_service, "enqueue", queue)
    client, bot, _ = notify
    body = {
        "type": "ops_alert",
        "event_id": "test-source-event-1",
        "source": "FMT",
        "failed": [{"error": "OpenRouter embeddings error: 401"}],
    }
    first = await post(client, body)
    second = await post(client, body)
    assert first.status == 503 and second.status == 200
    assert (
        reindex_alert.write_reindex_center.await_args_list[0].args[0]["event_id"]
        == body["event_id"]
    )
    assert (
        reindex_alert.write_reindex_center.await_args_list[1].args[0]["event_id"]
        == body["event_id"]
    )
    assert (
        queue.await_args_list[0].kwargs["dedup_key"]
        == queue.await_args_list[1].kwargs["dedup_key"]
    )
    bot.send_message.assert_not_awaited()
