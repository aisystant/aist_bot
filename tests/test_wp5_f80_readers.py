"""Exercise F80 reader setup and deployment FDW probes on disposable PostgreSQL.

Run with pytest; PG_BIN may locate PostgreSQL binaries. Every test starts its own
loopback-only cluster with SCRAM host authentication and synthetic data. No
external DSN is accepted. The migration modules are imported verbatim; the real
postgres_fdw extension connects as their engine's dedicated source readers.
"""

import asyncio
import importlib.util
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "00000000-0000-0000-0000-000000000001"
CONTRACT = "00000000-0000-0000-0000-000000000002"
OWNER = "neondb_owner"
ENGINE = "rewards_points_engine_owner"
SUB_READER = "rewards_subscription_fdw_reader"
DATABASES = ("rewards", "learning", "indicators", "subscription")


def load_migration(stem):
    spec = importlib.util.spec_from_file_location(
        f"wp5_test_{stem}", ROOT / "db/migrations" / f"{stem}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


READERS = load_migration("052_wp5_f80_readers")
DEPLOY = load_migration("052_wp5_f80")


def postgres_binaries():
    candidates = []
    if os.environ.get("PG_BIN"):
        candidates.append(Path(os.environ["PG_BIN"]))
    if shutil.which("initdb"):
        candidates.append(Path(shutil.which("initdb")).resolve().parent)
    candidates.extend(
        (
            Path("/opt/homebrew/opt/postgresql@16/bin"),
            Path("/usr/local/opt/postgresql@16/bin"),
        )
    )
    candidates.extend(sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True))
    for directory in candidates:
        if all(
            (directory / name).is_file() for name in ("initdb", "pg_ctl", "postgres")
        ):
            return directory
    pytest.skip("Local PostgreSQL binaries unavailable; set PG_BIN")


