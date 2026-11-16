"""Prepare narrow source views and FDW readers for WP-5 F80.

Default: read-only inventory. Explicit --apply creates the reviewed views and
grants, then the engine's subscription_srv mapping. REWARDS_URL must identify
the same cluster's neondb_owner account; database names are replaced with
learning/indicators/subscription. No reward function or balance write is run.

Mappings used by the main migration:
  learning_srv -> rewards_learning_fdw_reader -> public.rewards_confirmed_stages
  indicators_srv -> rewards_indicators_fdw_reader -> public.rewards_stage_profile
  subscription_srv -> rewards_subscription_fdw_reader -> contract and
                      public.rewards_contract_history

The subscription reader password is generated only when both the role and its
mapping are absent. It stays in memory until stored in PostgreSQL's role and
user-mapping catalogs. This program never prints DSNs, passwords, or SQL errors.
Existing credentials are never rotated. An existing reader without its mapping
requires operator recovery; this program refuses to guess its password.

Cross-database commits are not atomic. Source views/read grants may remain if a
later step fails. For recovery, inspect the named objects; do not retry by
rotating a reader already in use. For rollback, first restore the old rewards
functions and remove their new foreign-table dependencies, then drop only the
three views created here. Revoke the subscription reader's five contract-column
SELECT grants, public USAGE and subscription CONNECT; drop the engine's
subscription_srv user mapping before dropping a newly created, otherwise unused
rewards_subscription_fdw_reader role. Existing learning/indicators readers and
their other grants must remain. No automatic destructive cleanup is attempted.
"""

import argparse
import asyncio
import os
import secrets
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import asyncpg

OWNER = "neondb_owner"
ENGINE = "rewards_points_engine_owner"
SUB_READER = "rewards_subscription_fdw_reader"
CONTRACT_COLUMNS = ("contract_id", "account_id", "valid_from", "valid_to", "status")


@dataclass(frozen=True)
class Source:
    database: str
    reader: str
    server: str
    view: str
    select_sql: str
    canonical_sql: str


SOURCES = (
    Source(
        "learning",
        "rewards_learning_fdw_reader",
        "learning_srv",
        "rewards_confirmed_stages",
        "SELECT account_id,to_stage,occurred_at FROM learning.stage_transitions",
        "SELECT account_id, to_stage, occurred_at FROM learning.stage_transitions;",
    ),
    Source(
        "indicators",
        "rewards_indicators_fdw_reader",
        "indicators_srv",
        "rewards_stage_profile",
        "SELECT user_id,jsonb_build_object('3_derived',jsonb_build_object("
        "'3_4_qualification',jsonb_build_object("
        "'stage_id',data#>'{3_derived,3_4_qualification,stage_id}',"
        "'stage',data#>'{3_derived,3_4_qualification,stage}',"
        "'stage_source',data#>'{3_derived,3_4_qualification,stage_source}',"
        "'rcs_version',data#>'{3_derived,3_4_qualification,rcs_version}'))) AS data "
        "FROM public.digital_twins",
        "SELECT user_id, jsonb_build_object('3_derived', jsonb_build_object("
        "'3_4_qualification', jsonb_build_object("
        "'stage_id', data #> '{3_derived,3_4_qualification,stage_id}'::text[], "
        "'stage', data #> '{3_derived,3_4_qualification,stage}'::text[], "
        "'stage_source', data #> '{3_derived,3_4_qualification,stage_source}'::text[], "
        "'rcs_version', data #> '{3_derived,3_4_qualification,rcs_version}'::text[]))) "
        "AS data FROM public.digital_twins;",
    ),
    Source(
        "subscription",
        SUB_READER,
        "subscription_srv",
        "rewards_contract_history",
        "SELECT event_id,contract_id,event_type,occurred_at,jsonb_build_object("
        "'status',snapshot->'status','valid_from',snapshot->'valid_from',"
        "'valid_to',snapshot->'valid_to') AS snapshot FROM public.contract_event",
        "SELECT event_id, contract_id, event_type, occurred_at, jsonb_build_object("
        "'status', snapshot -> 'status'::text, 'valid_from', snapshot -> 'valid_from'::text, "
        "'valid_to', snapshot -> 'valid_to'::text) AS snapshot FROM public.contract_event;",
    ),
)


def database_url(dsn, database):
    parts = urlsplit(dsn)
    if parts.scheme not in {"postgres", "postgresql"} or not parts.hostname:
        raise RuntimeError("REWARDS_URL must be a PostgreSQL owner URL")
    if parts.path != "/rewards":
        raise RuntimeError("REWARDS_URL must target the rewards database")
    return urlunsplit(parts._replace(path=f"/{database}"))


