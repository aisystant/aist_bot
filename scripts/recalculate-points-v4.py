#!/usr/bin/env python3
"""
WP-327 v4.1: Пересчёт истории applied_events + point_balances по новой формуле.

Стратегия:
  1. Читаем reward_rules, qualification_levels_v4 в память.
  2. Читаем applied_events по account_id (для локальности и хронологии).
  3. Для каждой записи вычисляем новый base, raw, effective.
     - amount=0 → effective=0 (guard как в SQL функции)
     - max_per_day применяем ретроспективно
     - daily_total_cap применяем ретроспективно (action_cap × K, K=10)
  4. Батч-UPDATE applied_events.
  5. Пересчитываем point_balances с нуля.
"""

import os
import sys
import logging
from decimal import Decimal
from collections import defaultdict
from datetime import date

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

REWARDS_DSN = os.environ.get(
    "REWARDS_URL",
    "postgresql://neondb_owner:npg_ZlRWtDg1zf3J@ep-dark-hall-ag8bo8lf.c-2.eu-central-1.aws.neon.tech/rewards?sslmode=require",
)
K = 1
BATCH_SIZE = 5000


def load_rules(cur):
    cur.execute(
        """
        SELECT trigger_event, amount, effort_minutes, is_marker,
               group_mult, rarity_mult, max_per_day, streak_eligible
        FROM _foreign_reference.reward_rules
        WHERE valid_to IS NULL AND reward_kind = 'points'
        """
    )
    rules = {}
    for r in cur.fetchall():
        rules[r["trigger_event"]] = dict(r)
    log.info("Loaded %d reward_rules", len(rules))
    return rules


def load_levels(cur):
    cur.execute(
        """
        SELECT level_number, qual_mult, action_cap
        FROM _foreign_reference.qualification_levels_v4
        """
    )
    levels = {}
    for r in cur.fetchall():
        levels[r["level_number"]] = dict(r)
    log.info("Loaded %d qualification_levels", len(levels))
    return levels


def load_profiles(cur):
    cur.execute(
        """
        SELECT account_id, qualification_level,
               (rcs_current ->> 'stage')::INT AS stage
        FROM _foreign_indicators.calculated_profile
        """
    )
    profiles = {}
    for r in cur.fetchall():
        profiles[str(r["account_id"])] = {
            "level": r["qualification_level"],
            "stage": r["stage"] or 1,
        }
    log.info("Loaded %d profiles", len(profiles))
    return profiles


def resolve_level(payload, profile):
    """Маппинг старой схемы на новую 12-уровневую."""
    level = None
    stage = None
    if payload:
        level = payload.get("level")
        stage = payload.get("stage")

    if level is None and profile:
        level = profile.get("level")
        stage = profile.get("stage")

    if level is None:
        return 5

    level = int(level)
    stage = int(stage) if stage else 1

    if 1 <= level <= 3:
        return level
    elif level == 4:
        return max(1, min(5, stage))
    elif 5 <= level <= 11:
        return level + 1
    else:
        return 5


def recalculate_account(cur, rules, levels, profile, account_id):
    """Пересчитать applied_events для одного account_id. Возвращает список updates."""
    cur.execute(
        """
        SELECT event_id, event_type, base_amount, dom_mult,
               qual_mult, streak_mult, daily_cap, raw_amount, effective,
               applied_at, payload_snapshot, rule_id
        FROM applied_events
        WHERE account_id = %s
        ORDER BY applied_at, event_id
        """,
        (account_id,),
    )
    rows = cur.fetchall()

    updates = []
    daily_type_count = defaultdict(lambda: defaultdict(int))
    daily_sum = defaultdict(Decimal)

    for r in rows:
        rule = rules.get(r["event_type"])
        if rule is None:
            continue

        amount = rule.get("amount") or Decimal("0")
        # Guard: amount=0 → effective=0 (как в compute_effective_amount_v4)
        if amount == 0:
            if r["effective"] != 0:
                updates.append((
                    Decimal("0"),   # base_amount
                    Decimal("1.0"), # dom_mult
                    Decimal("1.0"), # qual_mult
                    Decimal("1.0"), # streak_mult
                    Decimal("0"),   # daily_cap
                    Decimal("0"),   # raw_amount
                    Decimal("0"),   # effective
                    r["event_id"],
                ))
            continue

        level = resolve_level(r["payload_snapshot"], profile)
        lvl = levels.get(level, {"qual_mult": Decimal("1.0"), "action_cap": Decimal("200")})

        effort = rule.get("effort_minutes")
        is_marker = rule.get("is_marker")
        group_mult = rule.get("group_mult") or 1
        rarity_mult = rule.get("rarity_mult") or Decimal("1.0")

        if effort and effort > 0:
            effort_factor = Decimal(str(effort)) ** Decimal("0.6")
            base = effort_factor * rarity_mult * Decimal(str(group_mult))
        elif is_marker:
            base = Decimal("1.0") * rarity_mult * Decimal(str(group_mult))
        else:
            base = Decimal(str(amount)) * rarity_mult * Decimal(str(group_mult))

        qual_mult = lvl["qual_mult"]
        action_cap = lvl["action_cap"]
        streak_mult = r["streak_mult"] or Decimal("1.0")

        raw = base * qual_mult * streak_mult
        candidate = min(raw, action_cap)

        # Determine date
        applied_at = r["applied_at"]
        if hasattr(applied_at, "date"):
            evt_date = applied_at.date()
        else:
            evt_date = date.today()  # fallback

        # max_per_day
        max_pd = rule.get("max_per_day")
        if max_pd is not None and max_pd > 0:
            if daily_type_count[evt_date][r["event_type"]] >= max_pd:
                effective = Decimal("0")
            else:
                effective = candidate
                daily_type_count[evt_date][r["event_type"]] += 1
        else:
            effective = candidate

        # daily_total_cap
        daily_total_cap = action_cap * K
        if daily_sum[evt_date] + effective > daily_total_cap:
            effective = max(Decimal("0"), daily_total_cap - daily_sum[evt_date])
        daily_sum[evt_date] += effective

        effective = effective.quantize(Decimal("0.01"))

        # Only update if changed meaningfully
        old_eff = r["effective"] or Decimal("0")
        if abs(effective - old_eff) > Decimal("0.01") or r["base_amount"] != round(base, 2):
            updates.append((
                round(base, 2),
                Decimal("1.0"),
                qual_mult,
                streak_mult,
                action_cap,
                round(raw, 2),
                effective,
                r["event_id"],
            ))

    return updates