def postgres_command(command):
    # A developer's PGHOST/PGSERVICE must never redirect these local operations.
    result = subprocess.run(
        [str(item) for item in command],
        env={
            key: value for key, value in os.environ.items() if not key.startswith("PG")
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@dataclass(frozen=True)
class Cluster:
    port: int
    password: str = field(repr=False)
    reader_password: str = field(repr=False)

    def dsn(self, database):
        return f"postgresql://{OWNER}:{self.password}@127.0.0.1:{self.port}/{database}"


@pytest.fixture
def cluster(monkeypatch):
    binaries = postgres_binaries()
    with tempfile.TemporaryDirectory(
        prefix="wp5-f80-readers-", dir="/tmp"
    ) as temporary:
        directory = Path(temporary)
        password = secrets.token_urlsafe(32)
        password_file = directory / "password"
        password_file.write_text(password)
        password_file.chmod(0o600)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        postgres_command(
            [
                binaries / "initdb",
                "-D",
                directory / "data",
                "--no-locale",
                "--auth-local=trust",
                "--auth-host=scram-sha-256",
                "-U",
                OWNER,
                f"--pwfile={password_file}",
            ]
        )
        postgres_command(
            [
                binaries / "pg_ctl",
                "-D",
                directory / "data",
                "-l",
                directory / "server.log",
                "-o",
                f"-h 127.0.0.1 -p {port} -k {directory}",
                "-w",
                "start",
            ]
        )
        try:
            fixture = Cluster(port, password, secrets.token_urlsafe(32))
            monkeypatch.setenv("REWARDS_URL", fixture.dsn("rewards"))
            yield fixture
        finally:
            postgres_command(
                [
                    binaries / "pg_ctl",
                    "-D",
                    directory / "data",
                    "-m",
                    "immediate",
                    "-w",
                    "stop",
                ]
            )


@asynccontextmanager
async def source_databases(cluster):
    async with AsyncExitStack() as stack:
        bootstrap = await asyncpg.connect(cluster.dsn("postgres"))
        stack.push_async_callback(bootstrap.close)
        await bootstrap.execute(f"CREATE ROLE {ENGINE} NOLOGIN NOINHERIT NOBYPASSRLS")
        await bootstrap.execute("CREATE ROLE untrusted_owner NOLOGIN")
        for role in ("rewards_learning_fdw_reader", "rewards_indicators_fdw_reader"):
            await bootstrap.execute(
                f"CREATE ROLE {role} LOGIN NOINHERIT NOBYPASSRLS PASSWORD '{cluster.reader_password}'"
            )
        connections = {}
        for database in DATABASES:
            await bootstrap.execute(f"CREATE DATABASE {database}")
            connection = await asyncpg.connect(cluster.dsn(database))
            stack.push_async_callback(connection.close)
            connections[database] = connection
        await seed_sources(connections)
        await seed_foreign_tables(connections["rewards"], cluster)
        yield connections


async def seed_sources(connections):
    await connections["learning"].execute(f"""
        CREATE SCHEMA learning;
        CREATE TABLE learning.domain_event (
            id bigint PRIMARY KEY, external_id text, occurred_at timestamptz,
            account_id uuid, event_type text);
        CREATE TABLE learning.stage_transitions (
            account_id uuid, to_stage smallint, occurred_at timestamptz, private_note text);
        INSERT INTO learning.domain_event VALUES
            (1,'fixture','2026-01-15T12:00:00Z','{ACCOUNT}','practice');
        INSERT INTO learning.stage_transitions VALUES
            ('{ACCOUNT}',3,'2026-01-01T00:00:00Z','private');
        GRANT USAGE ON SCHEMA learning TO rewards_learning_fdw_reader;
        GRANT SELECT ON learning.domain_event TO rewards_learning_fdw_reader;
    """)
    await connections["indicators"].execute(f"""
        CREATE TABLE public.calculated_profile (
            account_id uuid, rcs_current jsonb, indicators jsonb, qualification_level integer);
        CREATE TABLE public.digital_twins (user_id text, data jsonb);
        INSERT INTO public.calculated_profile VALUES ('{ACCOUNT}',NULL,'{{}}',4);
        GRANT SELECT ON public.calculated_profile TO rewards_indicators_fdw_reader;
    """)
    await connections["indicators"].execute(
        "INSERT INTO public.digital_twins VALUES($1,$2)",
        ACCOUNT,
        json.dumps(
            {
                "private": "secret",
                "3_derived": {
                    "private": "secret",
                    "3_4_qualification": {
                        "stage_id": 3,
                        "stage": "3",
                        "stage_source": "confirmed",
                        "rcs_version": "fixture",
                        "private": "secret",
                    },
                },
            }
        ),
    )
    await connections["subscription"].execute(f"""
        CREATE TABLE public.contract (
            contract_id uuid, account_id uuid, valid_from timestamptz,
            valid_to timestamptz, status text, private_note text);
        CREATE TABLE public.contract_event (
            event_id bigint, contract_id uuid, event_type text,
            occurred_at timestamptz, snapshot jsonb);
        INSERT INTO public.contract VALUES
            ('{CONTRACT}','{ACCOUNT}','2026-01-01T00:00:00Z',NULL,'active','private');
    """)
    await connections["subscription"].execute(
        "INSERT INTO public.contract_event VALUES(1,$1::text::uuid,'created',"
        "'2026-01-01T00:00:00Z',$2)",
        CONTRACT,
        json.dumps(
            {
                "status": "active",
                "valid_from": "2026-01-01T00:00:00Z",
                "valid_to": None,
                "private": "secret",
            }
        ),
    )


async def seed_foreign_tables(rewards, cluster):
    await rewards.execute("CREATE EXTENSION postgres_fdw")
    for source in READERS.SOURCES:
        await rewards.execute(
            f"CREATE SERVER {source.server} FOREIGN DATA WRAPPER postgres_fdw "
            f"OPTIONS(host '127.0.0.1',port '{cluster.port}',dbname '{source.database}')"
        )
        await rewards.execute(
            f"CREATE USER MAPPING FOR {OWNER} SERVER {source.server} "
            f"OPTIONS(user '{OWNER}',password '{cluster.password}')"
        )
        if source.database != "subscription":
            await rewards.execute(
                f"CREATE USER MAPPING FOR {ENGINE} SERVER {source.server} "
                f"OPTIONS(user '{source.reader}',password '{cluster.reader_password}')"
            )
            await rewards.execute(
                f"GRANT USAGE ON FOREIGN SERVER {source.server} TO {ENGINE}"
            )
    await rewards.execute(f"""
        CREATE SCHEMA _foreign_learning;
        CREATE SCHEMA _foreign_indicators;
        CREATE SCHEMA _foreign_subscription;
        CREATE FOREIGN TABLE _foreign_learning.domain_event (
            id bigint, external_id text, occurred_at timestamptz, account_id uuid, event_type text)
            SERVER learning_srv OPTIONS(schema_name 'learning',table_name 'domain_event');
        CREATE FOREIGN TABLE _foreign_indicators.calculated_profile (
            account_id uuid,rcs_current jsonb,indicators jsonb,qualification_level integer)
            SERVER indicators_srv OPTIONS(schema_name 'public',table_name 'calculated_profile');
        CREATE FOREIGN TABLE _foreign_subscription.contract (
            contract_id uuid,account_id uuid,valid_from timestamptz,valid_to timestamptz,status text)
            SERVER subscription_srv OPTIONS(schema_name 'public',table_name 'contract');
        GRANT USAGE ON SCHEMA _foreign_learning,_foreign_indicators TO {ENGINE};
        GRANT SELECT ON _foreign_learning.domain_event,_foreign_indicators.calculated_profile TO {ENGINE};
    """)


async def password_fingerprint(rewards):
    return await rewards.fetchval(
        "SELECT md5(rolpassword) FROM pg_authid WHERE rolname=$1", SUB_READER
    )


def test_default_check_cannot_write(cluster):
    async def scenario():
        async with source_databases(cluster) as connections:
            assert await READERS.run(False) is True
            assert await password_fingerprint(connections["rewards"]) is None
            for source in READERS.SOURCES:
                assert (
                    await connections[source.database].fetchval(
                        "SELECT to_regclass($1)", f"public.{source.view}"
                    )
                    is None
                )
            async with AsyncExitStack() as stack:
                connection = await READERS.connect(
                    stack, cluster.dsn("rewards"), "rewards", readonly=True
                )
                with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
                    await connection.execute("CREATE TABLE unexpected_write(id int)")

    asyncio.run(scenario())


def test_apply_is_repeatable_and_projects_only_required_fields(cluster):
    async def scenario():
        async with source_databases(cluster) as connections:
            assert await READERS.run(True) is False
            fingerprint = await password_fingerprint(connections["rewards"])
            assert fingerprint is not None
            assert await READERS.run(True) is False
            assert await READERS.run(False) is False
            assert await password_fingerprint(connections["rewards"]) == fingerprint
            twin = await connections["indicators"].fetchval(
                "SELECT data FROM public.rewards_stage_profile"
            )
            assert json.loads(twin) == {
                "3_derived": {
                    "3_4_qualification": {
                        "stage_id": 3,
                        "stage": "3",
                        "stage_source": "confirmed",
                        "rcs_version": "fixture",
                    }
                }
            }
            subscription = connections["subscription"]
            async with subscription.transaction():
                await subscription.execute(f"SET LOCAL ROLE {SUB_READER}")
                snapshot = await subscription.fetchval(
                    "SELECT snapshot FROM public.rewards_contract_history"
                )
                assert set(json.loads(snapshot)) == {"status", "valid_from", "valid_to"}
                assert (
                    str(
                        await subscription.fetchval(
                            "SELECT contract_id FROM public.contract"
                        )
                    )
                    == CONTRACT
                )
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    async with subscription.transaction():
                        await subscription.fetch(
                            "SELECT private_note FROM public.contract"
                        )
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    async with subscription.transaction():
                        await subscription.fetch(
                            "SELECT snapshot FROM public.contract_event"
                        )

    asyncio.run(scenario())


def test_engine_reads_real_fdw_columns_and_refuses_missing_column_grant(cluster):
    async def scenario():
        async with source_databases(cluster) as connections:
            assert await READERS.run(True) is False
            rewards = connections["rewards"]
            async with rewards.transaction():
                await DEPLOY.check_mappings(rewards)
                await DEPLOY.setup_foreign_tables(rewards)
                await DEPLOY.verify_owner_reads(rewards)
                await DEPLOY.setup_foreign_tables(rewards)
                await rewards.execute(f"SET LOCAL ROLE {ENGINE}")
                assert (
                    await rewards.fetchval(
                        "SELECT to_stage FROM _foreign_learning.stage_transitions"
                    )
                    == 3
                )
                assert (
                    str(
                        await rewards.fetchval(
                            "SELECT contract_id FROM _foreign_subscription.contract_event"
                        )
                    )
                    == CONTRACT
                )
                assert await rewards.fetchval("SELECT current_user") == ENGINE
                await rewards.execute("RESET ROLE")
            # Keep the sentinel key readable: the deployment must also read valid_to.
            await connections["subscription"].execute(
                f"REVOKE SELECT(valid_to) ON public.contract FROM {SUB_READER}"
            )
            async with rewards.transaction():
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await DEPLOY.verify_owner_reads(rewards)
                # The outer transaction remains usable, under the original role.
                assert await rewards.fetchval("SELECT current_user") == OWNER

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "corruption",
    [
        "ALTER VIEW public.rewards_stage_profile SET(security_barrier=false)",
        "ALTER VIEW public.rewards_stage_profile OWNER TO untrusted_owner",
        "CREATE OR REPLACE VIEW public.rewards_stage_profile AS SELECT user_id,data FROM public.digital_twins",
    ],
)
def test_unknown_view_is_not_overwritten(cluster, corruption):
    async def scenario():
        async with source_databases(cluster) as connections:
            indicators = connections["indicators"]
            assert await READERS.run(True) is False
            await indicators.execute(corruption)
            before = await indicators.fetchrow(
                "SELECT relowner,reloptions,pg_get_viewdef(oid) AS definition "
                "FROM pg_class WHERE oid='public.rewards_stage_profile'::regclass"
            )
            with pytest.raises(RuntimeError, match="Unknown existing view"):
                await READERS.run(True)
            assert (
                await indicators.fetchrow(
                    "SELECT relowner,reloptions,pg_get_viewdef(oid) AS definition "
                    "FROM pg_class WHERE oid='public.rewards_stage_profile'::regclass"
                )
                == before
            )

    asyncio.run(scenario())


def test_orphan_reader_is_not_rotated(cluster):
    async def scenario():
        async with source_databases(cluster) as connections:
            rewards = connections["rewards"]
            assert await READERS.run(True) is False
            fingerprint = await password_fingerprint(rewards)
            await rewards.execute(
                f"DROP USER MAPPING FOR {ENGINE} SERVER subscription_srv"
            )
            with pytest.raises(RuntimeError, match="reader/mapping is incomplete"):
                await READERS.run(True)
            assert await password_fingerprint(rewards) == fingerprint
            assert await READERS.mapping_user(rewards, "subscription_srv") is None

    asyncio.run(scenario())


@pytest.mark.parametrize("grantee", [SUB_READER, "PUBLIC"])
def test_unapproved_contract_column_access_is_refused(cluster, grantee):
    async def scenario():
        async with source_databases(cluster) as connections:
            assert await READERS.run(True) is False
            await connections["subscription"].execute(
                f"GRANT SELECT(private_note) ON public.contract TO {grantee}"
            )
            with pytest.raises(RuntimeError, match="unapproved contract columns"):
                await READERS.run(False)
            with pytest.raises(RuntimeError, match="unapproved contract columns"):
                await READERS.run(True)

    asyncio.run(scenario())


def test_source_failure_rolls_back_new_role_but_leaves_prior_source_commits(cluster):
    async def scenario():
        async with source_databases(cluster) as connections:
            subscription = connections["subscription"]
            await subscription.execute(
                "ALTER TABLE public.contract RENAME COLUMN valid_to TO incompatible"
            )
            with pytest.raises(asyncpg.UndefinedColumnError):
                await READERS.run(True)
            assert await password_fingerprint(connections["rewards"]) is None
            assert (
                await READERS.mapping_user(connections["rewards"], "subscription_srv")
                is None
            )
            assert (
                await subscription.fetchval(
                    "SELECT to_regclass('public.rewards_contract_history')"
                )
                is None
            )
            for source in READERS.SOURCES[:2]:
                assert (
                    await connections[source.database].fetchval(
                        "SELECT to_regclass($1)", f"public.{source.view}"
                    )
                    is not None
                )
            await subscription.execute(
                "ALTER TABLE public.contract RENAME COLUMN incompatible TO valid_to"
            )
            assert await READERS.run(True) is False

    asyncio.run(scenario())
