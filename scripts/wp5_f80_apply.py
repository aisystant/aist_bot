"""Apply an explicitly approved, frozen WP-5 F80 settlement. Default: read only.

The private JSON manifest contains pseudonymous account/event identifiers and must
stay outside the repository. No production recalculation or identity migration.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import asyncpg

TRIGGER = "public.trg_point_balances_track_earned()"
LEDGER_COLUMNS = (
    "event_id",
    "account_id",
    "event_type",
    "base_amount",
    "dom_mult",
    "qual_mult",
    "streak_mult",
    "daily_cap",
    "raw_amount",
    "effective",
    "cap_truncated",
    "bonuses_eligible",
    "applied_at",
    "rule_id",
    "external_id",
)
MONEY_COLUMNS = {
    "base_amount",
    "dom_mult",
    "qual_mult",
    "streak_mult",
    "daily_cap",
    "raw_amount",
    "effective",
}
# Literal queries only; a manifest cannot supply SQL or identifiers.
DEPENDENCIES = {
    "rules": "SELECT rule_id,trigger_event,reward_kind,amount,effort_minutes,is_marker,max_per_day,group_mult,rarity_mult,streak_eligible,valid_from,valid_to,match_condition FROM _foreign_reference.reward_rules WHERE reward_kind='points'",
    "levels": "SELECT level_number,qual_mult,action_cap FROM _foreign_reference.qualification_levels_v4",
    "config": "SELECT use_v4_formula,k,valid_to FROM _foreign_reference.loyalty_pool_config",
    "stages": "SELECT account_id,to_stage,occurred_at FROM _foreign_learning.stage_transitions WHERE account_id=ANY($1::uuid[]) AND occurred_at<=$2::timestamptz",
    "contracts": "SELECT contract_id,account_id,valid_from,valid_to,status FROM _foreign_subscription.contract WHERE account_id=ANY($1::uuid[])",
    "lifecycle": "SELECT event_id,contract_id,event_type,occurred_at,snapshot FROM _foreign_subscription.contract_event WHERE contract_id IN(SELECT contract_id FROM _foreign_subscription.contract WHERE account_id=ANY($1::uuid[]))",
    "qualifications": "SELECT a AS account_id,public._lookup_qualification_level(a) AS qualification,CASE WHEN public._lookup_qualification_level(a)=4 THEN public._wp5_stage_evidence(a) ELSE '{}'::jsonb END AS stage FROM unnest($1::uuid[]) a",
}
AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS public.wp5_f80_batches (
 digest text PRIMARY KEY, phase text NOT NULL CHECK(phase='WP-5-F80'),
 earned numeric NOT NULL, bonuses numeric NOT NULL, items integer NOT NULL,
 committed_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE IF NOT EXISTS public.wp5_f80_items (
 event_id bigint NOT NULL, kind text NOT NULL, digest text NOT NULL REFERENCES public.wp5_f80_batches,
 account_id uuid NOT NULL, earned numeric NOT NULL CHECK(earned>=0),
 bonuses numeric NOT NULL CHECK(bonuses>=0), occurred_at timestamptz NOT NULL,
 before_row jsonb, after_row jsonb, PRIMARY KEY(event_id,kind));
CREATE TABLE IF NOT EXISTS public.wp5_f80_balances (
 digest text NOT NULL REFERENCES public.wp5_f80_batches,account_id uuid NOT NULL,
 earned numeric NOT NULL,bonuses numeric NOT NULL,before_row jsonb,after_row jsonb,
 PRIMARY KEY(digest,account_id));
REVOKE ALL ON public.wp5_f80_batches,public.wp5_f80_items,public.wp5_f80_balances FROM PUBLIC;
"""


