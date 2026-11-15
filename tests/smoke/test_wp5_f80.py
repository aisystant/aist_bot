"""Exercise F80 SQL on disposable PostgreSQL, never on application databases.

Run with pytest. PG_BIN may locate PostgreSQL binaries; absent WP5_TEST_DSN,
the fixture creates a temporary Unix-socket-only server and stops it afterwards.
WP5_TEST_DSN optionally names a disposable server via a /tmp Unix socket, using
libpq key=value syntax. Each test creates and drops its own synthetic database.
The tested migration is loaded verbatim; only its FDW relations are local tables.
"""

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db/migrations/052_wp5_f80.sql"
ACCOUNT = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"
OCCURRED = "2026-01-15T12:00:00Z"
INGESTED = "2026-02-15T12:00:00Z"
CONTRACT_FROM = "2026-01-01T00:00:00Z"
CONTRACT_TO = "2026-02-01T00:00:00Z"


def literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, (dict, list)):
        value = json.dumps(value)
    return "'" + str(value).replace("'", "''") + "'"


def clean_pg_env():
    return {key: value for key, value in os.environ.items() if not key.startswith("PG")}


def run(command, **kwargs):
    return subprocess.run(
        [str(part) for part in command],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=clean_pg_env(),
        **kwargs,
    )


class Database:
    def __init__(self, psql, dsn):
        self.psql = psql
        self.dsn = dsn

    def execute(self, sql, *, succeeds=True):
        result = run(
            [self.psql, "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-d", self.dsn],
            input=sql,
        )
        if succeeds:
            assert result.returncode == 0, result.stderr
        else:
            assert result.returncode != 0, "The invalid accrual unexpectedly succeeded"
        return result.stdout.strip()

    def query(self, sql):
        return json.loads(
            self.execute(f"SELECT coalesce(json_agg(t), '[]') FROM ({sql}) t;")
        )

    def event(
        self,
        event_id=1,
        *,
        owner=ACCOUNT,
        event_type="practice",
        occurred=OCCURRED,
        external_id=None,
    ):
        self.execute(
            "INSERT INTO _foreign_learning.domain_event VALUES "
            f"({event_id},{literal(owner)},{literal(event_type)},"
            f"{literal(external_id)},{literal(occurred)});"
        )

    def profile(self, rcs=None, *, indicators=None, qualification=4):
        self.execute(
            "INSERT INTO _foreign_indicators.calculated_profile VALUES "
            f"({literal(ACCOUNT)},{literal(rcs)},{literal(indicators)},{qualification});"
        )

    def twin(self, qualification, *, estimate=False):
        key = "3_4_qualification_estimate" if estimate else "3_4_qualification"
        data = {"3_derived": {key: qualification}}
        self.execute(
            f"INSERT INTO _foreign_indicators.digital_twins VALUES ({literal(ACCOUNT)},{literal(data)});"
        )

    def contract(
        self, *, status="active", start=CONTRACT_FROM, end=CONTRACT_TO, owner=ACCOUNT
    ):
        contract_id = str(uuid.uuid4())
        self.execute(
            "INSERT INTO _foreign_subscription.contract VALUES "
            f"({literal(contract_id)},{literal(owner)},{literal(start)},{literal(end)},{literal(status)});"
        )
        return contract_id

    def lifecycle(
        self,
        contract_id,
        *,
        event_id=1,
        at=CONTRACT_FROM,
        status="active",
        snapshot=None,
    ):
        if snapshot is None:
            snapshot = {
                "valid_from": CONTRACT_FROM,
                "valid_to": CONTRACT_TO,
                "status": status,
            }
        self.execute(
            "INSERT INTO _foreign_subscription.contract_event VALUES "
            f"({event_id},{literal(contract_id)},'updated',{literal(at)},{literal(snapshot)});"
        )

    def apply(
        self,
        event_id=1,
        *,
        owner=ACCOUNT,
        event_type="practice",
        ingested=INGESTED,
        payload=None,
        succeeds=True,
        fail_upsert=False,
        session_timezone="UTC",
    ):
        # The worker deliberately executes compute and UPSERT as separate commands
        # in one transaction (db.apply_projection), to avoid trigger-cascade 21000.
        # Its narrow role can update points but cannot write earned_total directly.
        sql = rf"""
        BEGIN;
        SET LOCAL TIME ZONE {literal(session_timezone)};
        SET LOCAL ROLE projection_writer_rewards;
        SELECT public.compute_effective_amount_v4(
          {literal(owner)}::uuid, {event_id}, {literal(event_type)}, {literal(payload or {})}::jsonb,
          {literal(ingested)}::timestamptz) AS delta \gset
        INSERT INTO public.point_balances(account_id,points,last_updated,last_event_id)
        VALUES ({literal(owner)}, :delta, {literal(ingested)}, {event_id})
        ON CONFLICT(account_id) DO UPDATE SET
          points=point_balances.points+EXCLUDED.points,
          last_updated=EXCLUDED.last_updated,last_event_id=EXCLUDED.last_event_id;
        {"SELECT 1/0;" if fail_upsert else ""}
        COMMIT;
        """
        return self.execute(sql, succeeds=succeeds)

    def balances(self):
        return self.query(
            "SELECT points,earned_total FROM public.point_balances ORDER BY account_id"
        )

    def applied(self):
        return self.query("SELECT * FROM public.applied_events ORDER BY event_id")


