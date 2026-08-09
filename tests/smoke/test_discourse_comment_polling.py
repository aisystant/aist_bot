"""Регрессии мониторинга комментариев Discourse при HTTP 429/404."""

import asyncio
import importlib
from datetime import datetime, timedelta, timezone

from unittest.mock import AsyncMock, MagicMock

import pytest

from clients.discourse import (
    DiscourseClient,
    DiscourseError,
    DiscourseRateLimitError,
)
from core.error_classifier import classify_error
from db.queries import discourse as discourse_queries


class _Response:
    def __init__(self, status: int, *, headers: dict | None = None, payload=None):
        self.status = status
        self.headers = headers or {}
        self.payload = payload if payload is not None else {}
        self.released = False
        self.json_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def release(self):
        self.released = True

    async def json(self):
        self.json_calls += 1
        return self.payload


class _Session:
    closed = False

    def __init__(self, responses: list[_Response]):
        self.responses = iter(responses)

    async def request(self, *_args, **_kwargs):
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture(autouse=True)
def reset_discourse_scheduler_state(monkeypatch):
    scheduler = importlib.import_module("core.scheduler")
    monkeypatch.setattr(scheduler, "_discourse_rate_limited_until", 0.0)


@pytest.mark.asyncio
async def test_rate_limit_is_not_reported_as_missing_topic(monkeypatch):
    responses = [
        _Response(429, headers={"Retry-After": "0"}),
        _Response(429, headers={"Retry-After": "0"}),
        _Response(429, headers={"Retry-After": "30"}),
    ]
    client = DiscourseClient("https://discourse.invalid", "secret")
    client._session = _Session(responses)
    discourse_module = importlib.import_module("clients.discourse")
    monkeypatch.setattr(discourse_module.asyncio, "sleep", AsyncMock())

    with pytest.raises(DiscourseRateLimitError) as caught:
        await client.get_topic(42)

    assert caught.value.retry_after == 30
    assert responses[0].released is True
    assert responses[1].released is True


@pytest.mark.asyncio
async def test_only_http_404_returns_missing_topic():
    not_found = DiscourseClient("https://discourse.invalid", "secret")
    not_found._session = _Session([_Response(404)])
    assert await not_found.get_topic(42) is None

    unavailable = DiscourseClient("https://discourse.invalid", "secret")
    unavailable._session = _Session([_Response(403)])
    with pytest.raises(DiscourseError, match="HTTP 403"):
        await unavailable.get_topic(42)


@pytest.mark.asyncio
async def test_invalid_success_payload_is_temporary_error():
    invalid = DiscourseClient("https://discourse.invalid", "secret")
    invalid._session = _Session([_Response(200, payload=[])])

    with pytest.raises(DiscourseError, match="invalid payload"):
        await invalid.get_topic(42)


@pytest.mark.asyncio
async def test_empty_success_payload_is_temporary_error():
    invalid = DiscourseClient("https://discourse.invalid", "secret")
    invalid._session = _Session([_Response(200, payload={})])

    with pytest.raises(DiscourseError, match="invalid payload"):
        await invalid.get_topic(42)


@pytest.mark.asyncio
async def test_retry_after_supports_http_date_and_caps_body_value():
    client = DiscourseClient("https://discourse.invalid", "secret")
    future = datetime.now(timezone.utc) + timedelta(seconds=10)
    from_header = _Response(
        429,
        headers={"Retry-After": future.strftime("%a, %d %b %Y %H:%M:%S GMT")},
    )
    from_body = _Response(429, payload={"extras": {"wait_seconds": 3600}})

    header_delay = await client._get_retry_after(from_header, default=5)
    body_delay = await client._get_retry_after(from_body, default=5)

    assert 0 < header_delay <= 10
    assert body_delay == 3600


@pytest.mark.asyncio
async def test_final_generic_429_preserves_response_body():
    response = _Response(429, payload={"errors": ["slow down"]})
    client = DiscourseClient("https://discourse.invalid", "secret")
    client._session = _Session([response])

    result = await client._request_with_retry(
        "GET",
        "https://discourse.invalid/test",
        max_retries=0,
    )

    assert result is response
    assert response.json_calls == 0
    assert response.released is False


