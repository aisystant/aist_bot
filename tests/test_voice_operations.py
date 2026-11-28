"""Real isolated PostgreSQL: concurrent claims, restart/replay, fencing and runtime grants.

RUN_LOCAL_PG_TESTS=1 enables a temporary Unix-socket-only cluster. No production DSN is read.
"""

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
import pytest_asyncio

from db.queries import voice_operations as operations
from db.voice_operations_schema import ensure_voice_operations


@pytest.fixture(scope="session")
def postgres_socket():
    if os.environ.get("RUN_LOCAL_PG_TESTS") != "1":
        pytest.skip("Temporary PostgreSQL integration tests are opt-in")
    binary_dir = os.environ.get("VOICE_TEST_PG_BIN")
    postgres = (
        str(Path(binary_dir) / "postgres") if binary_dir else shutil.which("postgres")
    )
    if not postgres or not Path(postgres).is_file() or os.geteuid() == 0:
        pytest.skip("PostgreSQL server binaries and a non-root user are required")
    binary_dir = Path(postgres).resolve().parent
    initdb, pg_ctl = str(binary_dir / "initdb"), str(binary_dir / "pg_ctl")
    with tempfile.TemporaryDirectory(
        prefix="voice-pg-", dir="/private/tmp"
    ) as directory:
        data, socket = Path(directory) / "data", Path(directory) / "socket"
        socket.mkdir()
        subprocess.run(
            [
                initdb,
                "-D",
                str(data),
                "-U",
                "voice_test",
                "-A",
                "trust",
                "--no-locale",
                "--encoding=UTF8",
                "--no-sync",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        subprocess.run(
            [
                pg_ctl,
                "-D",
                str(data),
                "-l",
                str(Path(directory) / "postgres.log"),
                "-o",
                f"-F -k {socket} -h '' -p 55487",
                "-w",
                "start",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        try:
            yield str(socket)
        finally:
            subprocess.run(
                [pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=30,
            )


@pytest_asyncio.fixture
async def pool(postgres_socket, monkeypatch):
    pool = await asyncpg.create_pool(
        host=postgres_socket,
        port=55487,
        user="voice_test",
        database="postgres",
        min_size=1,
        max_size=16,
    )
    async with pool.acquire() as conn:
        await ensure_voice_operations(conn)
        await conn.execute("TRUNCATE development.voice_operation")
    monkeypatch.setattr(operations, "get_pool", AsyncMock(return_value=pool))
    try:
        yield pool
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_concurrent_callbacks_have_one_owner(pool):
    claims = await asyncio.gather(
        *(operations.claim_voice_operation(1, 2, 3) for _ in range(32))
    )
    owners = [claim for claim in claims if claim.state is operations.ClaimState.CLAIMED]
    assert len(owners) == 1
    assert len({claim.operation_id for claim in claims}) == 1
    assert all(
        claim.owner is None
        for claim in claims
        if claim.state is operations.ClaimState.BUSY
    )
    assert await operations.renew_voice_operation(owners[0])


@pytest.mark.asyncio
async def test_identity_includes_bot_chat_and_original_message(pool):
    claims = [
        await operations.claim_voice_operation(*key)
        for key in [(1, 2, 3), (9, 2, 3), (1, 9, 3), (1, 2, 9)]
    ]
    assert len({claim.operation_id for claim in claims}) == 4


@pytest.mark.asyncio
async def test_completed_receipt_survives_new_connections_and_replay(
    pool, postgres_socket
):
    claim = await operations.claim_voice_operation(1, 2, 3)
    receipt = {
        "kind": "note",
        "url": "https://example.invalid/note",
        "undo_ref": "fixture",
    }
    await operations.complete_voice_operation(claim, receipt)
    # A new connection sees durable state; no module-local cache is involved.
    conn = await asyncpg.connect(
        host=postgres_socket, port=55487, user="voice_test", database="postgres"
    )
    try:
        assert (
            await conn.fetchval("SELECT status FROM development.voice_operation")
            == "completed"
        )
    finally:
        await conn.close()
    replay = await operations.claim_voice_operation(1, 2, 3)
    assert replay.state is operations.ClaimState.COMPLETED
    assert replay.operation_id == claim.operation_id and replay.result == receipt
    assert replay.owner is None
    assert not await operations.release_voice_operation(claim)


@pytest.mark.asyncio
async def test_retry_preserves_identity_and_fences_previous_owner(pool):
    first = await operations.claim_voice_operation(1, 2, 3)
    assert await operations.release_voice_operation(first)
    retry = await operations.claim_voice_operation(1, 2, 3)
    assert retry.state is operations.ClaimState.CLAIMED
    assert retry.operation_id == first.operation_id and retry.owner != first.owner
    assert not await operations.renew_voice_operation(first)
    assert not await operations.release_voice_operation(first)
    with pytest.raises(operations.LeaseLost):
        await operations.complete_voice_operation(first, {"kind": "question"})
    await operations.complete_voice_operation(retry, {"kind": "question"})


@pytest.mark.asyncio
async def test_expired_lease_can_be_claimed_once_without_losing_message(pool):
    first = await operations.claim_voice_operation(1, 2, 3)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE development.voice_operation SET lease_until = clock_timestamp() - interval '1 second'"
        )
    assert not await operations.renew_voice_operation(first)
    with pytest.raises(operations.LeaseLost):
        await operations.complete_voice_operation(first, {"kind": "question"})
    claims = await asyncio.gather(
        *(operations.claim_voice_operation(1, 2, 3) for _ in range(8))
    )
    assert sum(claim.state is operations.ClaimState.CLAIMED for claim in claims) == 1
    assert all(claim.operation_id == first.operation_id for claim in claims)


@pytest.mark.asyncio
async def test_destination_is_bound_once_across_retries(pool):
    first = await operations.claim_voice_operation(1, 2, 3)
    original = {"repo": "owner/notes", "path": "notes.md", "branch": "main"}
    assert await operations.bind_voice_target(first, original) == original
    await operations.release_voice_operation(first)
    retry = await operations.claim_voice_operation(1, 2, 3)
    changed = {"repo": "other/notes", "path": "changed.md", "branch": "other"}
    assert retry.target == original
    assert await operations.bind_voice_target(retry, changed) == original
    with pytest.raises(operations.LeaseLost):
        await operations.bind_voice_target(first, changed)


@pytest.mark.asyncio
async def test_receipt_never_accepts_transcript_or_audio(pool):
    claim = await operations.claim_voice_operation(1, 2, 3)
    with pytest.raises(ValueError):
        await operations.complete_voice_operation(
            claim, {"kind": "note", "transcript": "private"}
        )
    async with pool.acquire() as conn:
        result = await conn.fetchval("SELECT result FROM development.voice_operation")
    assert result is None


@pytest.mark.asyncio
async def test_owner_grants_minimal_runtime_access_without_ddl(pool, postgres_socket):
    async with pool.acquire() as conn:
        await conn.execute("CREATE ROLE voice_runtime LOGIN")
        await ensure_voice_operations(conn, ["voice_runtime"])
    runtime = await asyncpg.create_pool(
        host=postgres_socket,
        port=55487,
        user="voice_runtime",
        database="postgres",
        min_size=1,
        max_size=2,
    )
    try:
        async with runtime.acquire() as conn:
            assert await ensure_voice_operations(conn) is False
            assert not await conn.fetchval(
                "SELECT has_schema_privilege(current_user, 'development', 'CREATE')"
            )
            assert not await conn.fetchval(
                "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
            )
    finally:
        await runtime.close()
        async with pool.acquire() as conn:
            await conn.execute("DROP OWNED BY voice_runtime")
            await conn.execute("DROP ROLE voice_runtime")


@pytest.mark.asyncio
async def test_existing_incompatible_schema_is_refused(pool):
    async with pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            await conn.execute(
                "ALTER TABLE development.voice_operation DROP COLUMN target"
            )
            with pytest.raises(RuntimeError, match="incompatible"):
                await ensure_voice_operations(conn)
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_retry_keeps_its_first_routing_decision(pool):
    first = await operations.claim_voice_operation(1, 2, 3)
    assert await operations.bind_voice_kind(first, "note") == "note"
    await operations.release_voice_operation(first)
    retry = await operations.claim_voice_operation(1, 2, 3)
    assert await operations.bind_voice_kind(retry, "question") == "note"
    with pytest.raises(operations.LeaseLost):
        await operations.complete_voice_operation(retry, {"kind": "question"})
    await operations.complete_voice_operation(retry, {"kind": "note"})


@pytest_asyncio.fixture
async def voice_runtime(pool, monkeypatch):
    from clients.github_api import GitHubNotesClient, NoteTarget
    from core import fleeting_notes
    from handlers import github, voice

    client = GitHubNotesClient()
    target = NoteTarget("owner/notes", "notes.md", "main")
    resolve_target = AsyncMock(return_value=target)
    append = AsyncMock(
        return_value={
            "repo": target.repo,
            "path": target.path,
            "branch": target.branch,
            "sha": "fixture-blob",
            "line_start": 1,
        }
    )
    trace = AsyncMock()
    monkeypatch.setattr("clients.github_api.github_notes", client)
    monkeypatch.setattr(voice, "github_notes", client)
    monkeypatch.setattr(client, "resolve_target", resolve_target)
    monkeypatch.setattr(client, "_append_to_file", append)
    monkeypatch.setattr(
        "clients.github_api.resolve_auth_context", AsyncMock(return_value=MagicMock())
    )
    monkeypatch.setattr(fleeting_notes, "capture_note_trace", trace)
    monkeypatch.setattr(voice, "voice_enabled_for", lambda _chat: True)
    monkeypatch.setattr(
        voice, "resolve_ory_id_from_chat", AsyncMock(return_value="account")
    )
    monkeypatch.setattr(voice, "set_consent_grant", AsyncMock())
    monkeypatch.setattr(voice, "get_consent_grant", AsyncMock(return_value=True))
    monkeypatch.setattr(voice, "_bot_awaits_reply", AsyncMock(return_value=False))
    monkeypatch.setattr(voice, "STT_API_KEY", "fixture")

    async def recognize(_audio):
        await asyncio.sleep(0.03)
        return "купить молоко"

    stt = AsyncMock(side_effect=recognize)
    monkeypatch.setattr(voice, "_transcribe_voice", stt)

    # The Telegram adapter delegates to the real common writer; no GitHub/network calls.
    async def process(message, source, **kwargs):
        return await fleeting_notes.write_note(
            message.from_user.id,
            message.text[1:],
            source,
            message_id=message.message_id,
            **kwargs,
        )

    monkeypatch.setattr(github, "process_fleeting_note", process)
    message = MagicMock()
    message.chat.id = message.from_user.id = 202
    message.chat.type = "private"
    message.message_id = 303
    message.reply_to_message = None
    message.voice.duration = 2
    message.bot.id = 101
    message.bot.download = AsyncMock(
        return_value=SimpleNamespace(read=lambda: b"fixture-audio")
    )
    message.answer = AsyncMock()
    message.reply = AsyncMock()
    message.model_copy.side_effect = lambda update: SimpleNamespace(
        text=update["text"],
        voice=None,
        from_user=message.from_user,
        chat=message.chat,
        message_id=message.message_id,
    )

    def callback():
        value = MagicMock()
        value.from_user.id = 202
        value.message.reply_to_message = message
        value.answer = AsyncMock()
        return value

    state = SimpleNamespace(get_state=AsyncMock(return_value=None))
    return SimpleNamespace(
        client=client,
        stt=stt,
        append=append,
        trace=trace,
        message=message,
        callback=callback,
        state=state,
        resolve_target=resolve_target,
    )


@pytest.mark.asyncio
async def test_distinct_consent_updates_and_later_replay_process_original_voice_once(
    voice_runtime,
):
    from handlers.voice import on_voice_consent_grant

    runtime = voice_runtime
    await asyncio.gather(
        *(on_voice_consent_grant(runtime.callback(), runtime.state) for _ in range(2))
    )
    await asyncio.sleep(0)
    assert runtime.message.bot.download.await_count == 1
    assert (
        runtime.stt.await_count
        == runtime.append.await_count
        == runtime.trace.await_count
        == 1
    )
    replay = runtime.callback()
    await on_voice_consent_grant(replay, runtime.state)
    assert (
        runtime.stt.await_count
        == runtime.append.await_count
        == runtime.trace.await_count
        == 1
    )
    assert "уже обработано" in runtime.message.answer.call_args.args[0]
    assert runtime.client._pending == []


@pytest.mark.asyncio
async def test_failed_voice_save_retry_keeps_original_operation_and_target(
    voice_runtime,
    monkeypatch,
):
    from clients.github_api import NoteTarget
    from handlers.voice import on_voice_consent_grant, on_voice_retry

    runtime = voice_runtime
    written = runtime.append.return_value
    runtime.append.side_effect = [None, written]
    await on_voice_consent_grant(runtime.callback(), runtime.state)
    assert runtime.client._pending == []  # durable voice retry owns this failure
    first = runtime.append.call_args.kwargs
    original_lines = first["note_lines"]
    monkeypatch.setattr(
        "clients.github_api.datetime",
        SimpleNamespace(now=lambda _tz: __import__("datetime").datetime(2040, 1, 1)),
    )
    runtime.resolve_target.return_value = NoteTarget("other/repo", "changed.md", "new")
    await on_voice_retry(runtime.callback(), runtime.state)
    await asyncio.sleep(0)
    second = runtime.append.call_args.kwargs
    assert second["note_lines"] == original_lines
    assert first["operation_id"] == second["operation_id"]
    assert (second["repo"], second["path"], second["branch"]) == (
        "owner/notes",
        "notes.md",
        "main",
    )
    runtime.trace.assert_awaited_once()
    assert runtime.trace.call_args.args[2] == 303


@pytest.mark.asyncio
async def test_first_note_hash_and_creation_time_survive_retry_and_fence_old_owner(
    pool,
):
    first = await operations.claim_voice_operation(1, 2, 3)
    assert first.created_at.utcoffset() is not None
    await operations.bind_voice_kind(first, "note")
    assert await operations.bind_voice_note_hash(first, "a" * 12) == "a" * 12
    assert await operations.release_voice_operation(first)
    retry = await operations.claim_voice_operation(1, 2, 3)
    assert retry.created_at == first.created_at
    assert await operations.bind_voice_note_hash(retry, "b" * 12) == "a" * 12
    with pytest.raises(operations.LeaseLost):
        await operations.bind_voice_note_hash(first, "c" * 12)
    await operations.complete_voice_operation(retry, {"kind": "note", "url": "fixture"})
    receipt = await operations.claim_voice_operation(1, 2, 3)
    assert receipt.result == {"kind": "note", "url": "fixture"}