SCHEMA = """
CREATE SCHEMA _foreign_learning;
CREATE SCHEMA _foreign_indicators;
CREATE SCHEMA _foreign_subscription;
CREATE SCHEMA _foreign_reference;
CREATE TABLE _foreign_learning.domain_event (
 id bigint PRIMARY KEY,account_id uuid,event_type text,external_id text,occurred_at timestamptz);
CREATE TABLE _foreign_learning.stage_transitions (
 account_id uuid,to_stage smallint,occurred_at timestamptz);
CREATE TABLE _foreign_indicators.calculated_profile (
 account_id uuid PRIMARY KEY,rcs_current jsonb,indicators jsonb,qualification_level int);
CREATE TABLE _foreign_indicators.digital_twins (user_id text PRIMARY KEY,data jsonb);
CREATE TABLE _foreign_subscription.contract (
 contract_id uuid PRIMARY KEY,account_id uuid,valid_from timestamptz,valid_to timestamptz,status text);
CREATE TABLE _foreign_subscription.contract_event (
 event_id bigint PRIMARY KEY,contract_id uuid,event_type text,occurred_at timestamptz,snapshot jsonb);
CREATE TABLE _foreign_reference.reward_rules (
 rule_id uuid PRIMARY KEY,trigger_event text,reward_kind text,amount numeric,effort_minutes int,
 is_marker boolean,max_per_day int,group_mult int,rarity_mult numeric,streak_eligible boolean,
 valid_from timestamptz,valid_to timestamptz,match_condition jsonb);
CREATE TABLE _foreign_reference.qualification_levels_v4 (
 level_number int PRIMARY KEY,qual_mult numeric,action_cap numeric);
CREATE TABLE _foreign_reference.loyalty_pool_config (
 use_v4_formula boolean,k int,valid_to timestamptz);
CREATE TABLE public.applied_events (
 event_id bigint PRIMARY KEY,account_id uuid,event_type text,base_amount numeric,
 dom_mult numeric,qual_mult numeric,streak_mult numeric,daily_cap numeric,
 raw_amount numeric,effective numeric,cap_truncated boolean,bonuses_eligible boolean,
 applied_at timestamptz,payload_snapshot jsonb,rule_id uuid,external_id text);
CREATE UNIQUE INDEX idx_applied_events_external_id_unique
 ON public.applied_events(external_id) WHERE external_id IS NOT NULL;
CREATE TABLE public.point_balances (
 account_id uuid PRIMARY KEY,points numeric NOT NULL DEFAULT 0,
 earned_total numeric NOT NULL DEFAULT 0,last_updated timestamptz,last_event_id bigint,
 CHECK(earned_total>=points));
INSERT INTO _foreign_reference.loyalty_pool_config VALUES (true,10,NULL);
INSERT INTO _foreign_reference.qualification_levels_v4
 SELECT n,n::numeric,1000 FROM generate_series(1,12) n;
INSERT INTO _foreign_reference.reward_rules
 SELECT gen_random_uuid(),event_type,'points',10,NULL,true,NULL,1,1,false,'2020-01-01',NULL,NULL
 FROM unnest(ARRAY['practice','referral_attributed','first_payment_detected']) event_type;

-- These supporting functions match the production definitions inspected for F80.
CREATE FUNCTION public._lookup_qualification_level(p_account_id uuid)
RETURNS integer LANGUAGE plpgsql STABLE AS $$
DECLARE v_level int;
BEGIN
 SELECT qualification_level INTO v_level FROM _foreign_indicators.calculated_profile
 WHERE account_id=p_account_id;
 RETURN COALESCE(v_level,4);
END $$;
CREATE FUNCTION public._compute_streak_mult(p_account_id uuid,p_ingested_at timestamptz DEFAULT now())
RETURNS numeric LANGUAGE plpgsql AS $$
DECLARE v_active_days int;
BEGIN
 WITH daily_totals AS (
  SELECT DATE(applied_at) AS day FROM applied_events WHERE account_id=p_account_id
  AND applied_at>=p_ingested_at-INTERVAL '28 days' AND applied_at<p_ingested_at
  GROUP BY DATE(applied_at) HAVING SUM(effective)>0)
 SELECT COUNT(*) INTO v_active_days FROM daily_totals;
 RETURN CASE WHEN v_active_days>=28 THEN 2.2 WHEN v_active_days>=14 THEN 1.7
             WHEN v_active_days>=7 THEN 1.3 ELSE 1.0 END;
END $$;
CREATE FUNCTION public.trg_point_balances_track_earned()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='INSERT' THEN
   NEW.earned_total:=GREATEST(NEW.earned_total,NEW.points,0::numeric);
 ELSIF TG_OP='UPDATE' THEN
   IF NEW.points>OLD.points THEN
     NEW.earned_total:=OLD.earned_total+(NEW.points-OLD.points);
   ELSIF current_user='rewards_points_engine_owner' AND NEW.points=OLD.points
         AND NEW.earned_total>OLD.earned_total THEN
     NULL;
   ELSE NEW.earned_total:=OLD.earned_total;
   END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_point_balances_earned_track
 BEFORE INSERT OR UPDATE OF points,earned_total ON public.point_balances
 FOR EACH ROW EXECUTE FUNCTION public.trg_point_balances_track_earned();
"""