@pytest.mark.asyncio
async def test_network_error_retries_then_becomes_typed_error(monkeypatch):
    client = DiscourseClient("https://discourse.invalid", "secret")
    client._session = _Session(
        [asyncio.TimeoutError(), asyncio.TimeoutError(), asyncio.TimeoutError()]
    )
    discourse_module = importlib.import_module("clients.discourse")
    sleep = AsyncMock()
    monkeypatch.setattr(discourse_module.asyncio, "sleep", sleep)

    with pytest.raises(DiscourseError, match="network error"):
        await client.get_topic(42)

    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_successful_check_resets_confirmed_404_counter(monkeypatch):
    pool = AsyncMock()
    monkeypatch.setattr(
        discourse_queries,
        "get_publication_pool",
        AsyncMock(return_value=pool),
    )

    await discourse_queries.update_post_comments_count(42, 7)

    sql, topic_id, posts_count = pool.execute.await_args.args
    assert "comment_check_failures = 0" in sql
    assert (topic_id, posts_count) == (42, 7)


@pytest.mark.asyncio
async def test_polling_prioritizes_oldest_check(monkeypatch):
    pool = AsyncMock()
    pool.fetch.return_value = []
    monkeypatch.setattr(
        discourse_queries,
        "get_publication_pool",
        AsyncMock(return_value=pool),
    )

    assert await discourse_queries.get_posts_for_comment_check() == []

    sql = " ".join(pool.fetch.await_args.args[0].split())
    expected = " ".join(
        """
            SELECT pp.* FROM public.published_post pp
        WHERE pp.discourse_topic_id IS NOT NULL
          AND (
              COALESCE(pp.comment_check_failures, 0) < 3
              OR pp.last_checked_at IS NULL
              OR pp.last_checked_at <= NOW() - INTERVAL '7 days'
          )
        ORDER BY pp.last_checked_at ASC NULLS FIRST, pp.published_at DESC, pp.id ASC
        LIMIT 10
        """.split()
    )
    assert sql == expected


@pytest.mark.asyncio
async def test_confirmed_404_increment_is_atomic_and_advances_queue(monkeypatch):
    pool = AsyncMock()
    monkeypatch.setattr(
        discourse_queries,
        "get_publication_pool",
        AsyncMock(return_value=pool),
    )

    await discourse_queries.increment_comment_check_failures(42)

    sql, topic_id = pool.execute.await_args.args
    assert "COALESCE(comment_check_failures, 0) + 1" in sql
    assert "last_checked_at = clock_timestamp()" in sql
    assert topic_id == 42


def test_discourse_429_has_specific_classifier_before_claude():
    result = classify_error(
        "core.scheduler",
        "[Discourse] Comment polling paused after rate limit; retry_after=30s",
        None,
    )

    assert result["category"] == "scheduler"
    assert result["severity"] == "L1"
    assert "не менять счётчик" in result["action"]

    case_variant = classify_error(
        "core.scheduler",
        "[DISCOURSE] COMMENT POLLING PAUSED AFTER RATE-LIMITED; retry_after=30s",
        None,
    )
    assert case_variant["category"] == "scheduler"


@pytest.mark.asyncio
async def test_scheduler_stops_batch_without_increment_on_rate_limit(monkeypatch):
    scheduler = importlib.import_module("core.scheduler")
    discourse_module = importlib.import_module("clients.discourse")
    queries_module = importlib.import_module("db.queries.discourse")

    fake_client = AsyncMock()
    fake_client.get_topic.side_effect = DiscourseRateLimitError(30)
    monkeypatch.setattr(discourse_module, "discourse", fake_client)

    get_posts = AsyncMock(
        return_value=[
            {"discourse_topic_id": 42, "posts_count": 1, "chat_id": 1},
            {"discourse_topic_id": 43, "posts_count": 1, "chat_id": 1},
        ]
    )
    increment = AsyncMock()
    monkeypatch.setattr(queries_module, "get_posts_for_comment_check", get_posts)
    monkeypatch.setattr(
        queries_module,
        "update_post_comments_count",
        AsyncMock(),
    )
    monkeypatch.setattr(
        queries_module,
        "increment_comment_check_failures",
        increment,
    )

    fake_bot = AsyncMock()
    monkeypatch.setattr(scheduler, "Bot", lambda **_kwargs: fake_bot)

    await scheduler._discourse_check_comments()

    fake_client.get_topic.assert_awaited_once_with(42)
    increment.assert_not_awaited()
    fake_bot.session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduler_cooldown_skips_without_http(monkeypatch):
    scheduler = importlib.import_module("core.scheduler")
    discourse_module = importlib.import_module("clients.discourse")
    fake_client = AsyncMock()
    monkeypatch.setattr(discourse_module, "discourse", fake_client)
    monkeypatch.setattr(
        scheduler,
        "_discourse_rate_limited_until",
        scheduler.time.monotonic() + 60,
    )

    await scheduler._discourse_check_comments()

    fake_client.get_topic.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_lock_causes_early_exit(monkeypatch):
    scheduler = importlib.import_module("core.scheduler")
    unlocked = AsyncMock()
    fake_lock = MagicMock()
    fake_lock.locked.return_value = True
    monkeypatch.setattr(scheduler, "_discourse_check_comments_unlocked", unlocked)
    monkeypatch.setattr(scheduler, "_discourse_comment_poll_lock", fake_lock)

    await scheduler._discourse_check_comments()

    unlocked.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_advances_after_temporary_error_and_continues(monkeypatch):
    scheduler = importlib.import_module("core.scheduler")
    discourse_module = importlib.import_module("clients.discourse")
    queries_module = importlib.import_module("db.queries.discourse")
    monkeypatch.setattr(
        scheduler,
        "_DISCOURSE_COMMENT_REQUEST_INTERVAL_SECONDS",
        0.0,
    )

    fake_client = AsyncMock()
    fake_client.get_topic.side_effect = [
        DiscourseError("HTTP 503"),
        {"posts_count": 1},
    ]
    monkeypatch.setattr(discourse_module, "discourse", fake_client)

    monkeypatch.setattr(
        queries_module,
        "get_posts_for_comment_check",
        AsyncMock(
            return_value=[
                {"discourse_topic_id": 42, "posts_count": 1, "chat_id": 1},
                {"discourse_topic_id": 43, "posts_count": 1, "chat_id": 1},
            ]
        ),
    )
    update = AsyncMock()
    mark_attempt = AsyncMock()
    monkeypatch.setattr(queries_module, "update_post_comments_count", update)
    monkeypatch.setattr(
        queries_module,
        "increment_comment_check_failures",
        AsyncMock(),
    )
    monkeypatch.setattr(
        queries_module,
        "mark_comment_check_attempt",
        mark_attempt,
    )

    fake_bot = AsyncMock()
    monkeypatch.setattr(scheduler, "Bot", lambda **_kwargs: fake_bot)

    await scheduler._discourse_check_comments()

    assert fake_client.get_topic.await_count == 2
    mark_attempt.assert_awaited_once_with(42)
    update.assert_awaited_once_with(43, 1)