def normalized(value):
    if isinstance(value, (asyncpg.Record, dict)):
        return {str(k): normalized(v) for k, v in dict(value).items()}
    if isinstance(value, (list, tuple)):
        return [normalized(v) for v in value]
    if isinstance(value, Decimal):
        return format(value.normalize(), "f") if value else "0"
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def canonical(value):
    return json.dumps(
        normalized(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def rows_digest(rows):
    return digest(sorted((normalized(r) for r in rows), key=canonical))


def external_hash(value):
    if value is None or (value.startswith("backfill-") and value[9:].isdigit()):
        return value
    return hashlib.md5(value.encode(), usedforsecurity=False).hexdigest()


def ledger_row(row):
    if row is None:
        return None
    result = {k: row[k] for k in LEDGER_COLUMNS}
    result["external_id"] = external_hash(result["external_id"])
    return normalized(result)


def money(value):
    amount = Decimal(str(value))
    if (
        not amount.is_finite()
        or amount < 0
        or amount != amount.quantize(Decimal(".01"))
    ):
        raise ValueError("Invalid nonnegative currency amount")
    return amount


def validate(plan, approved_digest=None):
    if plan["schema"] != 1 or plan["phase"] != "WP-5-F80":
        raise ValueError("Unexpected settlement manifest")
    if approved_digest is not None and digest(plan) != approved_digest:
        raise ValueError("Approval digest mismatch")
    totals = defaultdict(lambda: [Decimal(0), Decimal(0)])
    seen = set()
    for item in plan["items"]:
        key = int(item["event_id"])
        if key <= 0 or key in seen or item["kind"] not in {"new", "zero", "bonus"}:
            raise ValueError("Duplicate or unsupported event claim")
        seen.add(key)
        account = str(UUID(item["account_id"]))
        earned, bonus = money(item["earned"]), money(item["bonuses"])
        if earned == bonus == 0 or bonus > earned and item["kind"] != "bonus":
            raise ValueError("Invalid settlement delta")
        source = item["source"]
        if int(source["id"]) != key or source["account_id"] != account:
            raise ValueError("Source ownership migration is outside this settlement")
        before = item["before"]
        if before is not None and int(before["event_id"]) != key:
            raise ValueError("Baseline event mismatch")
        if item["kind"] == "new" and before is not None:
            raise ValueError("New event already has a baseline")
        if item["kind"] != "new" and (not before or before["account_id"] != account):
            raise ValueError("Baseline account mismatch")
        if item["kind"] == "bonus":
            if (
                earned
                or bonus != money(before["effective"])
                or before["bonuses_eligible"]
            ):
                raise ValueError("Invalid bonus-only restoration")
        else:
            after = item["after"]
            if (
                int(after["event_id"]) != key
                or after["event_type"] != source["event_type"]
                or datetime.fromisoformat(after["applied_at"])
                != datetime.fromisoformat(source["ingested_at"])
                or {k: after.get(k) for k in LEDGER_COLUMNS} != item["after_normalized"]
                or after["external_id"] != source["external_id"]
            ):
                raise ValueError("Award source or receipt mismatch")
            if (
                after["account_id"] != account
                or money(after["effective"]) != earned
                or bonus != (earned if after["bonuses_eligible"] else 0)
            ):
                raise ValueError("Award and balance delta disagree")
            if item["kind"] == "zero" and money(before["effective"]) != 0:
                raise ValueError("Positive receipt cannot be repriced")
        totals[account][0] += earned
        totals[account][1] += bonus
    if sum(v[0] for v in totals.values()) != money(plan["earned"]) or sum(
        v[1] for v in totals.values()
    ) != money(plan["bonuses"]):
        raise ValueError("Manifest totals mismatch")
    if set(totals) != set(plan["accounts"]) or set(plan["calculation_accounts"]) != {
        i["account_id"] for i in plan["items"] if i["kind"] != "bonus"
    }:
        raise ValueError("Account scope mismatch")
    return totals


async def dependencies(conn, plan):
    accounts = [UUID(a) for a in plan["accounts"]]
    stage_at = datetime.fromisoformat(plan["snapshot_at"])
    result = {}
    for key, query in DEPENDENCIES.items():
        if key == "qualifications":
            params = [[UUID(a) for a in plan["calculation_accounts"]]]
        elif key == "stages":
            params = [accounts, stage_at]
        elif key in {"contracts", "lifecycle"}:
            params = [accounts]
        else:
            params = []
        result[key] = rows_digest(await conn.fetch(query, *params))
    return result


async def check_balances(conn, plan):
    totals = validate(plan)
    rows = await conn.fetch(
        "SELECT account_id,points,earned_total FROM public.point_balances WHERE account_id=ANY($1::uuid[])",
        [UUID(a) for a in totals],
    )
    if len(rows) != len(totals):
        raise RuntimeError("Recipient wallet missing")
    for row in rows:
        earned, bonus = totals[str(row["account_id"])]
        if row["points"] + bonus > row["earned_total"] + earned:
            raise RuntimeError(
                "Bonus restoration requires earned-counter reconciliation"
            )


async def preflight(conn, plan):
    permissions = await conn.fetchrow(
        "SELECT (SELECT bool_and(has_table_privilege(rel,priv)) FROM (VALUES('public.applied_events','SELECT'),('public.applied_events','INSERT'),('public.applied_events','UPDATE'),('public.point_balances','SELECT'),('public.point_balances','UPDATE')) required(rel,priv)) table_access,has_schema_privilege('public','CREATE') schema_create,(SELECT proowner::regrole::text=current_user FROM pg_proc WHERE oid=$1::regprocedure) trigger_owner",
        TRIGGER,
    )
    if not all(permissions.values()):
        raise RuntimeError(
            "Settlement requires the existing table and trigger administrator"
        )
    if digest(await trigger_definition(conn)) != plan["trigger_digest"]:
        raise RuntimeError("Balance protection function changed")
    await check_balances(conn, plan)
    if await dependencies(conn, plan) != plan["dependencies"]:
        raise RuntimeError("Frozen calculation dependencies changed")
    ids = [int(i["event_id"]) for i in plan["items"]]
    sources = {
        r["id"]: r
        for r in await conn.fetch(
            "SELECT id,account_id,event_type,occurred_at,ingested_at,external_id,payload FROM _foreign_learning.domain_event WHERE id=ANY($1::bigint[])",
            ids,
        )
    }
    ledger = {
        r["event_id"]: r
        for r in await conn.fetch(
            "SELECT * FROM public.applied_events WHERE event_id=ANY($1::bigint[])", ids
        )
    }
    accounts = [UUID(a) for a in plan["accounts"]]
    historical = await conn.fetch(
        "SELECT * FROM public.applied_events WHERE account_id=ANY($1::uuid[]) AND applied_at<=$2::timestamptz",
        accounts,
        datetime.fromisoformat(plan["snapshot_at"]),
    )
    if rows_digest([ledger_row(r) for r in historical]) != plan["ledger_digest"]:
        raise RuntimeError("Historical account ledger changed")
    # New post-snapshot grants may carry an old occurrence date. Refuse those
    # instead of silently sharing the old day's budget with stale results.
    late = await conn.fetch(
        "SELECT a.event_id,a.account_id,e.occurred_at FROM public.applied_events a LEFT JOIN _foreign_learning.domain_event e ON e.id=a.event_id WHERE a.account_id=ANY($1::uuid[]) AND a.applied_at>$2::timestamptz",
        accounts,
        datetime.fromisoformat(plan["snapshot_at"]),
    )
    for row in late:
        limit = datetime.fromisoformat(plan["last_occurrence"][str(row["account_id"])])
        if row["occurred_at"] is None or row["occurred_at"] <= limit:
            raise RuntimeError("Post-snapshot historical award requires recalculation")
    for item in plan["items"]:
        key = int(item["event_id"])
        source = sources.get(key)
        if source is None:
            raise RuntimeError("Source event missing")
        observed = normalized(
            {
                k: source[k]
                for k in (
                    "id",
                    "account_id",
                    "event_type",
                    "occurred_at",
                    "ingested_at",
                )
            }
        )
        observed["external_id"] = external_hash(source["external_id"])
        # Exactly the same rule-constant projection used in the read-only copy.
        payload = (
            json.loads(source["payload"])
            if isinstance(source["payload"], str)
            else source["payload"]
        )
        observed["payload"] = (
            None
            if payload is None
            else {
                k: v
                for k, v in payload.items()
                if k in plan["payload_values"] and v in plan["payload_values"][k]
            }
        )
        if observed != item["source"] or ledger_row(ledger.get(key)) != item["before"]:
            raise RuntimeError("Source or baseline receipt changed")
    return sources


async def prior_receipt(conn, plan_hash, event_ids):
    if not await conn.fetchval(
        "SELECT to_regclass('public.wp5_f80_batches') IS NOT NULL"
    ):
        return False
    if await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM public.wp5_f80_batches WHERE digest=$1)", plan_hash
    ):
        return True
    if await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM public.wp5_f80_items WHERE event_id=ANY($1::bigint[]) AND digest<>$2)",
        event_ids,
        plan_hash,
    ):
        raise RuntimeError("A different settlement already claimed an event")
    return False