GRANTS = """
ALTER FUNCTION public.compute_effective_amount_v4(uuid,bigint,text,jsonb,timestamptz)
 OWNER TO rewards_points_engine_owner;
GRANT USAGE ON SCHEMA public,_foreign_learning,_foreign_indicators,_foreign_subscription,
 _foreign_reference TO rewards_points_engine_owner;
GRANT SELECT ON ALL TABLES IN SCHEMA _foreign_learning,_foreign_indicators,
 _foreign_subscription,_foreign_reference TO rewards_points_engine_owner;
GRANT SELECT,INSERT,UPDATE ON public.applied_events,public.point_balances TO rewards_points_engine_owner;
GRANT USAGE ON SCHEMA public TO projection_writer_rewards;
GRANT SELECT ON public.point_balances TO projection_writer_rewards;
GRANT INSERT(account_id,points,last_updated,last_event_id),
 UPDATE(points,last_updated,last_event_id) ON public.point_balances TO projection_writer_rewards;
GRANT EXECUTE ON FUNCTION public.compute_effective_amount_v4(uuid,bigint,text,jsonb,timestamptz)
 TO projection_writer_rewards;
"""


def pg_binary_directory():
    configured = os.environ.get("PG_BIN")
    if configured:
        return Path(configured)
    homebrew = Path("/opt/homebrew/opt/postgresql@16/bin")
    if (homebrew / "postgres").exists():
        return homebrew
    if shutil.which("postgres"):
        return Path(shutil.which("postgres")).parent
    return Path(shutil.which("psql") or str(homebrew / "psql")).parent


