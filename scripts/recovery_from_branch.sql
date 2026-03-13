-- ============================================================
-- RECOVERY SCRIPT: Restore data from Neon PITR branch
-- ============================================================
--
-- USAGE:
-- 1. Create a Neon branch "recovery" at 2026-03-13 09:28:00 UTC
-- 2. Run this script against the PRODUCTION database
-- 3. Use dblink or Neon's cross-branch features to read from recovery branch
--
-- ALTERNATIVE (simpler):
-- Run SELECT queries on recovery branch → export CSV → import to prod
-- ============================================================

-- ============================================================
-- STEP 1: Export from RECOVERY branch (run on recovery branch)
-- ============================================================
-- Run this SELECT on the recovery branch to see what needs to be restored:

SELECT
    chat_id,
    name, occupation, role, domain,
    interests, motivation, goals, language, experience_level,
    difficulty_preference, learning_style, study_duration,
    current_problems, desires, tg_username,
    aisystant_id, aisystant_linked_at, dt_connected_at,
    -- State fields
    mode, current_context, current_state, topic_order,
    schedule_time, schedule_time_2, feed_schedule_time,
    marathon_status, marathon_start_date, marathon_paused_at,
    current_topic_index, completed_topics, topics_today, last_topic_date,
    complexity_level, topics_at_current_complexity,
    feed_status, feed_started_at,
    active_days_total, active_days_streak, longest_streak, last_active_date,
    onboarding_completed, bot_blocked, bot_blocked_at, trial_started_at,
    assessment_state, assessment_date, stats_reset_date,
    notify_template_updates, created_at
FROM interns
ORDER BY chat_id;

-- ============================================================
-- STEP 2: Insert into PRODUCTION (run on production)
-- ============================================================
-- After exporting data from recovery branch, use INSERT statements below.
-- Replace VALUES with actual data from Step 1.
--
-- For each user from interns:

-- 2a. Insert/Update public.users (profile)
-- INSERT INTO public.users (
--     telegram_id, name, occupation, role, domain,
--     interests, motivation, goals, language, experience_level,
--     difficulty_preference, learning_style, study_duration,
--     current_problems, desires, tg_username,
--     aisystant_id, aisystant_linked_at, dt_connected_at, created_at
-- ) VALUES (
--     <chat_id>, <name>, <occupation>, <role>, <domain>,
--     <interests>, <motivation>, <goals>, <language>, <experience_level>,
--     <difficulty_preference>, <learning_style>, <study_duration>,
--     <current_problems>, <desires>, <tg_username>,
--     <aisystant_id>, <aisystant_linked_at>, <dt_connected_at>, <created_at>
-- ) ON CONFLICT (telegram_id) DO UPDATE SET
--     name = EXCLUDED.name,
--     occupation = EXCLUDED.occupation,
--     role = EXCLUDED.role,
--     domain = EXCLUDED.domain,
--     interests = EXCLUDED.interests,
--     motivation = EXCLUDED.motivation,
--     goals = EXCLUDED.goals,
--     language = EXCLUDED.language,
--     experience_level = EXCLUDED.experience_level,
--     difficulty_preference = EXCLUDED.difficulty_preference,
--     learning_style = EXCLUDED.learning_style,
--     study_duration = EXCLUDED.study_duration,
--     current_problems = EXCLUDED.current_problems,
--     desires = EXCLUDED.desires,
--     tg_username = EXCLUDED.tg_username,
--     aisystant_id = EXCLUDED.aisystant_id,
--     aisystant_linked_at = EXCLUDED.aisystant_linked_at,
--     dt_connected_at = EXCLUDED.dt_connected_at;

-- 2b. Insert development.user_state (bot state)
-- INSERT INTO development.user_state (
--     user_id, chat_id, mode, current_context, current_state, topic_order,
--     schedule_time, schedule_time_2, feed_schedule_time,
--     marathon_status, marathon_start_date, marathon_paused_at,
--     current_topic_index, completed_topics, topics_today, last_topic_date,
--     complexity_level, topics_at_current_complexity,
--     feed_status, feed_started_at,
--     active_days_total, active_days_streak, longest_streak, last_active_date,
--     onboarding_completed, bot_blocked, bot_blocked_at, trial_started_at,
--     assessment_state, assessment_date, stats_reset_date,
--     notify_template_updates, created_at
-- ) SELECT
--     u.id, <chat_id>, <mode>, <current_context>, <current_state>, <topic_order>,
--     <schedule_time>, <schedule_time_2>, <feed_schedule_time>,
--     <marathon_status>, <marathon_start_date>, <marathon_paused_at>,
--     <current_topic_index>, <completed_topics>, <topics_today>, <last_topic_date>,
--     <complexity_level>, <topics_at_current_complexity>,
--     <feed_status>, <feed_started_at>,
--     <active_days_total>, <active_days_streak>, <longest_streak>, <last_active_date>,
--     <onboarding_completed>, <bot_blocked>, <bot_blocked_at>, <trial_started_at>,
--     <assessment_state>, <assessment_date>, <stats_reset_date>,
--     <notify_template_updates>, <created_at>
-- FROM public.users u WHERE u.telegram_id = <chat_id>
-- ON CONFLICT (chat_id) DO NOTHING;

-- ============================================================
-- STEP 3: Backfill user_events.user_uuid (run on production)
-- ============================================================
UPDATE development.user_events e
SET user_uuid = u.id
FROM public.users u
WHERE e.user_id = u.telegram_id
  AND e.user_uuid IS NULL;

-- ============================================================
-- STEP 4: Backfill dt_user_id from dt_tokens (run on production)
-- ============================================================
UPDATE public.users SET dt_user_id = dt_tokens.dt_user_id
FROM dt_tokens
WHERE public.users.telegram_id = dt_tokens.chat_id
  AND dt_tokens.dt_user_id IS NOT NULL
  AND public.users.dt_user_id IS NULL;

-- ============================================================
-- STEP 5: Verify
-- ============================================================
SELECT count(*) AS total_users FROM public.users;
SELECT count(*) AS total_state FROM development.user_state;
SELECT count(*) AS events_linked FROM development.user_events WHERE user_uuid IS NOT NULL;
