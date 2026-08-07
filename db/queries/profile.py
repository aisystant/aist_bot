from __future__ import annotations

"""
Агрегированный профиль знаний пользователя.

Использует VIEW user_knowledge_profile (db/models.py).
"""

from typing import Optional

from db.connection import get_pool, get_learning_pool, get_journal_pool, get_health_pool
from config import get_logger
from db.sql_helpers import delete_from as _delete_from_sql

logger = get_logger(__name__)

# Optional user-data tables in the main pool, deleted by delete_all_user_data()
# outside the core transaction so a missing/optional table cannot abort the
# identity deletion. column = the identifier that stores THIS user's own id
# (verified against db/models.py) — not a group/channel id some tables also
# happen to name chat_id (e.g. community_members, see WP query in db/queries/workshop.py).
OPTIONAL_CHAT_TABLES = [
    ('assessments', 'chat_id'),
    ('channel_mentions_log', 'mentioned_chat_id'),
    # legacy-имя (мн. число); переехала в channel_monitor, publication пул,
    # WP-253 — здесь физически не существует. См. test_channel_monitors_*
    # в tests/test_delete_all_user_data_tables.py.
    ('channel_monitors', 'chat_id'),
    ('community_members', 'telegram_id'),
    ('conversion_events', 'chat_id'),
    ('discourse_accounts', 'chat_id'),
    ('dt_tokens', 'chat_id'),
    ('github_connections', 'chat_id'),
    ('google_calendar_connections', 'chat_id'),
    ('oauth_pending_states', 'telegram_user_id'),
    ('ory_tokens', 'chat_id'),
    ('published_posts', 'chat_id'),
    ('scheduled_publications', 'chat_id'),
    ('tier_events', 'chat_id'),
    ('training_attempts', 'chat_id'),
    ('training_children', 'chat_id'),
    ('training_progress', 'chat_id'),
    ('training_settings', 'chat_id'),
    ('workshop_payments', 'telegram_id'),
]