def validate_local_dsn(dsn):
    fields = dict(part.split("=", 1) for part in shlex.split(dsn))
    allowed = {"host", "port", "dbname", "user", "connect_timeout"}
    if set(fields) - allowed:
        raise ValueError(
            "WP5_TEST_DSN accepts only local host, port, dbname, user, connect_timeout"
        )
    socket = Path(fields.get("host", "")).resolve()
    if not socket.is_absolute() or not socket.is_relative_to(Path("/private/tmp")):
        raise ValueError(
            "WP5_TEST_DSN must use a Unix socket under /private/tmp (or /tmp)"
        )
    return dsn


@pytest.fixture(scope="module")
def pg_server():
    binaries = pg_binary_directory()
    if not (binaries / "psql").exists():
        pytest.skip(
            "PostgreSQL client unavailable; set PG_BIN to PostgreSQL 16 binaries"
        )
    supplied_dsn = os.environ.get("WP5_TEST_DSN")
    if supplied_dsn:
        yield Database(binaries / "psql", validate_local_dsn(supplied_dsn))
        return
    if not (binaries / "initdb").exists():
        pytest.skip("PostgreSQL server unavailable; set PG_BIN or local WP5_TEST_DSN")
    with tempfile.TemporaryDirectory(prefix="wp5-f80-", dir="/private/tmp") as temp:
        directory = Path(temp)
        data = directory / "data"
        initialized = run(
            [
                binaries / "initdb",
                "-D",
                data,
                "-A",
                "trust",
                "-U",
                "wp5_test",
                "--no-locale",
            ]
        )
        assert initialized.returncode == 0, initialized.stderr
        started = run(
            [
                binaries / "pg_ctl",
                "-D",
                data,
                "-l",
                directory / "postgres.log",
                "-o",
                f"-k {directory} -c listen_addresses='' -c fsync=off",
                "-w",
                "start",
            ]
        )
        assert started.returncode == 0, (
            started.stderr + (directory / "postgres.log").read_text()
        )
        try:
            yield Database(
                binaries / "psql", f"host={directory} user=wp5_test dbname=postgres"
            )
        finally:
            stopped = run(
                [binaries / "pg_ctl", "-D", data, "-m", "immediate", "-w", "stop"]
            )
            assert stopped.returncode == 0, stopped.stderr


@pytest.fixture
def db(pg_server):
    pg_server.execute("""
    DO $$ BEGIN
      IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='rewards_points_engine_owner') THEN
        CREATE ROLE rewards_points_engine_owner NOLOGIN;
      END IF;
      IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='projection_writer_rewards') THEN
        CREATE ROLE projection_writer_rewards NOLOGIN;
      END IF;
    END $$;
    """)
    name = "wp5_f80_test_" + uuid.uuid4().hex
    pg_server.execute(f'CREATE DATABASE "{name}";')
    database = Database(pg_server.psql, pg_server.dsn + f" dbname={name}")
    try:
        database.execute(SCHEMA)
        migration_sql = MIGRATION.read_text()
        assert migration_sql.rstrip().endswith(";"), (
            "Migration must terminate its final SQL statement"
        )
        database.execute(migration_sql)
        database.execute(GRANTS)
        yield database
    finally:
        pg_server.execute(f'DROP DATABASE "{name}" WITH (FORCE);')


