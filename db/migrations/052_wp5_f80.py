"""Manual WP-5 F80 migration; default is a read-only preflight.

REWARDS_URL must be an owner connection to rewards. --apply requires a new
--rollback file outside the repository. Reader credentials/grants must already
be provisioned as documented in docs/data/tables.md. No reward function is
called by this runner, including postflight. Historical balances are untouched.
"""

import argparse
import asyncio
import hashlib
import os
import sys
from pathlib import Path

import asyncpg

ENGINE = "rewards_points_engine_owner"
COMPUTE = "public.compute_effective_amount_v4(uuid,bigint,text,jsonb,timestamptz)"
STAGE = "public._lookup_student_stage(uuid)"
EXPECTED = {
    COMPUTE: "3d520da75c582c638c60d30a89253fa2a368e4f767c7544f43558f8a946cf49c",
    STAGE: "82f22236665af1cd28fa461a79115bdbad9d6c131eefb2610ae78b7f9b2a24fa",
}
SQL_FILE = Path(__file__).with_suffix(".sql")

# Explicit minimal columns. Existing servers/mappings are reused, never replaced.
FOREIGN_TABLES = (
    (
        "_foreign_learning",
        "stage_transitions",
        "learning_srv",
        "public",
        "rewards_confirmed_stages",
        "account_id uuid, to_stage smallint, occurred_at timestamptz",
        "account_id",
    ),
    (
        "_foreign_indicators",
        "digital_twins",
        "indicators_srv",
        "public",
        "rewards_stage_profile",
        "user_id text, data jsonb",
        "user_id",
    ),
    (
        "_foreign_subscription",
        "contract_event",
        "subscription_srv",
        "public",
        "rewards_contract_history",
        "event_id bigint, contract_id uuid, event_type text, occurred_at timestamptz, snapshot jsonb",
        "event_id",
    ),
)
READ_PROBES = (
    (
        "_foreign_learning.domain_event",
        "id",
        "id,external_id,occurred_at,account_id,event_type",
    ),
    (
        "_foreign_learning.stage_transitions",
        "account_id",
        "account_id,to_stage,occurred_at",
    ),
    (
        "_foreign_indicators.calculated_profile",
        "account_id",
        "account_id,rcs_current,indicators",
    ),
    ("_foreign_indicators.digital_twins", "user_id", "user_id,data"),
    (
        "_foreign_subscription.contract",
        "contract_id",
        "contract_id,account_id,valid_from,valid_to,status",
    ),
    (
        "_foreign_subscription.contract_event",
        "event_id",
        "event_id,contract_id,event_type,occurred_at,snapshot",
    ),
)


async def read_baseline(conn):
    """Refuse to overwrite concurrent or unreviewed changes to the functions."""
    result = {}
    for signature, expected in EXPECTED.items():
        definition = await conn.fetchval(
            "SELECT pg_get_functiondef($1::regprocedure)", signature
        )
        actual = hashlib.sha256(definition.strip().encode()).hexdigest()
        if actual != expected:
            raise RuntimeError(
                f"Function changed or migration already applied: {signature}; review first"
            )
        result[signature] = definition
    owner = await conn.fetchval(
        "SELECT proowner::regrole::text FROM pg_proc WHERE oid=$1::regprocedure",
        COMPUTE,
    )
    if owner != ENGINE:
        raise RuntimeError("Unexpected accrual function owner")
    return result


async def check_mappings(conn):
    for server in ("learning_srv", "indicators_srv", "subscription_srv"):
        present = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_user_mapping m "
            "JOIN pg_foreign_server s ON s.oid=m.umserver "
            "WHERE s.srvname=$1 AND m.umuser=$2::regrole)",
            server,
            ENGINE,
        )
        if not present:
            raise RuntimeError(f"Missing dedicated engine user mapping: {server}")


async def setup_foreign_tables(conn):
    for (
        schema,
        table,
        server,
        remote_schema,
        remote_table,
        columns,
        _,
    ) in FOREIGN_TABLES:
        name = f"{schema}.{table}"
        exists = await conn.fetchval("SELECT to_regclass($1)", name)
        if not exists:
            await conn.execute(
                f"CREATE FOREIGN TABLE {name} ({columns}) SERVER {server} "
                f"OPTIONS (schema_name '{remote_schema}', table_name '{remote_table}')"
            )
        row = await conn.fetchrow(
            "SELECT s.srvname,f.ftoptions FROM pg_foreign_table f "
            "JOIN pg_foreign_server s ON s.oid=f.ftserver WHERE f.ftrelid=$1::regclass",
            name,
        )
        required = {f"schema_name={remote_schema}", f"table_name={remote_table}"}
        if (
            not row
            or row["srvname"] != server
            or not required.issubset(row["ftoptions"])
        ):
            raise RuntimeError(f"Unexpected foreign relation: {name}")
        await conn.execute(f"GRANT USAGE ON SCHEMA {schema} TO {ENGINE}")
        await conn.execute(f"GRANT SELECT ON {name} TO {ENGINE}")
    await conn.execute(f"GRANT USAGE ON SCHEMA _foreign_subscription TO {ENGINE}")
    await conn.execute(f"GRANT SELECT ON _foreign_subscription.contract TO {ENGINE}")


