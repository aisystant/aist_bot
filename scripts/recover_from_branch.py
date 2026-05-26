#!/usr/bin/env python3
"""
Recovery script: restore interns data from Neon PITR branch.

Usage:
    python scripts/recover_from_branch.py <RECOVERY_BRANCH_CONNECTION_STRING>

The script:
1. Connects to recovery branch → reads interns table
2. Connects to production (DATABASE_URL env) → inserts into public.users + development.user_state
3. Backfills user_events.user_uuid and dt_user_id
"""

import asyncio
import os
import sys
import json

import asyncpg


async def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/recover_from_branch.py <RECOVERY_DB_URL>")
        print("  RECOVERY_DB_URL = connection string of the Neon recovery branch")
        print("  Production DB is read from DATABASE_URL env var")
        sys.exit(1)

    recovery_url = sys.argv[1]
    prod_url = os.environ.get("DATABASE_URL")
    if not prod_url:
        print("ERROR: DATABASE_URL env var not set (production database)")
        sys.exit(1)

    print("=== Step 0: Connecting to databases ===")
    recovery_conn = await asyncpg.connect(recovery_url, statement_cache_size=0)
    prod_conn = await asyncpg.connect(prod_url, statement_cache_size=0)

    try:
        # Step 1: Read all interns from recovery branch
        print("\n=== Step 1: Reading interns from recovery branch ===")
        interns = await recovery_conn.fetch("""
            SELECT
                chat_id,
                name, occupation, role, domain,
                interests, motivation, goals, language, experience_level,
                difficulty_preference, learning_style, study_duration,
                current_problems, desires, tg_username,
                aisystant_id, aisystant_linked_at, dt_connected_at,
                mode, current_context, current_state, topic_order,
                schedule_time, schedule_time_2, feed_schedule_time,
                marathon_status, marathon_start_date, marathon_paused_at,
                current_topic_index, completed_topics, topics_today, last_topic_date,
                complexity_level, topics_at_current_complexity,
                feed_status, feed_started_at,
                active_days_total, active_days_streak, longest_streak, last_active_date,
                onboarding_completed, bot_blocked, bot_blocked_at, bot_recheck_at, trial_started_at,
                assessment_state, assessment_date, stats_reset_date,
                notify_template_updates, created_at
            FROM interns
            ORDER BY chat_id
        """)
        print(f"  Found {len(interns)} users in recovery branch")

        if not interns:
            print("  No users to recover!")
            return

        # Step 2: Insert/update public.users (profile data)
        print("\n=== Step 2: Restoring profile data to public.users ===")
        users_restored = 0
        users_updated = 0
        for row in interns:
            result = await prod_conn.execute("""
                INSERT INTO public.users (
                    telegram_id, name, occupation, role, domain,
                    interests, motivation, goals, language, experience_level,
                    difficulty_preference, learning_style, study_duration,
                    current_problems, desires, tg_username,
                    aisystant_id, aisystant_linked_at, dt_connected_at, created_at
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9, $10,
                    $11, $12, $13,
                    $14, $15, $16,
                    $17, $18, $19, $20
                ) ON CONFLICT (telegram_id) DO UPDATE SET
                    name = COALESCE(NULLIF(EXCLUDED.name, ''), public.users.name),
                    occupation = COALESCE(NULLIF(EXCLUDED.occupation, ''), public.users.occupation),
                    role = COALESCE(NULLIF(EXCLUDED.role, ''), public.users.role),
                    domain = COALESCE(NULLIF(EXCLUDED.domain, ''), public.users.domain),
                    interests = CASE WHEN EXCLUDED.interests != '[]' THEN EXCLUDED.interests ELSE public.users.interests END,
                    motivation = COALESCE(NULLIF(EXCLUDED.motivation, ''), public.users.motivation),
                    goals = COALESCE(NULLIF(EXCLUDED.goals, ''), public.users.goals),
                    language = COALESCE(NULLIF(EXCLUDED.language, ''), public.users.language),
                    experience_level = COALESCE(NULLIF(EXCLUDED.experience_level, ''), public.users.experience_level),
                    difficulty_preference = COALESCE(NULLIF(EXCLUDED.difficulty_preference, ''), public.users.difficulty_preference),
                    learning_style = COALESCE(NULLIF(EXCLUDED.learning_style, ''), public.users.learning_style),
                    study_duration = COALESCE(EXCLUDED.study_duration, public.users.study_duration),
                    current_problems = COALESCE(NULLIF(EXCLUDED.current_problems, ''), public.users.current_problems),
                    desires = COALESCE(NULLIF(EXCLUDED.desires, ''), public.users.desires),
                    tg_username = COALESCE(EXCLUDED.tg_username, public.users.tg_username),
                    aisystant_id = COALESCE(EXCLUDED.aisystant_id, public.users.aisystant_id),
                    aisystant_linked_at = COALESCE(EXCLUDED.aisystant_linked_at, public.users.aisystant_linked_at),
                    dt_connected_at = COALESCE(EXCLUDED.dt_connected_at, public.users.dt_connected_at)
            """,
                row['chat_id'],
                row['name'] or '', row['occupation'] or '', row['role'] or '', row['domain'] or '',
                row['interests'] or '[]', row['motivation'] or '', row['goals'] or '',
                row['language'] or 'ru', row['experience_level'] or '',
                row['difficulty_preference'] or '', row['learning_style'] or '',
                row['study_duration'] or 15,
                row['current_problems'] or '', row['desires'] or '', row['tg_username'],
                row['aisystant_id'], row['aisystant_linked_at'], row['dt_connected_at'],
                row['created_at'],
            )
            if 'INSERT' in result:
                users_restored += 1
            else:
                users_updated += 1

        print(f"  Inserted: {users_restored}, Updated: {users_updated}")

        # Step 3: Insert development.user_state (bot state)
        print("\n=== Step 3: Restoring bot state to development.user_state ===")
        state_restored = 0
        for row in interns:
            # Get the user_id (UUID) from public.users
            user_row = await prod_conn.fetchrow(
                "SELECT id FROM public.users WHERE telegram_id = $1", row['chat_id']
            )
            if not user_row:
                print(f"  WARNING: No user found for chat_id={row['chat_id']}, skipping state")
                continue

            user_id = user_row['id']

            # Check if state already exists
            existing = await prod_conn.fetchrow(
                "SELECT 1 FROM development.user_state WHERE chat_id = $1", row['chat_id']
            )
            if existing:
                print(f"  State already exists for chat_id={row['chat_id']}, skipping")
                continue

            await prod_conn.execute("""
                INSERT INTO development.user_state (
                    user_id, chat_id, mode, current_context, current_state, topic_order,
                    schedule_time, schedule_time_2, feed_schedule_time,
                    marathon_status, marathon_start_date, marathon_paused_at,
                    current_topic_index, completed_topics, topics_today, last_topic_date,
                    complexity_level, topics_at_current_complexity,
                    feed_status, feed_started_at,
                    active_days_total, active_days_streak, longest_streak, last_active_date,
                    onboarding_completed, bot_blocked, bot_blocked_at, bot_recheck_at, trial_started_at,
                    assessment_state, assessment_date, stats_reset_date,
                    notify_template_updates, created_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6,
                    $7, $8, $9,
                    $10, $11, $12,
                    $13, $14, $15, $16,
                    $17, $18,
                    $19, $20,
                    $21, $22, $23, $24,
                    $25, $26, $27, $28,
                    $29, $30, $31,
                    $32, $33
                )
            """,
                user_id, row['chat_id'],
                row['mode'] or 'marathon',
                row['current_context'] or '{}',
                row['current_state'],
                row['topic_order'] or 'default',
                row['schedule_time'] or '09:00',
                row['schedule_time_2'],
                row['feed_schedule_time'],
                row['marathon_status'] or 'not_started',
                row['marathon_start_date'],
                row['marathon_paused_at'],
                row['current_topic_index'] or 0,
                row['completed_topics'] or 0,
                row['topics_today'] or 0,
                row['last_topic_date'],
                row['complexity_level'] or 1,
                row['topics_at_current_complexity'] or 0,
                row['feed_status'] or 'not_started',
                row['feed_started_at'],
                row['active_days_total'] or 0,
                row['active_days_streak'] or 0,
                row['longest_streak'] or 0,
                row['last_active_date'],
                row['onboarding_completed'] if row['onboarding_completed'] is not None else False,
                row['bot_blocked'] if row['bot_blocked'] is not None else False,
                row['bot_blocked_at'],
                row['bot_recheck_at'],
                row['trial_started_at'],
                row['assessment_state'],
                row['assessment_date'],
                row['stats_reset_date'],
                row['notify_template_updates'] if row['notify_template_updates'] is not None else False,
                row['created_at'],
            )
            state_restored += 1

        print(f"  State restored: {state_restored}")

        # Step 4: Backfill user_events.user_uuid
        print("\n=== Step 4: Backfilling user_events.user_uuid ===")
        result = await prod_conn.execute("""
            UPDATE development.user_events e
            SET user_uuid = u.id
            FROM public.users u
            WHERE e.user_id = u.telegram_id
              AND e.user_uuid IS NULL
        """)
        print(f"  {result}")

        # Step 5: Backfill dt_user_id from dt_tokens
        print("\n=== Step 5: Backfilling dt_user_id from dt_tokens ===")
        result = await prod_conn.execute("""
            UPDATE public.users SET dt_user_id = dt_tokens.dt_user_id
            FROM dt_tokens
            WHERE public.users.telegram_id = dt_tokens.chat_id
              AND dt_tokens.dt_user_id IS NOT NULL
              AND public.users.dt_user_id IS NULL
        """)
        print(f"  {result}")

        # Step 6: Verify
        print("\n=== Step 6: Verification ===")
        total_users = await prod_conn.fetchval("SELECT count(*) FROM public.users")
        total_state = await prod_conn.fetchval("SELECT count(*) FROM development.user_state")
        events_linked = await prod_conn.fetchval(
            "SELECT count(*) FROM development.user_events WHERE user_uuid IS NOT NULL"
        )
        print(f"  Users: {total_users}")
        print(f"  User states: {total_state}")
        print(f"  Events linked: {events_linked}")

        print("\n=== RECOVERY COMPLETE ===")

    finally:
        await recovery_conn.close()
        await prod_conn.close()


if __name__ == "__main__":
    asyncio.run(main())