@pytest.mark.parametrize("subscribed", [False, True])
@pytest.mark.parametrize("event_type", ["practice", "referral_attributed"])
def test_worker_credits_earned_and_only_subscriber_bonuses(db, subscribed, event_type):
    db.profile({"stage": 3})
    if subscribed:
        db.contract()
    for event_id in (1, 2):
        db.event(event_id, event_type=event_type)
        db.apply(event_id, event_type=event_type)
    earned = 60 if event_type == "practice" else 20
    assert db.balances() == [
        {"points": earned if subscribed else 0, "earned_total": earned}
    ]
    assert len(db.applied()) == 2
    assert all(row["bonuses_eligible"] is subscribed for row in db.applied())


@pytest.mark.parametrize("status", ["active", "expired"])
@pytest.mark.parametrize(
    "occurred,eligible",
    [
        ("2025-12-31T23:59:59Z", False),
        (CONTRACT_FROM, True),
        ("2026-01-31T23:59:59Z", True),
        (CONTRACT_TO, False),
    ],
)
def test_subscription_uses_event_occurrence_half_open_interval(
    db, status, occurred, eligible
):
    db.contract(status=status)
    db.event(occurred=occurred)
    db.apply()
    assert db.balances() == [{"points": 10 if eligible else 0, "earned_total": 10}]
    row = db.applied()[0]
    assert row["bonuses_eligible"] is eligible
    assert row["payload_snapshot"]["subscription"]["eligible"] is eligible
    assert row["payload_snapshot"]["stage_source"] == "initial_no_evidence"


@pytest.mark.parametrize(
    "source", ["transition", "profile", "nested", "legacy", "twin", "estimate"]
)
def test_stage_source_precedence_and_provenance(db, source):
    db.twin({"stage": 2}, estimate=source == "estimate")
    if source == "transition":
        db.profile({"stage": 3})
        db.execute(
            f"INSERT INTO _foreign_learning.stage_transitions VALUES ({literal(ACCOUNT)},4,'2025-01-01');"
        )
        expected, provenance = 4, "confirmed_transition"
    elif source == "profile":
        db.profile({"stage": 99, "stage_id": "STG.Student.Systematic"})
        expected, provenance = 3, "profile_rcs"
    elif source == "nested":
        db.profile(indicators={"3_derived": {"3_4_qualification": {"stage": 3}}})
        expected, provenance = 3, "profile_qualification"
    elif source == "legacy":
        db.profile(indicators={"3_4_qualification": {"stage": 3}})
        expected, provenance = 3, "profile_legacy"
    elif source == "twin":
        expected, provenance = 2, "twin_confirmed"
    else:
        expected, provenance = 1, "initial_no_evidence"
    db.event()
    db.apply()
    row = db.applied()[0]
    assert row["effective"] == expected * 10
    assert row["payload_snapshot"]["stage"] == expected
    assert row["payload_snapshot"]["stage_source"] == provenance


def test_transition_max_excludes_future_promotions(db):
    db.execute(f"""INSERT INTO _foreign_learning.stage_transitions VALUES
      ({literal(ACCOUNT)},4,'2024-01-01'),({literal(ACCOUNT)},2,'2025-01-01'),
      ({literal(ACCOUNT)},5,'9999-01-01');""")
    db.event()
    db.apply()
    assert db.applied()[0]["effective"] == 40


@pytest.mark.parametrize(
    "invalid", [{"stage": 0}, {"stage": 9}, {"stage": "bad"}, {"stage_id": "unknown"}]
)
def test_malformed_stage_aborts_without_silent_stage_one(db, invalid):
    db.profile(invalid)
    db.event()
    db.apply(succeeds=False)
    assert db.applied() == []
    assert db.balances() == []


def test_other_qualification_uses_its_level_without_student_evidence(db):
    db.profile({"stage": "invalid"}, qualification=6)
    db.contract()
    db.event()
    db.apply()
    row = db.applied()[0]
    assert row["effective"] == 70
    assert row["payload_snapshot"]["stage_source"] == "qualification_level"