async def trigger_state(conn):
    return dict(
        await conn.fetchrow(
            "SELECT prosrc,proconfig,prosecdef,provolatile,proisstrict,proleakproof,proparallel,proacl::text,proowner::text FROM pg_proc WHERE oid=$1::regprocedure",
            TRIGGER,
        )
    )


async def trigger_definition(conn):
    return await conn.fetchval("SELECT pg_get_functiondef($1::regprocedure)", TRIGGER)


async def write_awards(conn, plan, sources, plan_hash):
    await conn.execute("""CREATE TEMP TABLE wp5_f80_stage(
      event_id bigint PRIMARY KEY,kind text,account_id uuid,earned numeric,
      bonuses numeric,occurred_at timestamptz,award jsonb) ON COMMIT DROP""")
    records = []
    for item in plan["items"]:
        award = (
            {k: item["after"].get(k) for k in (*LEDGER_COLUMNS, "payload_snapshot")}
            if item["kind"] != "bonus"
            else {}
        )
        if award:
            award["external_id"] = sources[int(item["event_id"])]["external_id"]
        records.append(
            (
                int(item["event_id"]),
                item["kind"],
                UUID(item["account_id"]),
                money(item["earned"]),
                money(item["bonuses"]),
                datetime.fromisoformat(item["source"]["occurred_at"]),
                canonical(award),
            )
        )
    await conn.copy_records_to_table(
        "wp5_f80_stage", schema_name="pg_temp", records=records
    )
    await conn.execute(
        """INSERT INTO public.wp5_f80_items(event_id,kind,digest,account_id,earned,bonuses,occurred_at,before_row)
      SELECT s.event_id,s.kind,$1,s.account_id,s.earned,s.bonuses,s.occurred_at,to_jsonb(a)
      FROM pg_temp.wp5_f80_stage s LEFT JOIN public.applied_events a USING(event_id)""",
        plan_hash,
    )
    columns = (*LEDGER_COLUMNS, "payload_snapshot")
    # Identifiers derive only from this module's literal allowlist.
    projection = ",".join(f"r.{k}" for k in columns)
    await conn.execute(
        f"INSERT INTO public.applied_events({','.join(columns)}) SELECT {projection} FROM pg_temp.wp5_f80_stage s CROSS JOIN LATERAL jsonb_populate_record(NULL::public.applied_events,s.award) r WHERE s.kind='new'"  # nosec B608
    )
    assignment = ",".join(f"{k}=r.{k}" for k in columns if k != "event_id")
    await conn.execute(
        f"UPDATE public.applied_events a SET {assignment} FROM pg_temp.wp5_f80_stage s CROSS JOIN LATERAL jsonb_populate_record(NULL::public.applied_events,s.award) r WHERE s.kind='zero' AND a.event_id=s.event_id AND a.effective=0 AND a.account_id=s.account_id"  # nosec B608
    )
    await conn.execute(
        "UPDATE public.wp5_f80_items i SET after_row=to_jsonb(a) FROM public.applied_events a WHERE i.digest=$1 AND a.event_id=i.event_id",
        plan_hash,
    )
    after = {
        r["event_id"]: r
        for r in await conn.fetch(
            "SELECT a.* FROM public.applied_events a JOIN pg_temp.wp5_f80_stage s USING(event_id)"
        )
    }
    for item in plan["items"]:
        expected = (
            item["before"] if item["kind"] == "bonus" else item["after_normalized"]
        )
        if ledger_row(after.get(int(item["event_id"]))) != expected:
            raise RuntimeError("Ledger trigger changed the approved award")


