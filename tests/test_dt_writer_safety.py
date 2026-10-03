"""Real PostgreSQL regressions; a private disposable cluster, never a supplied DSN."""

import asyncio
from contextlib import asynccontextmanager
import itertools
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest.mock import AsyncMock

import asyncpg
import pytest
import pytest_asyncio

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@localhost:5432/fake")

from db.queries import dt_sync  # noqa: E402 -- synthetic config before bot imports
from scripts import fix_dt_identity_keys as repair  # noqa: E402


def _postgres_bin():
    candidates = [
        Path("/opt/homebrew/opt/postgresql@16/bin"),
        Path("/usr/lib/postgresql/16/bin"),
    ]
    if shutil.which("postgres"):
        candidates.append(Path(shutil.which("postgres")).parent)
    for directory in candidates:
        if all(
            (directory / name).is_file() for name in ("postgres", "initdb", "pg_ctl")
        ):
            version = subprocess.check_output(
                [directory / "postgres", "--version"], text=True
            )
            if " 16." in version:
                return directory
    pytest.skip("PostgreSQL 16 server binaries required for disposable SQL tests")


def _run_postgres(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True)
    if result.returncode:
        pytest.fail(
            f"Disposable PostgreSQL command failed: {result.stderr or result.stdout}"
        )


@pytest.fixture(scope="module")
def postgres():
    binaries = _postgres_bin()
    with tempfile.TemporaryDirectory(prefix="dt-writer-pg-", dir="/tmp") as temporary:
        root = Path(temporary)
        data = root / "data"
        _run_postgres(
            [binaries / "initdb", "-D", data, "--auth=trust", "-U", "dt_test"]
        )
        with (data / "postgresql.conf").open("a") as config:
            config.write(
                f"\nlisten_addresses = ''\nunix_socket_directories = '{root}'\n"
            )
        try:
            _run_postgres(
                [
                    binaries / "pg_ctl",
                    "-D",
                    data,
                    "-l",
                    root / "postgres.log",
                    "-w",
                    "-t",
                    "15",
                    "start",
                ]
            )
            yield {"host": str(root), "database": "postgres", "user": "dt_test"}
        finally:
            if (data / "postmaster.pid").exists():
                _run_postgres(
                    [binaries / "pg_ctl", "-D", data, "-w", "-m", "fast", "stop"]
                )


@pytest_asyncio.fixture
async def conn(postgres):
    connection = await asyncpg.connect(**postgres)
    await connection.execute("""
        DROP SCHEMA public CASCADE;
        CREATE SCHEMA public;
        CREATE TABLE public.users (
            id text PRIMARY KEY, ory_id text, telegram_id bigint, aisystant_id text
        );
        CREATE TABLE public.digital_twins (
            user_id text PRIMARY KEY, data jsonb NOT NULL,
            created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now()
        );
    """)
    try:
        yield connection
    finally:
        await connection.close()


async def _put(conn, key, data):
    await conn.execute(
        "INSERT INTO public.digital_twins(user_id, data) VALUES ($1, $2::jsonb)",
        key,
        json.dumps(data),
    )


async def _pair(conn, source=None, target=None, suffix="1"):
    source_id, target_id = f"source-{suffix}", f"target-{suffix}"
    await conn.execute(
        "INSERT INTO public.users(id,ory_id,telegram_id) VALUES ($1,$2,111)",
        source_id,
        target_id,
    )
    await _put(
        conn,
        source_id,
        source if source is not None else {"2_collected": {"source": 1}},
    )
    await _put(
        conn,
        target_id,
        target if target is not None else {"2_collected": {"target": 2}},
    )
    return source_id, target_id


async def _twins(conn):
    return {
        row["user_id"]: json.loads(row["data"])
        for row in await conn.fetch("SELECT user_id,data FROM public.digital_twins")
    }


class Pool:
    def __init__(self, *connections):
        self.connections = itertools.cycle(connections)

    @asynccontextmanager
    async def acquire(self):
        yield next(self.connections)


class ConnectionProxy:
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)


class PauseAfterLocks(ConnectionProxy):
    def __init__(self, connection):
        super().__init__(connection)
        self.locked = asyncio.Event()
        self.resume = asyncio.Event()

    async def fetch(self, sql, *args):
        result = await self.connection.fetch(sql, *args)
        if "ORDER BY user_id" in sql and "FOR UPDATE" in sql:
            self.locked.set()
            await asyncio.wait_for(self.resume.wait(), 5)
        return result