@pytest.mark.parametrize(
    "invalid", ["owner", "event_type", "time", "missing", "journal_unavailable"]
)
def test_invalid_source_rolls_back_without_claiming_event(db, invalid):
    if invalid != "missing":
        db.event(
            owner=OTHER if invalid == "owner" else ACCOUNT,
            event_type="other" if invalid == "event_type" else "practice",
            occurred=None if invalid == "time" else OCCURRED,
        )
    if invalid == "journal_unavailable":
        db.execute("DROP TABLE _foreign_learning.domain_event;")
    db.apply(succeeds=False)
    assert db.applied() == []
    assert db.balances() == []


@pytest.mark.parametrize("event_type", ["practice", "referral_attributed"])
@pytest.mark.parametrize("subscribed", [False, True])
def test_replay_and_semantic_duplicate_do_not_repeat_credits(
    db, event_type, subscribed
):
    if subscribed:
        db.contract()
    db.event(event_type=event_type, external_id="same-activity")
    db.apply(event_type=event_type)
    before = db.balances()
    db.apply(event_type=event_type)
    db.event(2, event_type=event_type, external_id="same-activity")
    db.apply(2, event_type=event_type)
    assert db.balances() == before
    assert len(db.applied()) == 1


@pytest.mark.parametrize("event_type", ["practice", "referral_attributed"])
def test_unrelated_unique_violation_aborts_transaction_and_can_retry(db, event_type):
    db.contract()
    db.execute(
        "CREATE UNIQUE INDEX unexpected_account_unique ON public.applied_events(account_id);"
    )
    db.event(event_type=event_type, external_id="first-activity")
    db.apply(event_type=event_type)
    before_balances, before_applied = db.balances(), db.applied()
    db.event(2, event_type=event_type, external_id="different-activity")
    db.apply(2, event_type=event_type, succeeds=False)
    assert db.balances() == before_balances
    assert db.applied() == before_applied
    db.execute("DROP INDEX public.unexpected_account_unique;")
    db.apply(2, event_type=event_type)
    assert db.balances() == [{"points": 20, "earned_total": 20}]
    assert len(db.applied()) == 2


def test_moscow_session_preserves_utc_daily_cap(db):
    db.contract()
    db.execute("""UPDATE _foreign_reference.qualification_levels_v4 SET action_cap=10;
      UPDATE _foreign_reference.loyalty_pool_config SET k=1;""")
    # These instants are on different Moscow calendar days, but the same UTC day.
    for event_id, ingested in (
        (1, "2026-02-15T20:30:00Z"),
        (2, "2026-02-15T22:30:00Z"),
    ):
        db.event(event_id)
        db.apply(event_id, ingested=ingested, session_timezone="Europe/Moscow")
    assert [row["effective"] for row in db.applied()] == [10, 0]
    assert db.balances() == [{"points": 10, "earned_total": 10}]


def test_delayed_event_uses_historical_subscription_and_current_stage(db):
    db.contract(status="expired")
    db.profile({"stage": 2})
    db.execute(f"""INSERT INTO _foreign_learning.stage_transitions VALUES
      ({literal(ACCOUNT)},2,'2026-01-01'),({literal(ACCOUNT)},4,'2026-02-10');""")
    db.event(occurred=OCCURRED)
    db.apply(ingested=INGESTED)
    assert db.balances() == [{"points": 40, "earned_total": 40}]
    snapshot = db.applied()[0]["payload_snapshot"]
    assert snapshot["stage"] == 4
    assert snapshot["stage_source"] == "confirmed_transition"
    assert snapshot["subscription"]["eligible"] is True
    assert snapshot["subscription"]["status"] == "expired"


@pytest.mark.parametrize(
    "relation",
    ["_foreign_learning.stage_transitions", "_foreign_subscription.contract_event"],
)
def test_unreadable_authoritative_source_aborts_instead_of_falling_back(db, relation):
    db.profile({"stage": 3})
    db.contract()
    db.event()
    db.execute(f"REVOKE SELECT ON {relation} FROM rewards_points_engine_owner;")
    db.apply(succeeds=False)
    assert db.applied() == []
    assert db.balances() == []