async def credit_balances(conn, totals, before_balances, plan_hash):
    for old in before_balances:
        earned, bonus = totals[str(old["account_id"])]
        new = await conn.fetchrow(
            "UPDATE public.point_balances SET points=points+$2,earned_total=earned_total+$3,last_updated=clock_timestamp() WHERE account_id=$1 RETURNING *",
            old["account_id"],
            bonus,
            earned,
        )
        if (
            new["points"] - old["points"] != bonus
            or new["earned_total"] - old["earned_total"] != earned
        ):
            raise RuntimeError("Balance delta disagrees with approved manifest")
        await conn.execute(
            "INSERT INTO public.wp5_f80_balances VALUES($1,$2,$3,$4,$5::jsonb,$6::jsonb)",
            plan_hash,
            old["account_id"],
            earned,
            bonus,
            canonical(dict(old)),
            canonical(dict(new)),
        )


async def settle(conn, plan, *, rollback=False, fail_after_balances=False):
    totals = validate(plan)
    plan_hash = digest(plan)
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(
            "SET LOCAL lock_timeout='10s'; SET LOCAL statement_timeout='45s'; SET LOCAL TimeZone='UTC'"
        )
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('WP-5-F80:settlement',0))"
        )
        if await prior_receipt(
            conn, plan_hash, [int(i["event_id"]) for i in plan["items"]]
        ):
            await tx.rollback()
            return "already_applied"
        # Match the normal writer's ledger-before-wallet order. Transactional DDL
        # below remains invisible to all other sessions; wallet reads stay allowed.
        await conn.execute(
            "LOCK TABLE public.applied_events IN SHARE ROW EXCLUSIVE MODE NOWAIT"
        )
        await conn.execute(
            "LOCK TABLE public.point_balances IN SHARE ROW EXCLUSIVE MODE NOWAIT"
        )
        sources = await preflight(conn, plan)
        original = await trigger_definition(conn)
        original_state = await trigger_state(conn)
        if original_state["prosecdef"]:
            raise RuntimeError("Expected invoker balance trigger")
        if digest(original) != plan["trigger_digest"]:
            raise RuntimeError("Balance protection function changed")
        await conn.execute(AUDIT_DDL)
        await conn.execute(
            "INSERT INTO public.wp5_f80_batches(digest,phase,earned,bonuses,items) VALUES($1,'WP-5-F80',$2,$3,$4)",
            plan_hash,
            money(plan["earned"]),
            money(plan["bonuses"]),
            len(plan["items"]),
        )
        await write_awards(conn, plan, sources, plan_hash)
        # Existing accounts are mandatory. New wallets require a separately
        # reviewed INSERT branch and are deliberately rejected here.
        before_balances = await conn.fetch(
            "SELECT * FROM public.point_balances WHERE account_id=ANY($1::uuid[]) ORDER BY account_id FOR UPDATE",
            [UUID(a) for a in totals],
        )
        if len(before_balances) != len(totals):
            raise RuntimeError("Recipient wallet missing")
        trusted_user = await conn.fetchval("SELECT current_user")
        cases = []
        for row in before_balances:
            earned, bonus = totals[str(row["account_id"])]
            cases.append(
                f"(OLD.account_id='{row['account_id']}'::uuid AND OLD.points={row['points']} AND OLD.earned_total={row['earned_total']} AND NEW.points=OLD.points+{bonus} AND NEW.earned_total=OLD.earned_total+{earned})"
            )
        if original.count("BEGIN\n") != 1:
            raise RuntimeError("Unexpected trigger body structure")
        guard = (
            "BEGIN\n IF TG_OP='UPDATE' AND current_user='"
            + trusted_user.replace("'", "''")
            + "' AND ("
            + " OR ".join(cases)
            + ") THEN RETURN NEW; END IF;\n"
        )
        await conn.execute(original.replace("BEGIN\n", guard, 1))
        await credit_balances(conn, totals, before_balances, plan_hash)
        if fail_after_balances:
            raise RuntimeError("Injected rollback check")
        await conn.execute(original)
        if (
            await trigger_definition(conn) != original
            or await trigger_state(conn) != original_state
        ):
            raise RuntimeError("Balance protection function was not restored")
        if rollback:
            await tx.rollback()
            return "rehearsed_rolled_back"
        await tx.commit()
        return "applied"
    except BaseException:
        await tx.rollback()
        raise


