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


# --- WP-568, 16.11: an unknown reindex result must not repeat in chat every hour ---

_UNKNOWN = {"target": "knowledge", "error": "timeout 20s: result unknown, job may be queued"}


@pytest.mark.asyncio
async def test_unknown_result_window_spans_hour_boundaries_and_error_text(monkeypatch):
    import core.ops_alert_dedup as dedup

    dedup.reset_ops_alert_dedup()
    clock = {"now": 1_800_000_000.0}
    monkeypatch.setattr(dedup.time, "time", lambda: clock["now"])

    body = {"source": "FMT-exocortex-template", "failed": [_UNKNOWN]}
    accepted, key, token = await dedup.reserve_reindex_alert(body)
    assert accepted
    dedup.mark_alert_sent(key, token)

    # 58 minutes later (the next hour bucket) and with a different timeout text: still the same signal.
    clock["now"] += 58 * 60
    later = {"source": "FMT-exocortex-template", "failed": [{"target": "knowledge", "error": "timeout 30000ms: result unknown"}]}
    accepted, _, _ = await dedup.reserve_reindex_alert(later)
    assert not accepted
    assert dedup.suppressed_count(key) == 1

    # Another source is a different signal.
    accepted, other_key, other_token = await dedup.reserve_reindex_alert({"source": "PACK-digital-platform", "failed": [_UNKNOWN]})
    assert accepted and other_key != key
    dedup.release_reservation(other_key, other_token)


@pytest.mark.asyncio
async def test_unknown_result_comes_back_after_window_with_repeat_count(monkeypatch):
    import core.ops_alert_dedup as dedup
    from core.reindex_alert import render_reindex_alert

    dedup.reset_ops_alert_dedup()
    clock = {"now": 1_800_000_000.0}
    monkeypatch.setattr(dedup.time, "time", lambda: clock["now"])
    body = {"source": "PACK-digital-platform", "failed": [_UNKNOWN]}

    accepted, key, token = await dedup.reserve_reindex_alert(body)
    dedup.mark_alert_sent(key, token)
    for _ in range(3):
        clock["now"] += 3600
        accepted, _, _ = await dedup.reserve_reindex_alert(body)
        assert not accepted

    clock["now"] += dedup.UNKNOWN_RESULT_WINDOW_SECONDS
    accepted, key, token = await dedup.reserve_reindex_alert(body)
    assert accepted
    text = render_reindex_alert(body, dedup.suppressed_count(key))
    assert "ещё 3 таких сообщения" in text
    dedup.mark_alert_sent(key, token)
    assert dedup.suppressed_count(key) == 0


@pytest.mark.asyncio
async def test_confirmed_error_is_not_swallowed_by_unknown_window():
    import core.ops_alert_dedup as dedup

    dedup.reset_ops_alert_dedup()
    unknown = {"source": "PACK-digital-platform", "failed": [_UNKNOWN]}
    accepted, key, token = await dedup.reserve_reindex_alert(unknown)
    dedup.mark_alert_sent(key, token)

    confirmed = {"source": "PACK-digital-platform", "failed": [{"target": "knowledge", "error": "HTTP 403 denied"}]}
    accepted, error_key, error_token = await dedup.reserve_reindex_alert(confirmed)
    assert accepted and error_key != key
    dedup.release_reservation(error_key, error_token)


def test_unknown_result_text_names_the_action_and_hides_nothing_new():
    from core.reindex_alert import render_reindex_alert

    text = render_reindex_alert({"source": "PACK-digital-platform", "failed": [_UNKNOWN]})
    assert "Что делать: пока ничего." in text
    assert "скажите агенту «проверь итог переиндексации PACK-digital-platform»" in text
    assert "Повторный запуск до проверки не нужен" in text
    assert "ещё" not in text  # no repeat line when nothing was suppressed


@pytest.mark.parametrize(
    "count, phrase",
    [(1, "ещё 1 такое сообщение"), (2, "ещё 2 таких сообщения"), (5, "ещё 5 таких сообщений"), (11, "ещё 11 таких сообщений"), (21, "ещё 21 такое сообщение"), (24, "ещё 24 таких сообщения")],
)
def test_repeat_line_agrees_with_the_number(count, phrase):
    from core.reindex_alert import render_reindex_alert

    assert phrase in render_reindex_alert({"source": "S", "failed": [_UNKNOWN]}, count)