@pytest.mark.asyncio
async def test_scheduler_advances_when_comment_count_decreases(monkeypatch):
    scheduler = importlib.import_module("core.scheduler")
    discourse_module = importlib.import_module("clients.discourse")
    queries_module = importlib.import_module("db.queries.discourse")

    fake_client = AsyncMock()
    fake_client.get_topic.return_value = {"posts_count": 1}
    monkeypatch.setattr(discourse_module, "discourse", fake_client)
    monkeypatch.setattr(
        queries_module,
        "get_posts_for_comment_check",
        AsyncMock(
            return_value=[
                {"discourse_topic_id": 42, "posts_count": 2, "chat_id": 1},
            ]
        ),
    )
    update = AsyncMock()
    monkeypatch.setattr(queries_module, "update_post_comments_count", update)
    monkeypatch.setattr(
        queries_module,
        "increment_comment_check_failures",
        AsyncMock(),
    )
    monkeypatch.setattr(
        queries_module,
        "mark_comment_check_attempt",
        AsyncMock(),
    )
    fake_bot = AsyncMock()
    monkeypatch.setattr(scheduler, "Bot", lambda **_kwargs: fake_bot)

    await scheduler._discourse_check_comments()

    update.assert_awaited_once_with(42, 1)


@pytest.mark.asyncio
async def test_scheduler_sends_notification_before_persisting_count(monkeypatch):
    scheduler = importlib.import_module("core.scheduler")
    discourse_module = importlib.import_module("clients.discourse")
    queries_module = importlib.import_module("db.queries.discourse")
    events = []

    fake_client = AsyncMock()
    fake_client.get_topic.return_value = {
        "posts_count": 2,
        "slug": "topic",
    }
    monkeypatch.setattr(discourse_module, "discourse", fake_client)
    monkeypatch.setattr(
        queries_module,
        "get_posts_for_comment_check",
        AsyncMock(
            return_value=[
                {
                    "discourse_topic_id": 42,
                    "posts_count": 1,
                    "chat_id": 1,
                    "title": "Title",
                },
            ]
        ),
    )
    update = AsyncMock(side_effect=lambda *_args: events.append("persist"))
    monkeypatch.setattr(queries_module, "update_post_comments_count", update)
    monkeypatch.setattr(
        queries_module,
        "increment_comment_check_failures",
        AsyncMock(),
    )
    monkeypatch.setattr(
        queries_module,
        "mark_comment_check_attempt",
        AsyncMock(),
    )
    fake_bot = AsyncMock()
    fake_bot.send_message.side_effect = lambda *_args, **_kwargs: events.append("send")
    monkeypatch.setattr(scheduler, "Bot", lambda **_kwargs: fake_bot)

    await scheduler._discourse_check_comments()

    assert events == ["send", "persist"]