async def run(args):
    plan = json.loads(Path(args.manifest).read_text())
    validate(plan, args.approved_digest if args.apply else None)
    if args.apply and (
        not args.approved_digest
        or money(args.earned) != money(plan["earned"])
        or money(args.bonuses) != money(plan["bonuses"])
    ):
        raise ValueError("Explicit digest and both approved totals required")
    dsn = os.environ.get("REWARDS_URL")
    if not dsn:
        raise ValueError("REWARDS_URL is required")
    conn = await asyncpg.connect(
        dsn,
        statement_cache_size=0,
        timeout=15,
        command_timeout=45,
        server_settings={
            "default_transaction_read_only": "off" if args.apply else "on"
        },
    )
    try:
        if await conn.fetchval("SELECT current_database()") != "rewards":
            raise ValueError("Expected rewards database")
        if args.apply:
            result = await settle(conn, plan)
        else:
            async with conn.transaction(readonly=True, isolation="repeatable_read"):
                if await prior_receipt(
                    conn, digest(plan), [int(i["event_id"]) for i in plan["items"]]
                ):
                    result = "already_applied"
                else:
                    await preflight(conn, plan)
                    result = "preflight_read_only"
        print(
            json.dumps(
                {
                    "result": result,
                    "digest": digest(plan),
                    "earned": plan["earned"],
                    "bonuses": plan["bonuses"],
                }
            )
        )
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approved-digest")
    parser.add_argument("--earned")
    parser.add_argument("--bonuses")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception as exc:  # noqa: BLE001 -- sanitize DSNs and private row data
        print(
            f"Settlement stopped ({type(exc).__name__}); inspect batch receipt before retrying.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