async def _wait_for_lock(conn, pid):
    async with asyncio.timeout(3):
        while not await conn.fetchval(
            "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid=$1", pid
        ):
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_repair_preserves_categories_and_target_priority(conn):
    target = {
        "1_declarative": {"name": "synthetic"},
        "2_collected": {"shared": {"winner": "target"}, "target": 2},
        "3_derived": {"score": 3},
        "4_generated": {"plan": 4},
    }
    source_id, target_id = await _pair(
        conn, {"2_collected": {"shared": {"winner": "source"}, "source": 1}}, target
    )
    assert await repair.repair_identity_keys(conn) == 1
    expected = dict(target)
    expected["2_collected"] = {**target["2_collected"], "source": 1}
    assert await _twins(conn) == {target_id: expected}
    assert source_id not in await _twins(conn)
    assert await repair.repair_identity_keys(conn) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe",
    [{"1_declarative": {"only_source": True}}, {"2_collected": None}, [], None],
)
async def test_unsafe_source_rolls_back_entire_batch(conn, unsafe):
    await _pair(conn, suffix="1")
    source_id, _ = await _pair(conn, suffix="2")
    await conn.execute(
        "UPDATE public.digital_twins SET data=$2::jsonb WHERE user_id=$1",
        source_id,
        json.dumps(unsafe),
    )
    before = await _twins(conn)
    with pytest.raises(repair.UnsafeRepairError):
        await repair.repair_identity_keys(conn)
    assert await _twins(conn) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mapping", [(("a", "c"), ("b", "c")), (("a", "b"), ("b", "c"))]
)
async def test_ambiguous_identity_mapping_does_not_change_rows(conn, mapping):
    await conn.executemany(
        "INSERT INTO public.users(id,ory_id) VALUES ($1,$2)", mapping
    )
    for key in {key for pair in mapping for key in pair}:
        await _put(conn, key, {"2_collected": {key: 1}})
    before = await _twins(conn)
    with pytest.raises(repair.UnsafeRepairError):
        await repair.repair_identity_keys(conn)
    assert await _twins(conn) == before


@pytest.mark.asyncio
async def test_canonical_self_pair_is_never_deleted(conn):
    await conn.execute("INSERT INTO public.users(id,ory_id) VALUES ('same','same')")
    await _put(conn, "same", {"1_declarative": {"keep": 1}})
    before = await _twins(conn)
    assert await repair.repair_identity_keys(conn) == 0
    assert await _twins(conn) == before


@pytest.mark.asyncio
async def test_delete_failure_rolls_back_prior_merge(conn):
    await _pair(conn)
    before = await _twins(conn)
    await conn.execute("""
        CREATE FUNCTION reject_delete() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'synthetic delete failure'; END $$;
        CREATE TRIGGER reject_delete BEFORE DELETE ON public.digital_twins
        FOR EACH ROW EXECUTE FUNCTION reject_delete();
    """)
    with pytest.raises(asyncpg.RaiseError, match="synthetic delete failure"):
        await repair.repair_identity_keys(conn)
    assert await _twins(conn) == before


@pytest.mark.asyncio
async def test_repair_locks_target_and_preserves_concurrent_writer(conn, postgres):
    _, target_id = await _pair(
        conn, target={"1_declarative": {}, "2_collected": {"target": 2}}
    )
    paused = PauseAfterLocks(conn)
    task = asyncio.create_task(repair.repair_identity_keys(paused))
    await asyncio.wait_for(paused.locked.wait(), 3)
    other = await asyncpg.connect(**postgres)
    observer = await asyncpg.connect(**postgres)
    try:
        writer = asyncio.create_task(
            other.execute(
                """
            UPDATE public.digital_twins SET data=jsonb_set(data,'{1_declarative}',
            '{"new_writer":true}'::jsonb) WHERE user_id=$1
        """,
                target_id,
            )
        )
        await _wait_for_lock(observer, other.get_server_pid())
        paused.resume.set()
        assert await asyncio.wait_for(task, 3) == 1
        await asyncio.wait_for(writer, 3)
        document = (await _twins(conn))[target_id]
        assert document["1_declarative"] == {"new_writer": True}
        assert document["2_collected"] == {"source": 1, "target": 2}
    finally:
        paused.resume.set()
        await other.close()
        await observer.close()


@pytest.mark.asyncio
async def test_new_identity_pair_cannot_enter_later_delete(conn, postgres):
    await _pair(conn)
    paused = PauseAfterLocks(conn)
    task = asyncio.create_task(repair.repair_identity_keys(paused))
    await asyncio.wait_for(paused.locked.wait(), 3)
    other = await asyncpg.connect(**postgres)
    try:
        source, target = await _pair(other, suffix="new")
        paused.resume.set()
        assert await asyncio.wait_for(task, 3) == 1
        twins = await _twins(conn)
        assert twins[source] == {"2_collected": {"source": 1}}
        assert twins[target] == {"2_collected": {"target": 2}}
    finally:
        paused.resume.set()
        await other.close()