def apply_updates(cur, updates):
    if not updates:
        return 0
    execute_values(
        cur,
        """
        UPDATE applied_events AS ae
        SET base_amount = v.base_amount,
            dom_mult = v.dom_mult,
            qual_mult = v.qual_mult,
            streak_mult = v.streak_mult,
            daily_cap = v.daily_cap,
            raw_amount = v.raw_amount,
            effective = v.effective
        FROM (VALUES %s) AS v(base_amount, dom_mult, qual_mult, streak_mult,
                               daily_cap, raw_amount, effective, event_id)
        WHERE ae.event_id = v.event_id
        """,
        updates,
        template="(%s, %s, %s, %s, %s, %s, %s, %s)",
        page_size=5000,
    )
    return len(updates)


def recalculate_balances(cur):
    log.info("Recalculating point_balances...")
    cur.execute(
        """
        UPDATE point_balances pb
        SET points = COALESCE(earned.earned, 0) - COALESCE(burned.burned, 0)
        FROM (
            SELECT account_id, COALESCE(SUM(effective), 0) AS earned
            FROM applied_events
            GROUP BY account_id
        ) earned
        LEFT JOIN (
            SELECT account_id, COALESCE(SUM(points_amount), 0) AS burned
            FROM redeemed_events
            WHERE status = 'confirmed'
            GROUP BY account_id
        ) burned ON earned.account_id = burned.account_id
        WHERE pb.account_id = earned.account_id
        """
    )
    log.info("Updated %d point_balances rows", cur.rowcount)

    cur.execute(
        """
        INSERT INTO point_balances (account_id, points, last_event_id, last_updated)
        SELECT earned.account_id,
               COALESCE(earned.earned, 0) - COALESCE(burned.burned, 0),
               0, NOW()
        FROM (
            SELECT account_id, COALESCE(SUM(effective), 0) AS earned
            FROM applied_events
            GROUP BY account_id
        ) earned
        LEFT JOIN (
            SELECT account_id, COALESCE(SUM(points_amount), 0) AS burned
            FROM redeemed_events
            WHERE status = 'confirmed'
            GROUP BY account_id
        ) burned ON earned.account_id = burned.account_id
        LEFT JOIN point_balances pb ON pb.account_id = earned.account_id
        WHERE pb.account_id IS NULL
        """
    )
    log.info("Inserted %d new point_balances rows", cur.rowcount)


def main():
    conn = psycopg2.connect(REWARDS_DSN)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        log.info("Creating backup...")
        cur.execute("DROP TABLE IF EXISTS point_balances_backup_v4")
        cur.execute("CREATE TABLE point_balances_backup_v4 AS SELECT * FROM point_balances")
        cur.execute("DROP TABLE IF EXISTS applied_events_backup_v4")
        cur.execute("CREATE TABLE applied_events_backup_v4 AS SELECT * FROM applied_events")
        conn.commit()

        rules = load_rules(cur)
        levels = load_levels(cur)
        profiles = load_profiles(cur)

        log.info("Fetching account list...")
        cur.execute("SELECT DISTINCT account_id FROM applied_events ORDER BY account_id")
        all_accounts = [str(r["account_id"]) for r in cur.fetchall()]
        log.info("Total accounts: %d", len(all_accounts))

        total_updated = 0
        total_accounts = len(all_accounts)
        for idx, account_id in enumerate(all_accounts, 1):
            profile = profiles.get(account_id)
            updates = recalculate_account(cur, rules, levels, profile, account_id)
            if updates:
                apply_updates(cur, updates)
                total_updated += len(updates)
            conn.commit()
            if idx % 100 == 0 or idx == total_accounts:
                log.info("Progress: %d/%d accounts, %d events updated", idx, total_accounts, total_updated)

        log.info("All accounts processed. Total events updated: %d", total_updated)

        recalculate_balances(cur)
        conn.commit()

        log.info("Done.")

    except Exception as e:
        conn.rollback()
        log.error("FAILED: %s", e, exc_info=True)
        sys.exit(1)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
