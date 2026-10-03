"""
WP-578 - the mentorship archive tap must never write participant message text
or Telegram identifiers into the log (PII, B7.3): unhandled worker errors,
exhausted write retries and queue overflow log only types and counts.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path or sys.path.index(_PROJECT_ROOT) > 0:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

import pytest

from engines.mentorship import archive_tap

SECRET_TEXT = "SENTINEL-participant-private-text"
CHAT_ID = -1009876543210
MESSAGE_ID = 777001
USER_ID = 555000111


def _event() -> archive_tap.RawMessageEvent:
    return archive_tap.RawMessageEvent(
        telegram_chat_id=CHAT_ID,
        chat_type="supergroup",
        telegram_message_id=MESSAGE_ID,
        telegram_user_id=USER_ID,
        text=SECRET_TEXT,
        message_at=datetime(2026, 9, 29, tzinfo=timezone.utc),
        is_edit=False,
        reply_to_user_id=None,
        forward_from_user_id=None,
        message_thread_id=None,
        is_reply=False,
        mentioned_user_ids=(),
    )


def _logged(caplog) -> str:
    return "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)


def _assert_clean(caplog):
    logged = _logged(caplog)
    assert logged, "expected at least one log record"
    for leaked in (SECRET_TEXT, str(CHAT_ID), str(MESSAGE_ID), str(USER_ID)):
        assert leaked not in logged


@pytest.fixture
def fresh_queue(monkeypatch):
    queue = asyncio.Queue(maxsize=archive_tap._QUEUE_MAXSIZE)
    monkeypatch.setattr(archive_tap, "_archive_queue", queue, raising=False)
    return queue


@pytest.mark.asyncio
async def test_unhandled_worker_error_logs_no_text_even_in_exception(monkeypatch, caplog, fresh_queue):
    async def boom(event):
        raise RuntimeError(f"Failing row contains ({SECRET_TEXT}, {CHAT_ID})")

    monkeypatch.setattr(archive_tap, "_process_one", boom)
    fresh_queue.put_nowait(_event())
    caplog.set_level(logging.DEBUG)
    task = asyncio.create_task(archive_tap.mentorship_archive_worker())
    await asyncio.wait_for(fresh_queue.join(), timeout=2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    _assert_clean(caplog)
    logged = _logged(caplog)
    assert "RawMessageEvent" in logged and "RuntimeError" in logged
    assert "boom" in logged  # stack frames stay available for diagnosis


@pytest.mark.asyncio
async def test_exhausted_write_retries_log_only_error_type(monkeypatch, caplog):
    monkeypatch.setattr(archive_tap, "_BACKOFF_SECONDS", (0, 0, 0))

    async def failing_write(**kwargs):
        raise ValueError(f"row failed: {SECRET_TEXT}")

    caplog.set_level(logging.DEBUG)
    await archive_tap._write_with_retry(
        write_archive_entry=failing_write,
        telegram_chat_id=CHAT_ID,
        telegram_message_id=MESSAGE_ID,
        text=SECRET_TEXT,
    )
    _assert_clean(caplog)
    assert "ValueError" in _logged(caplog)


def test_queue_overflow_logs_no_chat_id_and_counts_drop(caplog):
    tap = archive_tap._QueueWriterMixin.__new__(archive_tap._QueueWriterMixin)
    tap._queue = asyncio.Queue(maxsize=1)
    tap._queue.put_nowait(_event())
    before = archive_tap._dropped_counters["queue_full"]
    caplog.set_level(logging.DEBUG)
    tap._put(_event())
    _assert_clean(caplog)
    assert archive_tap._dropped_counters["queue_full"] == before + 1


def test_maybe_enqueue_failure_logs_no_text(monkeypatch, caplog):
    tap = archive_tap.ArchiveTapMiddleware()

    def boom(event):
        raise RuntimeError(f"bad: {SECRET_TEXT}")

    monkeypatch.setattr(tap, "_maybe_enqueue", boom)

    async def handler(event, data):
        return "handled"

    caplog.set_level(logging.DEBUG)
    result = asyncio.run(tap(handler, object(), {}))
    assert result == "handled"
    _assert_clean(caplog)