@pytest.mark.asyncio
async def test_source_change_before_lock_aborts_without_losing_either_row(
    conn, postgres
):
    source, target = await _pair(conn)
    other = await asyncpg.connect(**postgres)
    observer = await asyncpg.connect(**postgres)
    try:
        async with other.transaction():
            await other.execute(
                """
                UPDATE public.digital_twins SET data=jsonb_set(data,'{2_collected}',
                '{"concurrent_source":3}'::jsonb) WHERE user_id=$1
            """,
                source,
            )
            task = asyncio.create_task(repair.repair_identity_keys(conn))
            await _wait_for_lock(observer, conn.get_server_pid())
        with pytest.raises(asyncpg.SerializationError):
            await asyncio.wait_for(task, 3)
        twins = await _twins(conn)
        assert twins[source] == {"2_collected": {"concurrent_source": 3}}
        assert twins[target] == {"2_collected": {"target": 2}}
    finally:
        await other.close()
        await observer.close()


def _qualification_pools(monkeypatch, *connections):
    pools = itertools.cycle(Pool(connection) for connection in connections)
    monkeypatch.setattr(dt_sync, "get_pool", AsyncMock(side_effect=lambda: next(pools)))
    from db import connection

    secrets = AsyncMock()
    secrets.fetchval.return_value = None
    monkeypatch.setattr(
        connection, "get_secrets_pool", AsyncMock(return_value=Pool(secrets))
    )


class PauseAfterQualificationRead(ConnectionProxy):
    def __init__(self, connection):
        super().__init__(connection)
        self.read = asyncio.Event()
        self.resume = asyncio.Event()

    async def fetchval(self, sql, *args):
        result = await self.connection.fetchval(sql, *args)
        if sql.lstrip().startswith("SELECT data->"):
            self.read.set()
            await asyncio.wait_for(self.resume.wait(), 5)
        return result


@pytest.mark.asyncio
async def test_higher_qualification_wins_after_preliminary_absent_read(
    conn, postgres, monkeypatch
):
    _, target_id = await _pair(
        conn,
        target={
            "1_declarative": {"keep": 1},
            "2_collected": {"2_2_courses": {"sibling": 2}},
        },
    )
    paused = PauseAfterQualificationRead(conn)
    _qualification_pools(monkeypatch, paused)
    task = asyncio.create_task(dt_sync.ensure_default_qualification(111))
    await asyncio.wait_for(paused.read.wait(), 3)
    other = await asyncpg.connect(**postgres)
    try:
        higher = {"level": "Работник", "numeric": 25}
        await other.execute(
            """
            UPDATE public.digital_twins SET data=jsonb_set(data,
            '{2_collected,2_2_courses,qualification_level}',$2::jsonb) WHERE user_id=$1
        """,
            target_id,
            json.dumps(higher),
        )
        paused.resume.set()
        assert await asyncio.wait_for(task, 3) is False
        result = (await _twins(conn))[target_id]
        assert result["2_collected"]["2_2_courses"] == {
            "sibling": 2,
            "qualification_level": higher,
        }
        assert result["1_declarative"] == {"keep": 1}
    finally:
        paused.resume.set()
        await other.close()


@pytest.mark.asyncio
async def test_two_default_writers_have_one_winner(conn, postgres, monkeypatch):
    _, target_id = await _pair(conn, target={"2_collected": {"2_2_courses": {}}})
    other = await asyncpg.connect(**postgres)
    barrier = asyncio.Barrier(2)

    class Together(ConnectionProxy):
        async def fetchval(self, sql, *args):
            result = await self.connection.fetchval(sql, *args)
            if sql.lstrip().startswith("SELECT data->"):
                await asyncio.wait_for(barrier.wait(), 3)
            return result

    _qualification_pools(monkeypatch, Together(conn), Together(other))
    try:
        results = await asyncio.gather(
            dt_sync.ensure_default_qualification(111),
            dt_sync.ensure_default_qualification(111),
        )
        assert sorted(results) == [False, True]
        qualification = (await _twins(conn))[target_id]["2_collected"]["2_2_courses"][
            "qualification_level"
        ]
        assert qualification["numeric"] == 20
    finally:
        await other.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value,expected",
    [(None, True), ({}, True), (False, False), (0, False), ("", False), ([], False)],
)
async def test_qualification_empty_values_have_explicit_semantics(
    conn, monkeypatch, value, expected
):
    _, target_id = await _pair(
        conn, target={"2_collected": {"2_2_courses": {"qualification_level": value}}}
    )
    _qualification_pools(monkeypatch, conn)
    assert await dt_sync.ensure_default_qualification(111) is expected
    actual = (await _twins(conn))[target_id]["2_collected"]["2_2_courses"][
        "qualification_level"
    ]
    if expected:
        assert actual["numeric"] == 20
    else:
        assert actual == value


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, False, 0, "", []])
@pytest.mark.parametrize("parent", ["root", "2_collected", "2_2_courses"])
async def test_default_preserves_malformed_parent(conn, monkeypatch, value, parent):
    if parent == "root":
        document = value
    elif parent == "2_collected":
        document = {"1_declarative": {"keep": 1}, "2_collected": value}
    else:
        document = {
            "1_declarative": {"keep": 1},
            "2_collected": {"2_2_courses": value, "2_7_iwe": {"keep": 2}},
        }
    await conn.execute(
        "INSERT INTO public.users(id,ory_id,telegram_id) VALUES ('source','target',111)"
    )
    await _put(conn, "target", document)
    _qualification_pools(monkeypatch, conn)
    assert await dt_sync.ensure_default_qualification(111) is False
    assert (await _twins(conn))["target"] == document


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document", [{}, {"2_collected": {}}, {"2_collected": {"2_2_courses": {}}}]
)
async def test_default_creates_missing_parent_objects(conn, monkeypatch, document):
    _, target_id = await _pair(conn, target=document)
    _qualification_pools(monkeypatch, conn)
    assert await dt_sync.ensure_default_qualification(111) is True
    qualification = (await _twins(conn))[target_id]["2_collected"]["2_2_courses"][
        "qualification_level"
    ]
    assert qualification["numeric"] == 20