async def get_knowledge_profile(chat_id: int) -> Optional[dict]:
    """Агрегированный профиль знаний пользователя (мультипул после WP-268 Phase 5 G5).

    - Профиль + состояние + feed stats: bot_data
    - Ответы (theory/wp counts): learning BD
    - QA count: journal BD
    """
    # 1. Base profile from main pool (users + user_state)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT
                s.chat_id,
                u.name, u.occupation, u.role, u.domain,
                u.interests, u.goals, u.motivation,
                u.language, u.experience_level,
                s.mode, s.marathon_status, s.feed_status,
                s.current_topic_index, s.complexity_level,
                s.assessment_state, s.assessment_date,
                s.active_days_total, s.active_days_streak, s.longest_streak,
                s.last_active_date,
                u.created_at, u.updated_at, u.dt_connected_at, u.dt_user_id
            FROM development.user_state s
            JOIN public.users u ON u.id = s.user_id
            WHERE s.chat_id = $1
        ''', chat_id)
    if not row:
        return None
    result = dict(row)

    # 1b. Feed stats from learning pool (feed_weeks/feed_sessions → WP-253)
    try:
        lpool = await get_learning_pool()
        async with lpool.acquire() as lc:
            result['total_digests'] = await lc.fetchval(
                '''SELECT COUNT(*) FROM feed_sessions fs
                   JOIN feed_weeks fw ON fs.week_id = fw.id
                   WHERE fw.chat_id = $1''', chat_id) or 0
            result['total_fixations'] = await lc.fetchval(
                '''SELECT COUNT(*) FROM feed_sessions fs
                   JOIN feed_weeks fw ON fs.week_id = fw.id
                   WHERE fw.chat_id = $1 AND fs.status = 'completed' ''', chat_id) or 0
            result['current_feed_topics'] = await lc.fetchval(
                '''SELECT accepted_topics FROM feed_weeks
                   WHERE chat_id = $1 AND status = 'active'
                   ORDER BY created_at DESC LIMIT 1''', chat_id)
    except Exception as e:
        logger.warning(f"[Profile] learning pool feed stats failed: {e}")
        result['total_digests'] = 0
        result['total_fixations'] = 0
        result['current_feed_topics'] = None

    # 2. Answer counts from learning BD
    try:
        lp = await get_learning_pool()
        async with lp.acquire() as lc:
            theory = await lc.fetchval(
                "SELECT COUNT(*) FROM answers WHERE chat_id=$1 AND answer_type='theory_answer'", chat_id)
            wp = await lc.fetchval(
                "SELECT COUNT(*) FROM answers WHERE chat_id=$1 AND answer_type='work_product'", chat_id)
        result['theory_answers_count'] = theory or 0
        result['work_products_count'] = wp or 0
    except Exception as e:
        logger.warning(f"[Profile] learning pool answers failed: {e}")
        result['theory_answers_count'] = 0
        result['work_products_count'] = 0

    # 3. QA count from journal BD
    try:
        jp = await get_journal_pool()
        async with jp.acquire() as jc:
            qa_count = await jc.fetchval(
                "SELECT COUNT(*) FROM qa_history WHERE chat_id=$1", chat_id)
        result['qa_count'] = qa_count or 0
    except Exception as e:
        logger.warning(f"[Profile] journal pool qa failed: {e}")
        result['qa_count'] = 0

    return result


async def delete_all_user_data(chat_id: int) -> dict:
    """Каскадное удаление ВСЕХ данных пользователя из всех таблиц.

    Порядок: зависимые таблицы → user_state → users, затем вторичные БД
    (подписки, интеграции, награды, публикации, Digital Twin).
    Возвращает dict с количеством удалённых строк по таблицам.

    Ref: DP.D.028 (User Data Tiers — протокол удаления), WP-476 (ЦД в удалении).
    """
    pool = await get_pool()
    result = {}

    async with pool.acquire() as conn:
        for table, column in OPTIONAL_CHAT_TABLES:
            try:
                deleted = await conn.execute(
                    _delete_from_sql(table, f'{column} = $1'), chat_id
                )
                result[table] = _parse_delete_count(deleted)
            except Exception as e:
                logger.warning(f"[DELETE] optional table {table} cleanup failed: {e}")
                result[table] = 0

        # Legacy bot_data.request_traces (main pool) — same treatment as
        # channel_monitors above and for the same reason (moved out of the core
        # transaction below). Distinct result key: the health-pool copy further
        # down writes result['request_traces']. See test_delete_all_user_data_tables.py.
        try:
            deleted = await conn.execute(
                'DELETE FROM request_traces WHERE user_id = $1', chat_id
            )
            result['request_traces_legacy'] = _parse_delete_count(deleted)
        except Exception as e:
            logger.warning(f"[DELETE] legacy request_traces cleanup failed: {e}")
            result['request_traces_legacy'] = 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            # WP-268 Phase 3 Block 2: qa_history вынесен в journal БД (см. ниже)
            # WP-268 Phase 5 G5: answers/activity_log/assessments вынесены в learning BD (см. ниже)
            # WP-253: feed_week/feed_session/marathon_content → learning pool (см. ниже)
            # WP-7 Ф48: daily_activity_marker — атомарный гейт счётчика активных дней.
            tables_chat_id = [
                'development.daily_activity_marker',
                'reminders', 'feedback_reports', 'subscriptions',
            ]
            for table in tables_chat_id:
                deleted = await conn.execute(
                    _delete_from_sql(table, 'chat_id = $1'), chat_id
                )
                result[table] = _parse_delete_count(deleted)

            # Таблицы с user_id вместо chat_id (request_traces legacy — см. выше,
            # уже удалена вне транзакции, не дублируем здесь)
            tables_user_id = ['service_usage']
            for table in tables_user_id:
                deleted = await conn.execute(
                    _delete_from_sql(table, 'user_id = $1'), chat_id
                )
                result[table] = _parse_delete_count(deleted)

            # development.user_events
            deleted = await conn.execute(
                'DELETE FROM development.user_events WHERE user_id = $1', chat_id
            )
            result['user_events'] = _parse_delete_count(deleted)

            # Bot state
            deleted = await conn.execute(
                'DELETE FROM development.user_state WHERE chat_id = $1', chat_id
            )
            result['user_state'] = _parse_delete_count(deleted)

            # Identity — последняя (FK от user_state)
            deleted = await conn.execute(
                'DELETE FROM public.users WHERE telegram_id = $1', chat_id
            )
            result['users'] = _parse_delete_count(deleted)

    # Ory account UUID, резолвится один раз — subscription.contract, indicators,
    # rewards, persona и secrets ниже все ключуются по нему, не по chat_id
    # (core/access.py, core/tier_detector.py, db/queries/rewards.py). Neon не
    # поддерживает cross-DB JOIN, поэтому каждый пул ниже получает его отдельным
    # параметром. Нет записи в ory_identity → все account_id-блоки пишут result[x]=0.
    account_id = None
    try:
        from db.connection import get_persona_pool
        persona_pool = await get_persona_pool()
        async with persona_pool.acquire() as pconn:
            account_id = await pconn.fetchval(
                'SELECT account_id FROM ory_identity WHERE telegram_id = $1', chat_id
            )
    except Exception as e:
        if 'does not exist' not in str(e):
            logger.warning(f"[DELETE] persona account_id resolution failed: {e}")

    # WP-476: Digital Twin data lives in the indicators DB; the canonical user_id
    # in digital_twins is dt_user_id from secrets.dt_tokens, falling back to the
    # Ory account id. Resolve the id before deleting the token row, then remove both.
    dt_user_id = None
    try:
        from db.connection import get_secrets_pool
        secrets_pool = await get_secrets_pool()
        async with secrets_pool.acquire() as sconn:
            dt_row = await sconn.fetchrow(
                'SELECT dt_user_id FROM dt_tokens WHERE chat_id = $1', chat_id
            )
            if dt_row:
                dt_user_id = dt_row['dt_user_id']
            deleted = await sconn.execute(
                'DELETE FROM dt_tokens WHERE chat_id = $1', chat_id
            )
            result['secrets_dt_tokens'] = _parse_delete_count(deleted)
    except Exception as e:
        logger.warning(f"[DELETE] secrets dt_tokens cleanup failed: {e}")
        result['secrets_dt_tokens'] = 0

    # persona.user_integrations (WakaTime, GitHub OAuth tokens — 12-BC архитектура)
    if account_id:
        try:
            persona_pool = await get_persona_pool()
            async with persona_pool.acquire() as pconn:
                deleted = await pconn.execute(
                    'DELETE FROM user_integrations WHERE account_id = $1', account_id
                )
                result['persona_user_integrations'] = _parse_delete_count(deleted)
        except Exception as e:
            logger.warning(f"[DELETE] persona user_integrations cleanup failed: {e}")
            result['persona_user_integrations'] = 0
    else:
        result['persona_user_integrations'] = 0

    # WP-253 Gap C: github_connections живут в secrets Neon БД (после миграции от bot_data)
    if account_id:
        try:
            from db.connection import get_secrets_pool
            secrets_pool = await get_secrets_pool()
            async with secrets_pool.acquire() as sconn:
                deleted = await sconn.execute(
                    'DELETE FROM github_connections WHERE user_uuid = $1', account_id
                )
                result['secrets_github_connections'] = _parse_delete_count(deleted)
        except Exception as e:
            if 'does not exist' in str(e):
                result['secrets_github_connections'] = 0
            else:
                logger.warning(f"[DELETE] secrets github_connections cleanup failed: {e}")
                result['secrets_github_connections'] = 0
    else:
        result['secrets_github_connections'] = 0

    # WP-253 lift-and-shift: subscription.contract (core/access.py) — ключ account_id.
    if account_id:
        try:
            from db.connection import get_subscription_pool
            sub_pool = await get_subscription_pool()
            async with sub_pool.acquire() as subconn:
                deleted = await subconn.execute(
                    'DELETE FROM contract WHERE account_id = $1', account_id
                )
                result['subscription_contract'] = _parse_delete_count(deleted)
        except Exception as e:
            logger.warning(f"[DELETE] subscription contract cleanup failed: {e}")
            result['subscription_contract'] = 0
    else:
        result['subscription_contract'] = 0

    # WP-476: remove Digital Twin data from indicators DB. The canonical key is
    # dt_user_id (from secrets.dt_tokens) when available, otherwise the Ory account id.
    twin_user_id = dt_user_id or account_id
    if twin_user_id:
        try:
            from db.connection import get_indicators_pool
            ind_pool = await get_indicators_pool()
            async with ind_pool.acquire() as indconn:
                deleted = await indconn.execute(
                    'DELETE FROM public.calculated_profile WHERE account_id = $1::uuid',
                    str(account_id)
                )
                result['indicators_calculated_profile'] = _parse_delete_count(deleted)
                deleted = await indconn.execute(
                    'DELETE FROM digital_twins WHERE user_id = $1::uuid',
                    str(twin_user_id)
                )
                result['indicators_digital_twins'] = _parse_delete_count(deleted)
        except Exception as e:
            logger.warning(f"[DELETE] indicators cleanup failed: {e}")
            result['indicators_calculated_profile'] = 0
            result['indicators_digital_twins'] = 0
    else:
        result['indicators_calculated_profile'] = 0
        result['indicators_digital_twins'] = 0

    # WP-253 Ф9.3: rewards.point_balances (db/queries/rewards.py) — ключ account_id.
    if account_id:
        try:
            from db.connection import get_rewards_pool
            rewards_pool = await get_rewards_pool()
            async with rewards_pool.acquire() as rewconn:
                deleted = await rewconn.execute(
                    'DELETE FROM point_balances WHERE account_id = $1', account_id
                )
                result['rewards_point_balances'] = _parse_delete_count(deleted)
        except Exception as e:
            logger.warning(f"[DELETE] rewards point_balances cleanup failed: {e}")
            result['rewards_point_balances'] = 0
    else:
        result['rewards_point_balances'] = 0

    # WP-253 lift-and-shift: discourse_accounts (основной пул, выше) → club_account
    # (community пул) — ключ chat_id напрямую, без account_id (db/queries/discourse.py).
    try:
        from db.connection import get_community_pool
        community_pool = await get_community_pool()
        async with community_pool.acquire() as commconn:
            deleted = await commconn.execute(
                'DELETE FROM club_account WHERE chat_id = $1', chat_id
            )
            result['community_club_account'] = _parse_delete_count(deleted)
    except Exception as e:
        logger.warning(f"[DELETE] community club_account cleanup failed: {e}")
        result['community_club_account'] = 0

    # WP-253 lift-and-shift (8 мая 2026): channel_monitor/channel_mention_log
    # переехали в publication Neon БД под singular-именами — см.
    # db/queries/channels.py. Legacy plural-таблицы в основном пуле (выше)
    # почти наверняка пусты после миграции, но чистим на всякий случай —
    # реальные данные живут здесь.
    try:
        from db.connection import get_publication_pool
        pub_pool = await get_publication_pool()
        async with pub_pool.acquire() as pubconn:
            deleted = await pubconn.execute(
                'DELETE FROM channel_monitor WHERE chat_id = $1', chat_id
            )
            result['publication_channel_monitor'] = _parse_delete_count(deleted)
            deleted = await pubconn.execute(
                'DELETE FROM channel_mention_log WHERE mentioned_chat_id = $1', chat_id
            )
            result['publication_channel_mention_log'] = _parse_delete_count(deleted)
            # Same WP-253 migration, same pool/connection: published_post/scheduled_post
            # (db/queries/discourse.py) — the user's actual authored blog content.
            deleted = await pubconn.execute(
                'DELETE FROM published_post WHERE chat_id = $1', chat_id
            )
            result['publication_published_post'] = _parse_delete_count(deleted)
            deleted = await pubconn.execute(
                'DELETE FROM scheduled_post WHERE chat_id = $1', chat_id
            )
            result['publication_scheduled_post'] = _parse_delete_count(deleted)
    except Exception as e:
        logger.warning(f"[DELETE] publication pool cleanup failed: {e}")
        result['publication_channel_monitor'] = 0
        result['publication_channel_mention_log'] = 0
        result['publication_published_post'] = 0
        result['publication_scheduled_post'] = 0

    # WP-253 lift-and-shift: lead.conversion_event (db/queries/conversion.py) — ключ chat_id.
    try:
        from db.connection import get_lead_pool
        lead_pool = await get_lead_pool()
        async with lead_pool.acquire() as leadconn:
            deleted = await leadconn.execute(
                'DELETE FROM conversion_event WHERE chat_id = $1', chat_id
            )
            result['lead_conversion_event'] = _parse_delete_count(deleted)
    except Exception as e:
        logger.warning(f"[DELETE] lead conversion_event cleanup failed: {e}")
        result['lead_conversion_event'] = 0

    # WP-253 lift-and-shift: training_setting/training_child живут в reference БД
    # (db/queries/training.py) — ключ chat_id. training_child хранит имя ребёнка.
    try:
        from db.connection import get_reference_pool
        reference_pool = await get_reference_pool()
        async with reference_pool.acquire() as refconn:
            deleted = await refconn.execute(
                'DELETE FROM training_setting WHERE chat_id = $1', chat_id
            )
            result['reference_training_setting'] = _parse_delete_count(deleted)
            deleted = await refconn.execute(
                'DELETE FROM training_child WHERE chat_id = $1', chat_id
            )
            result['reference_training_child'] = _parse_delete_count(deleted)
    except Exception as e:
        logger.warning(f"[DELETE] reference training cleanup failed: {e}")
        result['reference_training_setting'] = 0
        result['reference_training_child'] = 0

    # WP-268 Phase 3 Block 1: fsm_states живёт в отдельной БД (FSM_URL, Railway-local Postgres)
    try:
        from db.connection import get_fsm_pool
        fsm_pool = await get_fsm_pool()
        async with fsm_pool.acquire() as fconn:
            deleted = await fconn.execute(
                'DELETE FROM fsm_states WHERE chat_id = $1', chat_id
            )
            result['fsm_states'] = _parse_delete_count(deleted)
    except Exception as e:
        logger.warning(f"[DELETE] fsm_states cleanup failed: {e}")
        result['fsm_states'] = 0

    # WP-268 Phase 3 Block 2: qa_history + feedback_triage живут в journal БД
    try:
        from db.connection import get_journal_pool
        journal_pool = await get_journal_pool()
        async with journal_pool.acquire() as jconn:
            # feedback_triage сначала (FK на qa_history)
            deleted = await jconn.execute(
                'DELETE FROM feedback_triage WHERE chat_id = $1', chat_id
            )
            result['feedback_triage'] = _parse_delete_count(deleted)
            deleted = await jconn.execute(
                'DELETE FROM qa_history WHERE chat_id = $1', chat_id
            )
            result['qa_history'] = _parse_delete_count(deleted)
    except Exception as e:
        logger.warning(f"[DELETE] journal cleanup failed: {e}")
        result['qa_history'] = 0

    # WP-268 Phase 5 G5 + WP-253: learning pool — answers, feed, marathon
    try:
        learning_pool = await get_learning_pool()
        async with learning_pool.acquire() as lconn:
            # feed_sessions зависит от feed_weeks (FK week_id) — удалять первой
            deleted = await lconn.execute(
                '''DELETE FROM feed_sessions
                   WHERE week_id IN (SELECT id FROM feed_weeks WHERE chat_id = $1)''',
                chat_id
            )
            result['feed_sessions'] = _parse_delete_count(deleted)
            for table in (
                'feed_weeks', 'marathon_content', 'answers', 'activity_log', 'assessments',
                # WP-253 lift-and-shift: training_progress/training_attempt (db/queries/training.py)
                'training_progress', 'training_attempt',
            ):
                deleted = await lconn.execute(
                    _delete_from_sql('learning.' + table, 'chat_id = $1'), chat_id
                )
                result[table] = _parse_delete_count(deleted)
    except Exception as e:
        logger.warning(f"[DELETE] learning cleanup failed: {e}")

    # WP-268 Phase 5 G5 Tier2: user_sessions вынесены в health BD
    # WP-253 G4 (8 мая): + request_traces переехал в health (writer core/tracing.py)
    try:
        health_pool = await get_health_pool()
        async with health_pool.acquire() as hconn:
            deleted = await hconn.execute(
                'DELETE FROM user_sessions WHERE chat_id = $1', chat_id
            )
            result['user_sessions'] = _parse_delete_count(deleted)
            deleted = await hconn.execute(
                'DELETE FROM request_traces WHERE user_id = $1', chat_id
            )
            result['request_traces'] = _parse_delete_count(deleted)
    except Exception as e:
        logger.warning(f"[DELETE] health cleanup failed: {e}")
        result['user_sessions'] = 0
        result['request_traces'] = 0

    total = sum(result.values())
    logger.info(f"[DELETE] user {chat_id}: {total} rows deleted from {len(result)} tables")
    return result


async def reset_learning_data(chat_id: int) -> dict:
    """Сброс учебных данных с сохранением профиля.

    Сохраняет: name, occupation, interests, motivation, goals, language,
    schedule_time, subscriptions, github/DT подключения, onboarding_completed.

    Сбрасывает: марафон, лента, ответы, пре-генерированный контент,
    активность, оценки, FSM state.

    Returns: dict с количеством удалённых строк по таблицам.
    """
    pool = await get_pool()
    result = {}

    async with pool.acquire() as conn:
        async with conn.transaction():
            # WP-253: feed_week/feed_session/marathon_content → learning pool (см. ниже)
            # Сбрасываем поля прогресса в user_state (профиль в users сохраняется)
            await conn.execute('''
                UPDATE development.user_state SET
                    marathon_status = 'not_started',
                    marathon_start_date = NULL,
                    marathon_paused_at = NULL,
                    current_topic_index = 0,
                    completed_topics = '[]',
                    topics_today = 0,
                    last_topic_date = NULL,
                    complexity_level = 1,
                    topics_at_current_complexity = 0,
                    feed_status = 'not_started',
                    feed_started_at = NULL,
                    active_days_total = 0,
                    active_days_streak = 0,
                    longest_streak = 0,
                    last_active_date = NULL,
                    assessment_state = NULL,
                    assessment_date = NULL,
                    stats_reset_date = NULL,
                    current_state = NULL,
                    current_context = '{}',
                    updated_at = NOW()
                WHERE chat_id = $1
            ''', chat_id)
            result['user_state_reset'] = 1

    # WP-268 Phase 3 Block 1: fsm_states теперь в отдельной БД (FSM_URL, Railway-local)
    try:
        from db.connection import get_fsm_pool
        fsm_pool = await get_fsm_pool()
        async with fsm_pool.acquire() as fconn:
            deleted = await fconn.execute(
                'DELETE FROM fsm_states WHERE chat_id = $1', chat_id
            )
            result['fsm_states'] = _parse_delete_count(deleted)
    except Exception as e:
        logger.warning(f"[RESET] fsm_states cleanup failed: {e}")
        result['fsm_states'] = 0

    # WP-268 Phase 5 G5 + WP-253: learning pool — answers, feed, marathon
    try:
        learning_pool = await get_learning_pool()
        async with learning_pool.acquire() as lconn:
            # feed_sessions зависит от feed_weeks (FK week_id) — удалять первой
            deleted = await lconn.execute(
                '''DELETE FROM feed_sessions
                   WHERE week_id IN (SELECT id FROM feed_weeks WHERE chat_id = $1)''',
                chat_id
            )
            result['feed_sessions'] = _parse_delete_count(deleted)
            for table in ('feed_weeks', 'marathon_content', 'answers', 'activity_log', 'assessments'):
                deleted = await lconn.execute(
                    _delete_from_sql('learning.' + table, 'chat_id = $1'), chat_id
                )
                result[table] = _parse_delete_count(deleted)
    except Exception as e:
        logger.warning(f"[RESET] learning cleanup failed: {e}")

    total = sum(result.values())
    logger.info(f"[RESET] user {chat_id}: learning data reset, {total} rows affected across {len(result)} tables")
    return result


def _parse_delete_count(status_str: str) -> int:
    """Извлечь количество из строки 'DELETE N'."""
    try:
        return int(status_str.split()[-1])
    except (ValueError, IndexError):
        return 0