def normalized_sql(value):
    # Exact pg_get_viewdef(..., true) output, checked on PostgreSQL 16 with
    # search_path=pg_catalog. Only insignificant whitespace is removed.
    return "".join(value.split()).rstrip(";")


async def connect(stack, dsn, database, *, readonly):
    conn = await asyncpg.connect(
        database_url(dsn, database),
        statement_cache_size=0,
        command_timeout=45,
        server_settings={"default_transaction_read_only": "on" if readonly else "off"},
    )
    stack.push_async_callback(conn.close)
    row = await conn.fetchrow(
        "SELECT current_database() AS db,current_user AS role,rolbypassrls "
        "FROM pg_roles WHERE rolname=current_user"
    )
    if row["db"] != database or row["role"] != OWNER or not row["rolbypassrls"]:
        raise RuntimeError(f"Expected {database} owner with RLS bypass")
    return conn


async def role_exists(conn, role):
    row = await conn.fetchrow(
        "SELECT rolcanlogin,rolinherit,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
        "FROM pg_roles WHERE rolname=$1",
        role,
    )
    if row is None:
        return False
    if not row["rolcanlogin"] or any(
        row[key]
        for key in (
            "rolsuper",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolbypassrls",
        )
    ):
        raise RuntimeError(f"Reader has unexpected login/admin attributes: {role}")
    if role == SUB_READER and (
        row["rolinherit"]
        or await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=$1::regrole)",
            role,
        )
    ):
        raise RuntimeError(
            "Subscription reader must have NOINHERIT and no role memberships"
        )
    return True


async def mapping_user(conn, server):
    # Do not fetch password options, even for inspection.
    return await conn.fetchval(
        "SELECT coalesce((SELECT substring(opt FROM 6) FROM unnest(m.umoptions) opt "
        "WHERE opt LIKE 'user=%'),'') FROM pg_user_mapping m "
        "JOIN pg_foreign_server s ON s.oid=m.umserver "
        "WHERE m.umuser=$1::regrole AND s.srvname=$2",
        ENGINE,
        server,
    )


async def check_view(conn, source):
    await conn.execute("SET LOCAL search_path=pg_catalog")
    row = await conn.fetchrow(
        "SELECT c.relkind::text AS relkind,c.relowner::regrole::text AS owner,c.reloptions,"
        "CASE WHEN c.relkind='v' THEN pg_get_viewdef(c.oid,true) END AS definition "
        "FROM pg_class c WHERE c.oid=to_regclass($1)",
        f"public.{source.view}",
    )
    if row is None:
        return False
    options = set(row["reloptions"] or ())
    if (
        row["relkind"] != "v"
        or row["owner"] != OWNER
        or options != {"security_barrier=true"}
        or normalized_sql(row["definition"]) != normalized_sql(source.canonical_sql)
    ):
        raise RuntimeError(
            f"Unknown existing view definition/owner/options: public.{source.view}"
        )
    return True


async def inventory(connections):
    rewards = connections["rewards"]
    missing = []
    async with rewards.transaction(readonly=True):
        if not await rewards.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=$1)", ENGINE
        ):
            raise RuntimeError("Accrual engine owner is absent")
        for source in SOURCES:
            if not await rewards.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_foreign_server WHERE srvname=$1)",
                source.server,
            ):
                raise RuntimeError(
                    f"Expected foreign server is absent: {source.server}"
                )
            mapped = await mapping_user(rewards, source.server)
            if mapped not in (None, source.reader):
                raise RuntimeError(f"Unexpected engine mapping user on {source.server}")
            if mapped is None:
                if source.database != "subscription":
                    raise RuntimeError(
                        f"Existing engine mapping is absent: {source.server}"
                    )
                missing.append("subscription engine mapping")
        if not await rewards.fetchval(
            "SELECT has_server_privilege($1,'subscription_srv','USAGE')", ENGINE
        ):
            missing.append("engine USAGE on subscription_srv")
    for source in SOURCES:
        conn = connections[source.database]
        async with conn.transaction(readonly=True):
            exists = await role_exists(conn, source.reader)
            mapped = await mapping_user(rewards, source.server)
            if source.database == "subscription" and exists != (mapped is not None):
                raise RuntimeError(
                    "Subscription reader/mapping is incomplete. Inspect partial rollout; "
                    "restore the matching mapping or remove only an unused orphan role before retrying."
                )
            if not exists:
                if source.database != "subscription":
                    raise RuntimeError(
                        f"Expected existing reader is absent: {source.reader}"
                    )
                missing.append("subscription reader role")
            if not await check_view(conn, source):
                missing.append(f"{source.database}.public.{source.view}")
            elif exists and not await conn.fetchval(
                "SELECT has_table_privilege($1,$2,'SELECT')",
                source.reader,
                f"public.{source.view}",
            ):
                missing.append(f"SELECT on {source.database}.public.{source.view}")
            if exists and source.database == "subscription":
                excessive = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM pg_attribute "
                    "WHERE attrelid='public.contract'::regclass AND attnum>0 "
                    "AND NOT attisdropped AND NOT(attname=ANY($2::text[])) "
                    "AND has_column_privilege($1,attrelid,attname,'SELECT'))",
                    source.reader,
                    list(CONTRACT_COLUMNS),
                )
                if excessive:
                    raise RuntimeError(
                        "Subscription reader can access unapproved contract columns"
                    )
                for column in CONTRACT_COLUMNS:
                    if not await conn.fetchval(
                        "SELECT has_column_privilege($1,'public.contract',$2,'SELECT')",
                        source.reader,
                        column,
                    ):
                        missing.append(f"subscription.contract.{column} SELECT")
                if not await conn.fetchval(
                    "SELECT has_database_privilege($1,'subscription','CONNECT')",
                    source.reader,
                ):
                    missing.append("subscription CONNECT")
            if exists and not await conn.fetchval(
                "SELECT has_schema_privilege($1,'public','USAGE')", source.reader
            ):
                missing.append(f"{source.database}.public USAGE")
    return missing