def test_worker_transaction_failure_leaves_retry_possible(db):
    db.contract()
    db.event()
    db.apply(succeeds=False, fail_upsert=True)
    assert db.applied() == []
    assert db.balances() == []
    db.apply()
    assert db.balances() == [{"points": 10, "earned_total": 10}]


@pytest.mark.parametrize("status", ["paused", "cancelled", "pending"])
def test_current_ambiguous_contract_requires_lifecycle(db, status):
    db.contract(status=status)
    db.event()
    db.apply(succeeds=False)
    assert db.applied() == []
    assert db.balances() == []


@pytest.mark.parametrize(
    "occurred,eligible", [(OCCURRED, True), ("2026-01-20T00:00:00Z", False)]
)
def test_cancelled_contract_uses_historical_lifecycle(db, occurred, eligible):
    contract_id = db.contract(status="cancelled")
    db.lifecycle(contract_id, status="active")
    db.lifecycle(contract_id, event_id=2, at="2026-01-20T00:00:00Z", status="cancelled")
    db.event(occurred=occurred)
    db.apply()
    assert db.balances() == [{"points": 10 if eligible else 0, "earned_total": 10}]


def test_proven_contract_wins_over_ambiguous_overlap(db):
    db.contract(status="cancelled")
    db.contract(status="expired")
    db.event()
    db.apply()
    assert db.balances() == [{"points": 10, "earned_total": 10}]


@pytest.mark.parametrize(
    "bad_snapshot",
    [
        {"status": "active"},
        {"status": "active", "valid_from": "invalid", "valid_to": CONTRACT_TO},
    ],
)
def test_incomplete_lifecycle_never_silently_denies_bonus(db, bad_snapshot):
    contract_id = db.contract()
    db.lifecycle(contract_id, snapshot=bad_snapshot)
    db.event()
    db.apply(succeeds=False)
    assert db.applied() == []


@pytest.mark.parametrize("cap_kind", ["daily", "event_count"])
def test_daily_and_action_caps_and_maximum_count_still_apply(db, cap_kind):
    db.contract()
    db.profile({"stage": 3})
    db.execute("UPDATE _foreign_reference.qualification_levels_v4 SET action_cap=25;")
    if cap_kind == "daily":
        db.execute("UPDATE _foreign_reference.loyalty_pool_config SET k=1;")
    else:
        db.execute("UPDATE _foreign_reference.reward_rules SET max_per_day=1;")
    for event_id in (1, 2):
        db.event(event_id)
        db.apply(event_id)
    assert [row["effective"] for row in db.applied()] == [25, 0]
    assert all(row["cap_truncated"] for row in db.applied())
    assert db.balances() == [{"points": 25, "earned_total": 25}]


def test_streak_multiplier_preserves_existing_formula(db):
    db.contract()
    db.execute(f"""INSERT INTO public.applied_events(event_id,account_id,effective,applied_at)
      SELECT -n,{literal(ACCOUNT)},1,{literal(INGESTED)}::timestamptz-make_interval(days=>n)
      FROM generate_series(1,7) n;
      UPDATE _foreign_reference.reward_rules SET streak_eligible=true;""")
    db.event()
    db.apply()
    row = next(row for row in db.applied() if row["event_id"] == 1)
    assert row["streak_mult"] == 1.3
    assert row["effective"] == 13
    assert db.balances() == [{"points": 13, "earned_total": 13}]