async def verify_owner_reads(conn):
    """Read identical sentinel rows; catalog grants alone cannot prove RLS visibility."""
    sentinels = []
    # SQL identifiers come only from the literal READ_PROBES allowlist, never
    # from a caller or database row. Row values remain bound parameters.
    for table, key, columns in READ_PROBES:
        value = await conn.fetchval(f"SELECT {key} FROM {table} LIMIT 1")  # nosec B608
        if value is None and not table.endswith(".contract_event"):
            raise RuntimeError(
                f"No sentinel available for {table}; inspect before rollout"
            )
        sentinels.append((table, key, columns, value))
    # A failed read rolls back this savepoint and its role change. Issuing RESET
    # in a failed transaction would mask the original privilege/connection error.
    async with conn.transaction():
        await conn.execute(f"SET LOCAL ROLE {ENGINE}")
        for table, key, columns, value in sentinels:
            if value is None:
                # History is currently empty. The source precondition must prove
                # RLS is disabled or has an explicit SELECT policy for the reader.
                await conn.fetchrow(f"SELECT {columns} FROM {table} LIMIT 1")  # nosec B608
            else:
                visible = await conn.fetchrow(
                    f"SELECT {columns} FROM {table} WHERE {key}=$1 LIMIT 1",  # nosec B608
                    value,
                )
                if not visible:
                    raise RuntimeError(f"Engine cannot see admin sentinel in {table}")
        await conn.execute("RESET ROLE")


def write_rollback(path, baseline):
    path = Path(path).resolve()
    if path.is_relative_to(Path(__file__).resolve().parents[2]):
        raise RuntimeError("Rollback must be outside the repository")
    sql = (
        "BEGIN;\n"
        + "\n".join(v.rstrip() + ";" for v in baseline.values())
        + "\nCOMMIT;\n"
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(sql)


async def run(args):
    dsn = os.environ.get("REWARDS_URL")
    if not dsn:
        raise RuntimeError("REWARDS_URL is required")
    conn = await asyncpg.connect(dsn, statement_cache_size=0, command_timeout=60)
    try:
        if await conn.fetchval("SELECT current_database()") != "rewards":
            raise RuntimeError("Expected rewards database")
        async with conn.transaction(readonly=not args.apply):
            # Set before long statements, not inside the accrual function: changing
            # statement_timeout inside a running statement does not rearm its timer.
            await conn.execute("SET LOCAL statement_timeout='45s'")
            if args.apply:
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended('wp5:f80:migration',0))"
                )
            baseline = await read_baseline(conn)
            await check_mappings(conn)
            if not args.apply:
                print("Baseline and engine mappings verified; no changes made.")
                return
            write_rollback(args.rollback, baseline)
            await setup_foreign_tables(conn)
            await verify_owner_reads(conn)
            await conn.execute(SQL_FILE.read_text())
            for signature in (
                "public._wp5_stage_value(jsonb)",
                "public._wp5_stage_evidence(uuid)",
                "public._wp5_subscription_evidence(uuid,timestamptz)",
            ):
                await conn.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
                await conn.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {ENGINE}")
            # Replacement preserves the existing owner and caller ACL.
            owner = await conn.fetchval(
                "SELECT proowner::regrole::text FROM pg_proc WHERE oid=$1::regprocedure",
                COMPUTE,
            )
            if owner != ENGINE:
                raise RuntimeError("Accrual ownership changed unexpectedly")
            await verify_owner_reads(conn)
        print(
            "Function migration committed; no accruals or balance corrections were run."
        )
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--rollback", help="new file outside repository, required for --apply"
    )
    args = parser.parse_args()
    if args.apply and not args.rollback:
        parser.error("--apply requires --rollback")
    try:
        asyncio.run(run(args))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:  # noqa: BLE001 - sanitize all failures at the CLI boundary
        # Driver diagnostics can expose connection options. Keep logs sanitized.
        print(
            f"Migration failed ({type(exc).__name__}); inspect database state before retrying.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
