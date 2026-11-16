-- WP-5 F80. Execute through 052_wp5_f80.py, which checks the deployed body,
-- provisions only approved FDW relations and verifies the engine owner's reads.
CREATE OR REPLACE FUNCTION public._wp5_stage_value(p_value jsonb)
RETURNS integer LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $$
 SELECT CASE
   WHEN p_value ? 'stage_id' AND p_value->>'stage_id' <> '' THEN
     CASE p_value->>'stage_id'
       WHEN 'STG.Student.Random' THEN 1
       WHEN 'STG.Student.Practicing' THEN 2
       WHEN 'STG.Student.Systematic' THEN 3
       WHEN 'STG.Student.Disciplined' THEN 4
       WHEN 'STG.Student.Proactive' THEN 5 END
   WHEN p_value->>'stage' ~ '^[1-5]$' THEN (p_value->>'stage')::integer
 END;
$$;

CREATE OR REPLACE FUNCTION public._wp5_stage_evidence(p_account_id uuid)
RETURNS jsonb LANGUAGE plpgsql STABLE
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
 v_stage integer;
 v_profile record;
 v_twin jsonb;
 v_value jsonb;
 v_item record;
 v_invalid boolean := false;
BEGIN
 -- The stage evaluator stores promotions only. The confirmed reader uses MAX.
 SELECT max(to_stage) INTO v_stage
 FROM _foreign_learning.stage_transitions
 WHERE account_id = p_account_id AND occurred_at <= CURRENT_TIMESTAMP
   AND to_stage BETWEEN 1 AND 5;
 IF v_stage IS NOT NULL THEN
   RETURN jsonb_build_object('stage',v_stage,'source','confirmed_transition');
 END IF;
 SELECT rcs_current, indicators INTO v_profile
 FROM _foreign_indicators.calculated_profile WHERE account_id=p_account_id;
 SELECT data INTO v_twin FROM _foreign_indicators.digital_twins
 WHERE user_id=p_account_id::text;
 FOR v_item IN SELECT * FROM (VALUES
   ('profile_rcs',v_profile.rcs_current),
   ('profile_qualification',v_profile.indicators#>'{3_derived,3_4_qualification}'),
   ('profile_legacy',v_profile.indicators->'3_4_qualification'),
   ('twin_confirmed',v_twin#>'{3_derived,3_4_qualification}')
 ) AS sources(source,value)
 LOOP
   v_value := v_item.value;
   -- Older profiler versions also mirrored estimates into the confirmed key.
   IF v_value->>'stage_source' = 'profiler' OR v_value->>'rcs_version' = 'v5' THEN
     CONTINUE;
   END IF;
   v_stage := public._wp5_stage_value(v_value);
   IF v_stage IS NOT NULL THEN
     RETURN jsonb_build_object('stage',v_stage,'source',v_item.source);
   END IF;
   v_invalid := v_invalid OR COALESCE(
     (v_value ? 'stage' AND v_value->'stage' <> 'null'::jsonb)
     OR (v_value ? 'stage_id' AND v_value->'stage_id' <> 'null'::jsonb), false);
 END LOOP;
 IF v_invalid THEN
   RAISE EXCEPTION 'WP-5 F80: stage evidence is invalid' USING ERRCODE='22023';
 END IF;
 RETURN jsonb_build_object('stage',1,'source','initial_no_evidence');
END;
$$;

CREATE OR REPLACE FUNCTION public._lookup_student_stage(p_account_id uuid)
RETURNS integer LANGUAGE sql STABLE
SET search_path = pg_catalog, pg_temp
AS $$ SELECT (public._wp5_stage_evidence(p_account_id)->>'stage')::integer $$;

CREATE OR REPLACE FUNCTION public._wp5_subscription_evidence(
 p_account_id uuid, p_occurred_at timestamptz)
RETURNS jsonb LANGUAGE plpgsql STABLE
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $$
DECLARE
 c record;
 e record;
 v_from timestamptz;
 v_to timestamptz;
 v_status text;
 v_history boolean;
 v_unknown boolean := false;
 v_source text;
BEGIN
 IF p_occurred_at IS NULL THEN
   RAISE EXCEPTION 'WP-5 F80: occurrence time required' USING ERRCODE='22004';
 END IF;
 FOR c IN SELECT contract_id,valid_from,valid_to,status FROM _foreign_subscription.contract
          WHERE account_id=p_account_id ORDER BY valid_from,contract_id
 LOOP
   SELECT EXISTS(SELECT 1 FROM _foreign_subscription.contract_event
                 WHERE contract_id=c.contract_id) INTO v_history;
   IF v_history THEN
     SELECT snapshot, event_type INTO e FROM _foreign_subscription.contract_event
     WHERE contract_id=c.contract_id AND occurred_at<=p_occurred_at
     ORDER BY occurred_at DESC,event_id DESC LIMIT 1;
     IF NOT FOUND OR e.snapshot IS NULL OR NOT(e.snapshot ?& ARRAY['status','valid_from','valid_to']) THEN
       -- An imported lifecycle can start after the original entitlement.
       -- Missing history cannot prove either entitlement or absence.
       v_unknown := true;
       CONTINUE;
     END IF;
     BEGIN
       v_from := (e.snapshot->>'valid_from')::timestamptz;
       v_to := (e.snapshot->>'valid_to')::timestamptz;
     EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
       v_unknown := true;
       CONTINUE;
     END;
     v_status := e.snapshot->>'status';
     v_source := 'lifecycle_snapshot';
   ELSE
     v_from := c.valid_from; v_to := c.valid_to; v_status := c.status;
     v_source := 'contract_interval';
   END IF;
   IF v_from IS NULL OR v_to IS NULL OR v_to<=v_from THEN
     v_unknown := true;
     CONTINUE;
   END IF;
   IF p_occurred_at<v_from OR p_occurred_at>=v_to THEN CONTINUE; END IF;
   IF v_status IN ('active','expired') THEN
     RETURN jsonb_build_object('eligible',true,'contract_id',c.contract_id,
       'status',v_status,'source',v_source,'valid_from',v_from,'valid_to',v_to);
   ELSIF v_history AND v_status IN ('paused','cancelled','pending') THEN
     CONTINUE;
   ELSE
     -- Current paused/cancelled status cannot reconstruct a past entitlement.
     v_unknown := true;
   END IF;
 END LOOP;
 IF v_unknown THEN
   RAISE EXCEPTION 'WP-5 F80: subscription history is insufficient' USING ERRCODE='22023';
 END IF;
 RETURN jsonb_build_object('eligible',false,'source','no_covering_contract');
END;
$$;

CREATE OR REPLACE FUNCTION public.compute_effective_amount_v4(p_account_id uuid, p_event_id bigint, p_event_type text, p_payload jsonb, p_ingested_at timestamp with time zone)
 RETURNS numeric
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public', 'pg_temp'
 SET "TimeZone" TO 'UTC'
AS $function$
-- wp567_dual_write_external_id
-- wp5_f80_subscription_occurrence_v1
DECLARE
    -- rule columns
    v_rule_id           UUID;
    v_amount            NUMERIC;
    v_effort_minutes    INTEGER;
    v_is_marker         BOOLEAN;
    v_max_per_day       INTEGER;
    v_group_mult        INTEGER;
    v_rarity_mult       NUMERIC;
    v_streak_eligible   BOOLEAN;

    -- config
    v_K                 INTEGER := 10;
    v_use_v4            BOOLEAN := FALSE;

    -- qualification (12-level unified)
    v_level             INT;
    v_stage             INT;
    v_qual_mult         NUMERIC := 1.0;
    v_action_cap        NUMERIC := 200.0;

    -- streak
    v_streak_mult       NUMERIC := 1.0;

    -- effort / base
    v_effort_factor     NUMERIC;
    v_base              NUMERIC := 0;
    v_raw               NUMERIC;
    v_effective         NUMERIC;
    v_cap_truncated     BOOLEAN := FALSE;

    -- daily cap
    v_daily_total_cap   NUMERIC;
    v_today_total       NUMERIC;
    v_remaining_total   NUMERIC;

    -- Bonus eligibility is independent of qualification and stage.
    v_bonuses_eligible  BOOLEAN := TRUE;
    v_max_per_day_reached BOOLEAN := FALSE;

    -- WP-547 forward-fix: результат points-инкремента для внешнего UPSERT
    v_points_delta      NUMERIC;
    v_claimed_event     BIGINT;

    -- Read the semantic key together with mandatory source-event evidence.
    v_external_id       TEXT;
    v_constraint_name  TEXT;
    v_occurred_at timestamptz;
    v_source_event_id bigint;
    v_event_account uuid;
    v_event_type text;
    v_stage_evidence jsonb;
    v_subscription jsonb;
BEGIN
    IF p_account_id IS NULL OR p_event_id IS NULL THEN
        RAISE EXCEPTION 'WP-547: account_id and event_id are required'
            USING ERRCODE = '22004';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('wp547:event:' || p_event_id::TEXT, 0)
    );

    IF EXISTS (SELECT 1 FROM public.applied_events WHERE event_id = p_event_id) THEN
        RETURN 0;
    END IF;

    -- Eligibility requires the actual source event. Failure must leave it unapplied.
    v_source_event_id := p_event_id;
    IF p_event_id < 0 THEN
        -- Live worker uses -(gateway_id*2) for welcome and -(gateway_id*2+1)
        -- for referral. The historical ETL namespace has a different source.
        IF p_event_id <= -2000000000000 OR
           NOT ((p_event_type='first_payment_detected' AND mod(p_event_id,2)=0)
             OR (p_event_type='referral_attributed' AND mod(p_event_id,2)=-1)) THEN
            RAISE EXCEPTION 'WP-5 F80: unsupported derived event namespace' USING ERRCODE='22023';
        END IF;
        v_source_event_id := (-p_event_id)/2;
    END IF;
    SELECT external_id, occurred_at, account_id, event_type
    INTO v_external_id, v_occurred_at, v_event_account, v_event_type
    FROM _foreign_learning.domain_event WHERE id=v_source_event_id;
    IF NOT FOUND OR v_occurred_at IS NULL THEN
        RAISE EXCEPTION 'WP-5 F80: source event and occurrence time required' USING ERRCODE='22004';
    END IF;
    IF p_event_id < 0 THEN
        IF v_event_type IS DISTINCT FROM 'payment_received' OR
           (p_event_type='first_payment_detected' AND v_event_account IS DISTINCT FROM p_account_id) OR
           (p_event_type='referral_attributed' AND
             (v_event_account::text IS DISTINCT FROM p_payload->>'referred_account_id'
              OR v_event_account IS NOT DISTINCT FROM p_account_id)) THEN
            RAISE EXCEPTION 'WP-5 F80: derived payment identity mismatch' USING ERRCODE='22023';
        END IF;
        -- Both derived actions share the payment; their synthetic primary keys
        -- provide idempotency. Do not collide on the payment external_id.
        v_external_id := NULL;
    ELSIF v_event_account IS DISTINCT FROM p_account_id OR v_event_type IS DISTINCT FROM p_event_type THEN
        RAISE EXCEPTION 'WP-5 F80: source event identity mismatch' USING ERRCODE='22023';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('wp547:account:' || p_account_id::TEXT, 0)
    );

    -- Флаг формулы
    SELECT use_v4_formula, COALESCE(K, 10)
    INTO v_use_v4, v_K
    FROM _foreign_reference.loyalty_pool_config
    WHERE valid_to IS NULL
    LIMIT 1;

    IF NOT COALESCE(v_use_v4, FALSE) THEN
        RETURN 0;
    END IF;

    SELECT rule_id, amount, effort_minutes, is_marker, max_per_day, group_mult, rarity_mult, streak_eligible
    INTO v_rule_id, v_amount, v_effort_minutes, v_is_marker, v_max_per_day, v_group_mult, v_rarity_mult, v_streak_eligible
    FROM _foreign_reference.reward_rules
    WHERE trigger_event = p_event_type
      AND reward_kind = 'points'
      AND valid_from <= p_ingested_at
      AND (valid_to IS NULL OR valid_to > p_ingested_at)
      AND (match_condition IS NULL OR match_condition <@ p_payload)
    ORDER BY valid_from DESC
    LIMIT 1;

    IF v_rule_id IS NULL THEN
        RETURN 0;
    END IF;

    IF v_max_per_day IS NOT NULL AND v_max_per_day > 0 THEN
        DECLARE v_today_count INTEGER;
        BEGIN
            SELECT COUNT(*) INTO v_today_count
            FROM public.applied_events
            WHERE account_id = p_account_id
              AND event_type = p_event_type
              AND DATE(applied_at) = DATE(p_ingested_at);
            v_max_per_day_reached := v_today_count >= v_max_per_day;
        END;
    END IF;

    v_subscription := public._wp5_subscription_evidence(p_account_id, v_occurred_at);
    v_bonuses_eligible := (v_subscription->>'eligible')::boolean;

    IF p_event_type = 'referral_attributed' THEN
        v_effective := ROUND(COALESCE(v_amount, 0), 2);
        IF v_max_per_day_reached THEN
            v_effective := 0;
            v_cap_truncated := TRUE;
        END IF;

        -- WP-567: external_id carries its own unique index (partial, WHERE
        -- NOT NULL). ON CONFLICT (event_id) only dedups the technical key --
        -- a semantic-key collision here means two different event_ids claim
        -- the same real-world fact, which must not silently pass through as
        -- an unhandled unique_violation aborting the whole crediting call.
        -- Treat it exactly like the existing event_id dedup above: log and
        -- return 0 (no delta), the same "already applied, no-op" contract
        -- the rest of this function already uses.
        BEGIN
            INSERT INTO public.applied_events (
                event_id, account_id, event_type, base_amount,
                dom_mult, qual_mult, streak_mult, daily_cap,
                raw_amount, effective, cap_truncated, bonuses_eligible,
                applied_at, payload_snapshot, rule_id, external_id
            ) VALUES (
                p_event_id, p_account_id, p_event_type, v_effective,
                1.0, 1.0, 1.0, v_effective,
                v_effective, v_effective, v_cap_truncated, v_bonuses_eligible,
                p_ingested_at,
                jsonb_build_object(
                    'fixed_amount', TRUE,
                    'source_event_id', v_source_event_id,
                    'occurred_at', v_occurred_at,
                    'ingested_at', p_ingested_at,
                    'subscription', v_subscription,
                    'bonuses_eligible', v_bonuses_eligible,
                    'max_per_day_reached', v_max_per_day_reached,
                    'wp', 473
                ),
                v_rule_id, v_external_id
            )
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id INTO v_claimed_event;
        EXCEPTION WHEN unique_violation THEN
            GET STACKED DIAGNOSTICS v_constraint_name = CONSTRAINT_NAME;
            IF v_constraint_name IS DISTINCT FROM 'idx_applied_events_external_id_unique' THEN
                RAISE;
            END IF;
            RAISE WARNING 'WP-567: blocked probable duplicate grant, external_id=% already recorded under a different event_id (this event_id=%)', v_external_id, p_event_id;
            RETURN 0;
        END;

        IF v_claimed_event IS NULL THEN
            RETURN 0;
        END IF;

        IF v_bonuses_eligible THEN
            v_points_delta := v_effective;
        ELSE
            v_points_delta := 0;
            IF v_effective > 0 THEN
                INSERT INTO public.point_balances (
                    account_id, points, earned_total, last_updated, last_event_id
                ) VALUES (
                    p_account_id, 0, v_effective, p_ingested_at, p_event_id
                )
                ON CONFLICT (account_id) DO UPDATE
                SET earned_total = point_balances.earned_total + EXCLUDED.earned_total,
                    last_updated = EXCLUDED.last_updated,
                    last_event_id = EXCLUDED.last_event_id;
            END IF;
        END IF;

        RETURN v_points_delta;
    END IF;

    v_level := public._lookup_qualification_level(p_account_id);
    IF v_level IS NULL OR v_level = 4 OR v_level NOT BETWEEN 1 AND 11 THEN
        v_stage_evidence := public._wp5_stage_evidence(p_account_id);
        v_stage := (v_stage_evidence->>'stage')::integer;
    ELSE
        v_stage_evidence := jsonb_build_object('source','qualification_level');
    END IF;

    IF v_level BETWEEN 1 AND 3 THEN
        v_level := v_level;
    ELSIF v_level = 4 THEN
        v_level := COALESCE(v_stage, 1);
    ELSIF v_level BETWEEN 5 AND 11 THEN
        v_level := v_level + 1;
    ELSE
        v_level := COALESCE(v_stage, 1);
    END IF;

    v_level := GREATEST(1, LEAST(12, v_level));

    SELECT qual_mult, action_cap
    INTO v_qual_mult, v_action_cap
    FROM _foreign_reference.qualification_levels_v4
    WHERE level_number = v_level;

    IF COALESCE(v_streak_eligible, FALSE) THEN
        v_streak_mult := COALESCE(public._compute_streak_mult(p_account_id, p_ingested_at), 1.0);
    END IF;

    IF v_effort_minutes IS NOT NULL AND v_effort_minutes > 0 THEN
        v_effort_factor := POWER(v_effort_minutes::NUMERIC, 0.6);
        v_base := v_effort_factor * COALESCE(v_rarity_mult, 1.0) * COALESCE(v_group_mult, 1);
    ELSIF v_is_marker THEN
        v_base := v_amount * COALESCE(v_rarity_mult, 1.0) * COALESCE(v_group_mult, 1);
    ELSE
        v_base := COALESCE(v_amount, 0) * COALESCE(v_rarity_mult, 1.0) * COALESCE(v_group_mult, 1);
    END IF;

    v_raw := v_base * v_qual_mult * v_streak_mult;
    v_effective := LEAST(v_raw, v_action_cap);
    v_cap_truncated := (v_effective < v_raw);

    v_daily_total_cap := v_action_cap * v_K;

    SELECT COALESCE(SUM(effective), 0) INTO v_today_total
    FROM public.applied_events
    WHERE account_id = p_account_id
      AND DATE(applied_at) = DATE(p_ingested_at);

    v_remaining_total := GREATEST(0, v_daily_total_cap - v_today_total);

    IF v_effective > v_remaining_total THEN
        v_effective := v_remaining_total;
        v_cap_truncated := TRUE;
    END IF;

    v_effective := ROUND(v_effective, 2);
    IF v_max_per_day_reached THEN
        v_effective := 0;
        v_cap_truncated := TRUE;
    END IF;

    -- WP-567: same unique_violation contract as the referral branch above --
    -- a semantic-key collision here is a real, distinct-event_id duplicate
    -- grant attempt; log and no-op rather than let it abort uncaught.
    BEGIN
        INSERT INTO public.applied_events (
            event_id, account_id, event_type, base_amount,
            dom_mult, qual_mult, streak_mult, daily_cap,
            raw_amount, effective, cap_truncated, bonuses_eligible,
            applied_at, payload_snapshot, rule_id, external_id
        ) VALUES (
            p_event_id, p_account_id, p_event_type, ROUND(v_base, 2),
            1.0, v_qual_mult, v_streak_mult, v_action_cap,
            ROUND(v_raw, 2), v_effective, v_cap_truncated, v_bonuses_eligible,
            p_ingested_at,
            jsonb_build_object(
                'level', v_level,
                'stage', v_stage,
                'stage_source', v_stage_evidence->>'source',
                'source_event_id', v_source_event_id,
                'occurred_at', v_occurred_at,
                'ingested_at', p_ingested_at,
                'subscription', v_subscription,
                'bonuses_eligible', v_bonuses_eligible,
                'effort_minutes', v_effort_minutes,
                'group_mult', v_group_mult,
                'rarity_mult', v_rarity_mult,
                'max_per_day_reached', v_max_per_day_reached,
                'daily_total_cap', v_daily_total_cap
            ),
            v_rule_id, v_external_id
        )
        ON CONFLICT (event_id) DO NOTHING
        RETURNING event_id INTO v_claimed_event;
    EXCEPTION WHEN unique_violation THEN
        GET STACKED DIAGNOSTICS v_constraint_name = CONSTRAINT_NAME;
        IF v_constraint_name IS DISTINCT FROM 'idx_applied_events_external_id_unique' THEN
            RAISE;
        END IF;
        RAISE WARNING 'WP-567: blocked probable duplicate grant, external_id=% already recorded under a different event_id (this event_id=%)', v_external_id, p_event_id;
        RETURN 0;
    END;

    IF v_claimed_event IS NULL THEN
        RETURN 0;
    END IF;

    IF v_bonuses_eligible THEN
        v_points_delta := v_effective;
    ELSE
        v_points_delta := 0;
        IF v_effective > 0 THEN
            INSERT INTO public.point_balances (
                account_id, points, earned_total, last_updated, last_event_id
            ) VALUES (
                p_account_id, 0, v_effective, p_ingested_at, p_event_id
            )
            ON CONFLICT (account_id) DO UPDATE
            SET earned_total = point_balances.earned_total + EXCLUDED.earned_total,
                last_updated = EXCLUDED.last_updated,
                last_event_id = EXCLUDED.last_event_id;
        END IF;
    END IF;

    RETURN v_points_delta;
END;
$function$;