@pytest.mark.parametrize("subscribed", [False, True])
def test_live_payment_derived_welcome_and_referral_have_distinct_claims(db, subscribed):
    if subscribed:
        db.contract(owner=ACCOUNT)
        db.contract(owner=OTHER)
    db.event(17, event_type="payment_received", external_id="payment-17")
    db.apply(-34, event_type="first_payment_detected")
    db.apply(
        -35,
        owner=OTHER,
        event_type="referral_attributed",
        payload={"referred_account_id": ACCOUNT},
    )
    assert (
        db.balances() == [{"points": 10 if subscribed else 0, "earned_total": 10}] * 2
    )
    applied = db.applied()
    assert {row["event_id"] for row in applied} == {-34, -35}
    assert all(row["external_id"] is None for row in applied)
    db.apply(-34, event_type="first_payment_detected")
    db.apply(
        -35,
        owner=OTHER,
        event_type="referral_attributed",
        payload={"referred_account_id": ACCOUNT},
    )
    assert (
        db.balances() == [{"points": 10 if subscribed else 0, "earned_total": 10}] * 2
    )


@pytest.mark.parametrize(
    "event_id,event_type,owner,payload",
    [
        (-35, "first_payment_detected", ACCOUNT, {}),
        (-34, "referral_attributed", OTHER, {"referred_account_id": ACCOUNT}),
        (-34, "first_payment_detected", OTHER, {}),
        (-35, "referral_attributed", OTHER, {}),
        (-35, "referral_attributed", ACCOUNT, {"referred_account_id": ACCOUNT}),
        (-2000000000034, "first_payment_detected", ACCOUNT, {}),
    ],
)
def test_invalid_derived_payment_identity_never_claims_event(
    db, event_id, event_type, owner, payload
):
    db.event(17, event_type="payment_received")
    db.apply(
        event_id, event_type=event_type, owner=owner, payload=payload, succeeds=False
    )
    assert db.applied() == []
    assert db.balances() == []


def test_already_applied_legacy_negative_id_remains_idempotent(db):
    db.execute(f"""INSERT INTO public.applied_events(event_id,account_id,effective)
      VALUES (-2000000000034,{literal(ACCOUNT)},10);""")
    db.apply(-2000000000034, event_type="first_payment_detected")
    assert len(db.applied()) == 1
    assert db.balances() == [{"points": 0, "earned_total": 0}]


@pytest.mark.parametrize("source", ["rcs", "nested", "legacy", "twin"])
def test_explicit_profiler_estimate_is_not_confirmed_stage(db, source):
    estimate = {"stage": 4, "rcs_version": "v5", "stage_source": "profiler"}
    if source == "rcs":
        db.profile(estimate)
    elif source == "nested":
        db.profile(indicators={"3_derived": {"3_4_qualification": estimate}})
    elif source == "legacy":
        db.profile(indicators={"3_4_qualification": estimate})
    else:
        db.twin(estimate)
    db.event()
    db.apply()
    row = db.applied()[0]
    assert row["effective"] == 10
    assert row["payload_snapshot"]["stage_source"] == "initial_no_evidence"


def test_payment_hook_rolls_back_welcome_when_referral_fails(db):
    db.contract()
    db.event(17, event_type="payment_received")
    db.execute(
        rf"""
      BEGIN;
      SET LOCAL ROLE projection_writer_rewards;
      SELECT public.compute_effective_amount_v4({literal(ACCOUNT)},-34,
        'first_payment_detected','{{}}',{literal(INGESTED)}) AS welcome_delta \gset
      INSERT INTO point_balances(account_id,points,last_updated,last_event_id)
        SELECT {literal(ACCOUNT)},:welcome_delta,{literal(INGESTED)},17 WHERE :welcome_delta>0
        ON CONFLICT(account_id) DO UPDATE SET points=point_balances.points+EXCLUDED.points;
      SELECT public.compute_effective_amount_v4({literal(OTHER)},-35,
        'referral_attributed','{{}}',{literal(INGESTED)});
      COMMIT;
    """,
        succeeds=False,
    )
    assert db.applied() == []
    assert db.balances() == []


def test_dsn_refuses_network_or_credential_sources():
    for dsn in (
        "host=127.0.0.1 dbname=production",
        "host=/private/tmp service=live",
        "hostaddr=127.0.0.1",
    ):
        with pytest.raises(ValueError):
            validate_local_dsn(dsn)