class CollectedSource:
    async def fetchrow(self, sql, *args):
        if "FROM development.engagement e" in sql:
            counters = (
                "sessions_total",
                "events_total",
                "marathon_steps_total",
                "feed_completed_total",
                "training_attempts_total",
                "training_passed_total",
                "assessments_total",
                "marathon_tasks_total",
                "active_days",
                "events_last_7d",
                "events_last_30d",
                "ai_chats_total",
            )
            return {
                **dict.fromkeys(counters, 1),
                "user_id": 111,
                "user_uuid": "target-1",
                "user_ory_id": "target-1",
                "first_event_at": None,
                "last_event_at": None,
            }
        if "FROM public.domain_event" in sql:
            return {"coding_seconds_30d": 0, "commits_30d": 0}
        if any(
            table in sql
            for table in ("notification_engagement", "public.users", "public.dt_tokens")
        ):
            return None
        raise AssertionError(f"Unexpected source query: {sql}")

    async def fetch(self, sql, *args):
        assert "learning_history" in sql
        return []

    async def fetchval(self, sql, *args):
        assert "data->'2_collected'" in sql
        return json.dumps({"2_6_coding": {"old": 1}, "2_7_iwe": {"old": 1}})


@pytest.mark.asyncio
async def test_single_sync_preserves_latest_unwritten_groups_and_reports_only_written(
    conn, postgres, monkeypatch
):
    _, target_id = await _pair(
        conn,
        target={
            "1_declarative": {"keep": 1},
            "2_collected": {"2_6_coding": {"old": 1}, "2_7_iwe": {"old": 1}},
        },
    )
    other = await asyncpg.connect(**postgres)

    class ConcurrentWrite(ConnectionProxy):
        async def execute(self, sql, *args):
            await other.execute(
                """
                UPDATE public.digital_twins SET data=data || jsonb_build_object('2_collected',
                '{"2_6_coding":{"latest":2},"2_7_iwe":{"latest":3}}'::jsonb) WHERE user_id=$1
            """,
                target_id,
            )
            return await self.connection.execute(sql, *args)

    source = CollectedSource()
    monkeypatch.setattr(
        dt_sync, "get_pool", AsyncMock(return_value=Pool(source, ConcurrentWrite(conn)))
    )
    monkeypatch.setattr(
        dt_sync, "get_learning_pool", AsyncMock(return_value=Pool(source))
    )
    from db import connection

    monkeypatch.setattr(
        connection, "get_secrets_pool", AsyncMock(return_value=Pool(source))
    )
    event = AsyncMock()
    monkeypatch.setattr(dt_sync, "post_event", event)
    try:
        assert await dt_sync.sync_one_user_to_dt(target_id) is True
        await asyncio.sleep(0)  # Allow the existing event task to finish.
        result = (await _twins(conn))[target_id]
        assert result["2_collected"]["2_6_coding"] == {"latest": 2}
        assert result["2_collected"]["2_7_iwe"] == {"latest": 3}
        assert result["1_declarative"] == {"keep": 1}
        event.assert_awaited_once()
        assert event.call_args.kwargs["payload"]["sections_written"] == [
            "2_1_account",
            "2_2_courses",
            "2_3_practice",
            "2_4_time",
        ]
    finally:
        await other.close()
