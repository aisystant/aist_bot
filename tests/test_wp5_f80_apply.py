"""Real PostgreSQL checks for the one-off historical settlement; fictional data."""

import asyncio
import copy
import importlib.util
import json
import shlex
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest

_fixture_spec = importlib.util.spec_from_file_location(
    "f80_test_fixtures", Path(__file__).with_name("test_wp5_f80.py")
)
_fixture_module = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture_module)
ACCOUNT = _fixture_module.ACCOUNT
pg_server = _fixture_module.pg_server
db = _fixture_module.db

MODULE = Path(__file__).resolve().parents[1] / "scripts/wp5_f80_apply.py"
spec = importlib.util.spec_from_file_location("f80_apply", MODULE)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


async def connect(database):
    options = dict(word.split("=", 1) for word in shlex.split(database.dsn))
    options["database"] = options.pop("dbname")
    return await asyncpg.connect(**options)


async def fixture_plan(database):
    database.profile(qualification=5)
    for key in (1, 2, 3):
        database.event(key, external_id=f"event-{key}")
    database.execute(
        "ALTER TABLE _foreign_learning.domain_event ADD COLUMN ingested_at timestamptz DEFAULT '2026-01-02', ADD COLUMN payload jsonb DEFAULT '{}'"
    )
    database.execute(f"""
      INSERT INTO public.applied_events(event_id,account_id,event_type,effective,bonuses_eligible,applied_at)
      VALUES(1,'{ACCOUNT}','practice',0,false,'2026-01-02'),(3,'{ACCOUNT}','practice',7,false,'2026-01-02');
      INSERT INTO public.point_balances(account_id,points,earned_total) VALUES('{ACCOUNT}',100,100);
    """)
    conn = await connect(database)
    plan = {
        "schema": 1,
        "phase": "WP-5-F80",
        "snapshot_at": "2026-12-31T00:00:00+00:00",
        "items": [],
        "earned": "15",
        "bonuses": "12",
        "accounts": [ACCOUNT],
        "calculation_accounts": [ACCOUNT],
        "payload_values": {},
        "last_occurrence": {ACCOUNT: "2026-12-31T00:00:00+00:00"},
    }
    try:
        for key, kind, earned, bonus in (
            (1, "zero", "10", "0"),
            (2, "new", "5", "5"),
            (3, "bonus", "0", "7"),
        ):
            before = await conn.fetchrow(
                "SELECT * FROM applied_events WHERE event_id=$1", key
            )
            source = dict(
                await conn.fetchrow(
                    "SELECT * FROM _foreign_learning.domain_event WHERE id=$1", key
                )
            )
            source = {
                k: source[k]
                for k in (
                    "id",
                    "account_id",
                    "event_type",
                    "occurred_at",
                    "ingested_at",
                    "external_id",
                    "payload",
                )
            }
            source["external_id"] = app.external_hash(source["external_id"])
            source["payload"] = json.loads(source["payload"])
            item = {
                "event_id": key,
                "account_id": ACCOUNT,
                "kind": kind,
                "earned": earned,
                "bonuses": bonus,
                "before": app.ledger_row(before),
                "source": app.normalized(source),
            }
            if kind != "bonus":
                after = {k: None for k in (*app.LEDGER_COLUMNS, "payload_snapshot")}
                after.update(
                    event_id=key,
                    account_id=ACCOUNT,
                    event_type="practice",
                    effective=earned,
                    bonuses_eligible=bool(int(bonus)),
                    applied_at=item["source"]["ingested_at"],
                    payload_snapshot={"occurred_at": item["source"]["occurred_at"]},
                )
                after["external_id"] = source["external_id"]
                item["after"] = after
                item["after_normalized"] = {k: after[k] for k in app.LEDGER_COLUMNS}
                item["after_normalized"]["external_id"] = source["external_id"]
            plan["items"].append(item)
        plan["dependencies"] = await app.dependencies(conn, plan)
        plan["ledger_digest"] = app.rows_digest(
            [
                app.ledger_row(r)
                for r in await conn.fetch("SELECT * FROM applied_events")
            ]
        )
        plan["trigger_digest"] = app.digest(await app.trigger_definition(conn))
    finally:
        await conn.close()
    return plan