async def apply_sources(connections):
    """Commit each source before installing the mapping; no distributed atomicity."""
    password = None
    for source in SOURCES:
        conn = connections[source.database]
        async with conn.transaction():
            await conn.execute("SET LOCAL statement_timeout='30s'")
            if source.database == "subscription" and not await role_exists(
                conn, source.reader
            ):
                password = secrets.token_urlsafe(48)
                # Password alphabet is generated locally (URL-safe), never user SQL.
                await conn.execute(
                    f"CREATE ROLE {SUB_READER} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                    f"NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '{password}'"
                )
            if not await check_view(conn, source):
                await conn.execute(
                    f"CREATE VIEW public.{source.view} WITH(security_barrier=true) AS {source.select_sql}"
                )
                # Verify the exact definition before committing it.
                await check_view(conn, source)
            await conn.execute(f"REVOKE ALL ON public.{source.view} FROM PUBLIC")
            await conn.execute(f"GRANT USAGE ON SCHEMA public TO {source.reader}")
            await conn.execute(
                f"GRANT SELECT ON public.{source.view} TO {source.reader}"
            )
            if source.database == "subscription":
                await conn.execute(
                    f"GRANT CONNECT ON DATABASE subscription TO {SUB_READER}"
                )
                await conn.execute(
                    f"GRANT SELECT ({','.join(CONTRACT_COLUMNS)}) ON public.contract TO {SUB_READER}"
                )
    rewards = connections["rewards"]
    async with rewards.transaction():
        if password is not None:
            if await mapping_user(rewards, "subscription_srv") is not None:
                raise RuntimeError(
                    "Subscription mapping appeared concurrently; inspect before continuing"
                )
            await rewards.execute(
                f"CREATE USER MAPPING FOR {ENGINE} SERVER subscription_srv "
                f"OPTIONS (user '{SUB_READER}',password '{password}')"
            )
        await rewards.execute(
            f"GRANT USAGE ON FOREIGN SERVER subscription_srv TO {ENGINE}"
        )
    password = None


async def run(apply):
    dsn = os.environ.get("REWARDS_URL")
    if not dsn:
        raise RuntimeError("REWARDS_URL is required")
    async with AsyncExitStack() as stack:
        connections = {}
        for database in ("rewards", "learning", "indicators", "subscription"):
            connections[database] = await connect(
                stack, dsn, database, readonly=not apply
            )
        missing = await inventory(connections)
        if not apply:
            print(
                "Read-only inventory: " + (", ".join(missing) if missing else "ready")
            )
            return bool(missing)
        await apply_sources(connections)
        missing = await inventory(connections)
        if missing:
            raise RuntimeError("Reader setup incomplete: " + ", ".join(missing))
        print(
            "Narrow source readers prepared; run the main migration's engine visibility checks next."
        )
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        missing = asyncio.run(run(args.apply))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:  # noqa: BLE001 - never expose generated credentials
        # asyncpg errors can carry connection strings or SQL containing a mapping
        # credential. Emit only the class, never the driver message or traceback.
        print(
            f"Reader setup failed ({type(exc).__name__}); inspect named objects before retrying.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    raise SystemExit(2 if missing else 0)


if __name__ == "__main__":
    main()