@pytest.mark.asyncio
async def test_handler_shows_suppressed_repeats_on_the_next_delivered_message(notify, monkeypatch):
    """End to end through /internal/notify: repeats are swallowed, then counted in the next chat message."""
    import core.ops_alert_dedup as dedup

    client, bot, _ = notify
    clock = {"now": 1_800_000_000.0}
    monkeypatch.setattr(dedup.time, "time", lambda: clock["now"])
    body = {"type": "ops_alert", "source": "PACK-digital-platform", "failed": [_UNKNOWN]}

    first = await post(client, body)
    assert first.status == 200 and bot.send_message.await_count == 1
    assert "ещё" not in bot.send_message.await_args.kwargs["text"]

    for _ in range(2):
        clock["now"] += 40 * 60
        again = await post(client, body)
        assert (await again.json()) == {"ok": True, "deduped": True}
    assert bot.send_message.await_count == 1  # nothing new reached the chat

    clock["now"] += dedup.UNKNOWN_RESULT_WINDOW_SECONDS
    later = await post(client, body)
    assert later.status == 200 and bot.send_message.await_count == 2
    assert "ещё 2 таких сообщения" in bot.send_message.await_args.kwargs["text"]


def test_mixed_unknown_and_error_never_says_nothing_to_do():
    from core.reindex_alert import render_reindex_alert

    text = render_reindex_alert(
        {"source": "S", "failed": [_UNKNOWN, {"target": "personal", "error": "HTTP 403 denied"}]}
    )
    assert "Запросы завершились с ошибкой" in text
    assert "Что делать: пока ничего" not in text


@pytest.mark.asyncio
async def test_payload_without_source_has_no_shared_window():
    import core.ops_alert_dedup as dedup

    dedup.reset_ops_alert_dedup()
    nameless = {"failed": [_UNKNOWN]}
    accepted, key, token = await dedup.reserve_reindex_alert(nameless)
    assert accepted and not key.startswith("reindex-unknown:")
    dedup.mark_alert_sent(key, token)
    # a different nameless payload (other target) is a different signal
    other = {"failed": [{"target": "personal", "error": "timeout 20s: result unknown"}]}
    accepted, other_key, other_token = await dedup.reserve_reindex_alert(other)
    assert accepted
    dedup.release_reservation(other_key, other_token)


@pytest.mark.asyncio
async def test_expired_dedup_state_is_pruned(monkeypatch):
    import core.ops_alert_dedup as dedup

    dedup.reset_ops_alert_dedup()
    clock = {"now": 1_800_000_000.0}
    monkeypatch.setattr(dedup.time, "time", lambda: clock["now"])
    for hour in range(5):
        accepted, key, token = await dedup.reserve_reindex_alert({"source": f"S{hour}", "failed": [{"target": "t", "error": f"HTTP {400 + hour}"}]})
        dedup.mark_alert_sent(key, token)
        clock["now"] += 3600
    assert len(dedup._LAST_SENT) == 5
    dedup._SUPPRESSED["reindex:gone:1:abc"] = 3
    clock["now"] += 2 * 86_400
    accepted, key, token = await dedup.reserve_reindex_alert({"source": "fresh", "failed": []})
    assert accepted
    assert not dedup._LAST_SENT and "reindex:gone:1:abc" not in dedup._SUPPRESSED
    dedup.release_reservation(key, token)


@pytest.mark.asyncio
async def test_repeat_counter_survives_a_gap_longer_than_a_day(monkeypatch):
    import core.ops_alert_dedup as dedup
    from core.reindex_alert import render_reindex_alert

    dedup.reset_ops_alert_dedup()
    clock = {"now": 1_800_000_000.0}
    monkeypatch.setattr(dedup.time, "time", lambda: clock["now"])
    body = {"source": "PACK-digital-platform", "failed": [_UNKNOWN]}
    accepted, key, token = await dedup.reserve_reindex_alert(body)
    dedup.mark_alert_sent(key, token)
    for _ in range(5):
        clock["now"] += 3600
        accepted, _, _ = await dedup.reserve_reindex_alert(body)
        assert not accepted
    clock["now"] += 25 * 3600
    accepted, key, token = await dedup.reserve_reindex_alert(body)
    assert accepted
    assert "ещё 5 таких сообщений" in render_reindex_alert(body, dedup.suppressed_count(key))
    dedup.release_reservation(key, token)


def test_action_line_without_source_name_has_no_dangling_question_mark():
    from core.reindex_alert import render_reindex_alert

    text = render_reindex_alert({"failed": [_UNKNOWN]})
    assert "«проверь итог переиндексации»" in text
    assert "?" not in text.split("Что делать:")[1]