def test_settlement_exact_credit_replay_and_restored_protection(db):
    async def check():
        plan = await fixture_plan(db)
        conn = await connect(db)
        try:
            await conn.execute(
                "REVOKE ALL ON FUNCTION trg_point_balances_track_earned() FROM PUBLIC; GRANT EXECUTE ON FUNCTION trg_point_balances_track_earned() TO rewards_points_engine_owner; COMMENT ON FUNCTION trg_point_balances_track_earned() IS 'custom ACL preservation fixture'"
            )
            original = await app.trigger_state(conn)
            positive = await conn.fetchval(
                "SELECT to_jsonb(a) FROM applied_events a WHERE event_id=3"
            )
            assert await app.settle(conn, plan) == "applied"
            balance = await conn.fetchrow(
                "SELECT points,earned_total FROM point_balances"
            )
            assert (balance["points"], balance["earned_total"]) == (112, 115)
            assert (
                await conn.fetchval(
                    "SELECT to_jsonb(a) FROM applied_events a WHERE event_id=3"
                )
                == positive
            )
            assert await app.trigger_state(conn) == original
            assert (
                await conn.fetchval(
                    "SELECT external_id FROM applied_events WHERE event_id=2"
                )
                == "event-2"
            )
            assert await app.settle(conn, plan) == "already_applied"
            assert await conn.fetchval("SELECT sum(bonuses) FROM wp5_f80_items") == 12
            changed = copy.deepcopy(plan)
            changed["notes"] = "different approval"
            with pytest.raises(RuntimeError, match="different settlement"):
                await app.settle(conn, changed)
            # The ordinary protection remains active after the correction.
            await conn.execute("UPDATE point_balances SET earned_total=9999")
            assert await conn.fetchval("SELECT earned_total FROM point_balances") == 115
        finally:
            await conn.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    "failure",
    [
        "after_balances",
        "source_drift",
        "dependency_drift",
        "semantic_collision",
        "ledger_trigger",
        "balance_constraint",
    ],
)
def test_any_failure_rolls_back_all_money_audit_and_ddl(db, failure):
    async def check():
        plan = await fixture_plan(db)
        conn = await connect(db)
        try:
            original = await app.trigger_state(conn)
            if failure == "balance_constraint":
                plan["items"] = [i for i in plan["items"] if i["kind"] == "bonus"]
                plan["earned"], plan["bonuses"] = "0", "7"
                plan["calculation_accounts"] = []
            elif failure == "source_drift":
                await conn.execute(
                    "UPDATE _foreign_learning.domain_event SET occurred_at=occurred_at+interval '1 hour' WHERE id=1"
                )
            elif failure == "dependency_drift":
                await conn.execute(
                    "UPDATE _foreign_reference.reward_rules SET amount=99"
                )
            elif failure == "semantic_collision":
                await conn.execute(
                    "INSERT INTO applied_events(event_id,account_id,event_type,external_id,applied_at) VALUES(9,$1,'practice','event-2','2027-01-01')",
                    UUID("00000000-0000-0000-0000-000000000099"),
                )
            elif failure == "ledger_trigger":
                await conn.execute("""CREATE FUNCTION change_date() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.applied_at=now(); RETURN NEW; END $$;
                CREATE TRIGGER unexpected_date BEFORE INSERT ON applied_events FOR EACH ROW EXECUTE FUNCTION change_date();""")
            with pytest.raises((RuntimeError, asyncpg.UniqueViolationError)):
                await app.settle(
                    conn, plan, fail_after_balances=failure == "after_balances"
                )
            assert await conn.fetchval("SELECT points FROM point_balances") == 100
            assert await conn.fetchval("SELECT earned_total FROM point_balances") == 100
            assert (
                await conn.fetchval(
                    "SELECT effective FROM applied_events WHERE event_id=1"
                )
                == 0
            )
            assert not await conn.fetchval(
                "SELECT to_regclass('public.wp5_f80_batches') IS NOT NULL"
            )
            assert await app.trigger_state(conn) == original
        finally:
            await conn.close()

    asyncio.run(check())


def test_busy_wallet_aborts_without_blocking_existing_writer(db):
    async def check():
        plan = await fixture_plan(db)
        conn, other = await connect(db), await connect(db)
        try:
            async with other.transaction():
                await other.execute("UPDATE point_balances SET points=points-1")
                with pytest.raises(asyncpg.LockNotAvailableError):
                    await app.settle(conn, plan)
                # No ledger lock survives the failed settlement transaction.
                await other.execute("SET LOCAL lock_timeout='100ms'")
                await other.execute(
                    "UPDATE applied_events SET effective=effective WHERE event_id=3"
                )
            assert await conn.fetchval("SELECT points FROM point_balances") == 99
            assert await app.settle(conn, plan) == "applied"
            assert await conn.fetchval("SELECT points FROM point_balances") == 111
        finally:
            await conn.close()
            await other.close()

    asyncio.run(check())


def test_bonus_only_preserves_earned_under_non_superuser_noinherit_owner(db):
    async def check():
        plan = await fixture_plan(db)
        conn = await connect(db)
        try:
            await conn.execute("UPDATE point_balances SET points=90")
            plan["items"] = [i for i in plan["items"] if i["kind"] == "bonus"]
            plan["earned"], plan["bonuses"] = "0", "7"
            plan["calculation_accounts"] = []
            plan["dependencies"] = await app.dependencies(conn, plan)
            await conn.execute("""DO $$ BEGIN
              IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='wp5_f80_fixture_admin') THEN
                CREATE ROLE wp5_f80_fixture_admin NOLOGIN NOINHERIT NOSUPERUSER;
              END IF; END $$;
              ALTER TABLE applied_events OWNER TO wp5_f80_fixture_admin;
              ALTER TABLE point_balances OWNER TO wp5_f80_fixture_admin;
              ALTER FUNCTION trg_point_balances_track_earned() OWNER TO wp5_f80_fixture_admin;
              GRANT USAGE,CREATE ON SCHEMA public TO wp5_f80_fixture_admin;
              GRANT USAGE ON SCHEMA _foreign_learning,_foreign_indicators,_foreign_subscription,_foreign_reference TO wp5_f80_fixture_admin;
              GRANT SELECT ON ALL TABLES IN SCHEMA _foreign_learning,_foreign_indicators,_foreign_subscription,_foreign_reference TO wp5_f80_fixture_admin;
              GRANT rewards_points_engine_owner TO wp5_f80_fixture_admin WITH INHERIT FALSE;
              SET ROLE wp5_f80_fixture_admin;""")
            assert not await conn.fetchval(
                "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
            )
            assert not await conn.fetchval(
                "SELECT rolinherit FROM pg_roles WHERE rolname=current_user"
            )
            original = await app.trigger_state(conn)
            assert await app.settle(conn, plan) == "applied"
            balance = await conn.fetchrow(
                "SELECT points,earned_total FROM point_balances"
            )
            assert (balance["points"], balance["earned_total"]) == (97, 100)
            assert await app.trigger_state(conn) == original
            assert await conn.fetchval("SELECT count(*) FROM wp5_f80_items") == 1
        finally:
            await conn.close()

    asyncio.run(check())
