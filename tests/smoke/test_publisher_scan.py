"""Регрессии недельного backfill и уведомлений публикатора (WP-502)."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from clients import github_content
from config import settings
from core import scheduler
from db.queries import discourse as discourse_queries
from db.queries import github as github_queries
from db.queries import users as user_queries


SYNTHETIC_CHAT_ID = 900001
SYNTHETIC_CATEGORY_ID = 7


class _AcquireConnection:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return None


def _post(*, year: int, title: str, status: str = "ready") -> dict:
    today = datetime.now().date()
    month, day = (today.month, today.day) if year == today.year else (1, 1)
    path = (
        f"docs/{year}/{year}-{month:02d}-{day:02d}-"
        f"{title.lower().replace(' ', '-')}.md"
    )
    content = (
        "---\n"
        "type: post\n"
        f"title: {title}\n"
        f"status: {status}\n"
        "target: club\n"
        "tags: []\n"
        "---\n"
        "Synthetic body.\n"
    )
    return {"name": path.rsplit("/", 1)[-1], "path": path, "content": content}


def _install_scan_harness(
    monkeypatch,
    *,
    posts: list[dict],
    schedule_results=(),
    queue_count: int = 0,
):
    current_year = datetime.now().year
    files_by_directory: dict[str, list[dict]] = {}
    content_by_path: dict[str, str] = {}
    for post in posts:
        directory = post["path"].rsplit("/", 1)[0]
        files_by_directory.setdefault(directory, []).append(
            {"name": post["name"], "path": post["path"]}
        )
        content_by_path[post["path"]] = post["content"]

    async def list_dirs(_path):
        return []

    async def list_files(path):
        return files_by_directory.get(path, [])

    async def read_file(path):
        content = content_by_path.get(path)
        return (content, "synthetic-sha") if content is not None else None

    client = SimpleNamespace(
        list_dirs=AsyncMock(side_effect=list_dirs),
        list_files=AsyncMock(side_effect=list_files),
        read_file=AsyncMock(side_effect=read_file),
        close=AsyncMock(),
    )
    bot = SimpleNamespace(
        send_message=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )

    mocks = {
        "get_users": AsyncMock(
            return_value=[
                {
                    "chat_id": SYNTHETIC_CHAT_ID,
                    "access_token": "synthetic-token",
                    "knowledge_repo": "fixture/knowledge",
                }
            ]
        ),
        "get_accounts": AsyncMock(
            return_value=[
                {
                    "chat_id": SYNTHETIC_CHAT_ID,
                    "blog_category_id": SYNTHETIC_CATEGORY_ID,
                }
            ]
        ),
        "published_files": AsyncMock(return_value=set()),
        "published_titles": AsyncMock(return_value=set()),
        "scheduled_files": AsyncMock(return_value=set()),
        "scheduled_titles": AsyncMock(return_value=set()),
        "scheduled_count": AsyncMock(return_value=queue_count),
        "scheduled_dates": AsyncMock(return_value=set()),
        "schedule": AsyncMock(side_effect=list(schedule_results)),
    }

    monkeypatch.setattr(scheduler, "Bot", lambda **_kwargs: bot)
    monkeypatch.setattr(scheduler, "_bot_token", "synthetic-bot-token")
    monkeypatch.setattr(github_content, "create_content_client", lambda *_args: client)
    monkeypatch.setattr(github_queries, "get_users_with_knowledge_repo", mocks["get_users"])
    monkeypatch.setattr(discourse_queries, "get_all_discourse_accounts", mocks["get_accounts"])
    monkeypatch.setattr(
        discourse_queries,
        "get_all_published_source_files",
        mocks["published_files"],
    )
    monkeypatch.setattr(
        discourse_queries,
        "get_all_published_titles_lower",
        mocks["published_titles"],
    )
    monkeypatch.setattr(
        discourse_queries,
        "get_all_scheduled_source_files",
        mocks["scheduled_files"],
    )
    monkeypatch.setattr(
        discourse_queries,
        "get_all_scheduled_titles_lower",
        mocks["scheduled_titles"],
    )
    monkeypatch.setattr(discourse_queries, "get_scheduled_count", mocks["scheduled_count"])
    monkeypatch.setattr(discourse_queries, "get_scheduled_dates", mocks["scheduled_dates"])
    monkeypatch.setattr(discourse_queries, "schedule_publication", mocks["schedule"])
    monkeypatch.setattr(user_queries, "moscow_now", lambda: datetime(current_year, 9, 2, 9, 0))
    monkeypatch.setattr(settings, "PUBLISHER_DAYS", "tue,wed,thu,fri,sat,sun")
    monkeypatch.setattr(settings, "PUBLISHER_TIME", "10:00")
    monkeypatch.setattr(settings, "PUBLISHER_INTERVAL", 1)
    monkeypatch.setattr(settings, "PUBLISHER_MIN_QUEUE", 2)

    return SimpleNamespace(bot=bot, client=client, mocks=mocks, current_year=current_year)


@pytest.mark.asyncio
async def test_notify_false_still_confirms_scheduled_backfill_without_queue_watch(monkeypatch):
    old_post = _post(year=datetime.now().year - 1, title="Recovered post")
    harness = _install_scan_harness(monkeypatch, posts=[old_post], schedule_results=[101])

    await scheduler._smart_publisher_scan_unlocked(notify=False, backfill=True)

    harness.mocks["schedule"].assert_awaited_once()
    harness.bot.send_message.assert_awaited_once()
    chat_id, message = harness.bot.send_message.await_args.args
    assert chat_id == SYNTHETIC_CHAT_ID
    assert message.startswith("Добавлено в график публикаций (1):")
    assert "Recovered post" in message
    assert "В очереди:" not in message
    assert "В очереди публикаций:" not in message
    assert "Нужны новые посты" not in message


@pytest.mark.asyncio
async def test_notify_false_with_no_candidates_sends_nothing(monkeypatch):
    draft = _post(year=datetime.now().year - 1, title="Draft post", status="draft")
    harness = _install_scan_harness(monkeypatch, posts=[draft])

    await scheduler._smart_publisher_scan_unlocked(notify=False, backfill=True)

    harness.client.read_file.assert_awaited_once()
    harness.mocks["scheduled_titles"].assert_awaited_once_with(SYNTHETIC_CHAT_ID)
    harness.mocks["schedule"].assert_not_awaited()
    harness.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_true_with_low_queue_sends_queue_watch(monkeypatch):
    draft = _post(year=datetime.now().year, title="Useful draft", status="draft")
    harness = _install_scan_harness(monkeypatch, posts=[draft], queue_count=0)

    await scheduler._smart_publisher_scan_unlocked(notify=True, backfill=False)

    harness.mocks["schedule"].assert_not_awaited()
    harness.bot.send_message.assert_awaited_once()
    chat_id, message = harness.bot.send_message.await_args.args
    assert chat_id == SYNTHETIC_CHAT_ID
    assert "В очереди публикаций: 0 (мин. 2)." in message
    assert "Нужны новые посты" in message
    assert "Useful draft" in message


@pytest.mark.asyncio
async def test_notify_true_sends_confirmation_and_queue_watch(monkeypatch):
    ready = _post(year=datetime.now().year, title="Ready post")
    harness = _install_scan_harness(
        monkeypatch,
        posts=[ready],
        schedule_results=[102],
        queue_count=0,
    )

    await scheduler._smart_publisher_scan_unlocked(notify=True, backfill=False)

    assert harness.bot.send_message.await_count == 2
    messages = [call.args[1] for call in harness.bot.send_message.await_args_list]
    confirmations = [
        message
        for message in messages
        if message.startswith("Добавлено в график публикаций (1):")
    ]
    assert len(confirmations) == 1
    assert sum("В очереди: 0 (мин. 2)." in message for message in messages) == 1


@pytest.mark.asyncio
async def test_concurrent_scans_skip_second_unlocked_run(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_scan(**_kwargs):
        started.set()
        await release.wait()

    unlocked = AsyncMock(side_effect=blocking_scan)
    monkeypatch.setattr(scheduler, "_publisher_scan_lock", asyncio.Lock())
    monkeypatch.setattr(scheduler, "_smart_publisher_scan_unlocked", unlocked)

    first = asyncio.create_task(scheduler._smart_publisher_scan(notify=False, backfill=True))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.wait_for(
            scheduler._smart_publisher_scan(notify=True, backfill=False),
            timeout=5,
        )
        unlocked.assert_awaited_once_with(notify=False, backfill=True)
    finally:
        release.set()
        await first

    unlocked.assert_awaited_once()


@pytest.mark.asyncio
async def test_schedule_publication_conflict_returns_none_without_lookup(monkeypatch):
    connection = SimpleNamespace(fetchrow=AsyncMock(return_value=None))
    pool = SimpleNamespace(acquire=lambda: _AcquireConnection(connection))
    monkeypatch.setattr(
        discourse_queries,
        "get_publication_pool",
        AsyncMock(return_value=pool),
    )

    result = await discourse_queries.schedule_publication(
        chat_id=SYNTHETIC_CHAT_ID,
        title="Duplicate",
        raw="Synthetic body",
        category_id=SYNTHETIC_CATEGORY_ID,
        schedule_time=datetime(2026, 9, 3, 10, 0),
        source_file="docs/2026/duplicate.md",
    )

    assert result is None
    connection.fetchrow.assert_awaited_once()
    assert "ON CONFLICT" in connection.fetchrow.await_args.args[0]


@pytest.mark.asyncio
async def test_mixed_new_and_duplicate_candidates_confirm_only_new(monkeypatch):
    year = datetime.now().year
    new_post = _post(year=year, title="New post")
    duplicate = _post(year=year, title="Duplicate post")
    harness = _install_scan_harness(
        monkeypatch,
        posts=[new_post, duplicate],
        schedule_results=[103, None],
    )

    await scheduler._smart_publisher_scan_unlocked(notify=False, backfill=False)

    assert harness.mocks["schedule"].await_count == 2
    harness.bot.send_message.assert_awaited_once()
    message = harness.bot.send_message.await_args.args[1]
    assert "Добавлено в график публикаций (1):" in message
    assert "New post" in message
    assert "Duplicate post" not in message
