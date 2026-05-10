from __future__ import annotations

"""
Агрегированный профиль знаний пользователя.

Использует VIEW user_knowledge_profile (db/models.py).
"""

from typing import Optional

from db.connection import get_pool, get_learning_pool, get_journal_pool, get_health_pool
from config import get_logger

logger = get_logger(__name__)


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

    Порядок: зависимые таблицы → user_state → users.
    Возвращает dict с количеством удалённых строк по таблицам.

    Ref: DP.D.028 (User Data Tiers — протокол удаления).
    """
    pool = await get_pool()
    result = {}

    async with pool.acquire() as conn:
        async with conn.transaction():
            # WP-268 Phase 3 Block 2: qa_history вынесен в journal БД (см. ниже)
            # WP-268 Phase 5 G5: answers/activity_log/assessments вынесены в learning BD (см. ниже)
            # WP-253: feed_week/feed_session/marathon_content → learning pool (см. ниже)
            tables_chat_id = [
                'reminders', 'feedback_reports', 'subscriptions',
            ]
            for table in tables_chat_id:
                deleted = await conn.execute(
                    f'DELETE FROM {table} WHERE chat_id = $1', chat_id
                )
                result[table] = _parse_delete_count(deleted)

            # Таблицы с user_id вместо chat_id (только legacy — request_traces переехал в health)
            tables_user_id = ['service_usage']
            for table in tables_user_id:
                deleted = await conn.execute(
                    f'DELETE FROM {table} WHERE user_id = $1', chat_id
                )
                result[table] = _parse_delete_count(deleted)
            # Legacy bot_data.request_traces — historical, будет DROPPED после soak.
            # Удаляем для GDPR-полноты пока таблица существует.
            try:
                deleted = await conn.execute(
                    'DELETE FROM request_traces WHERE user_id = $1', chat_id
                )
                result['request_traces_legacy'] = _parse_delete_count(deleted)
            except Exception as e:
                logger.warning(f"[DELETE] legacy request_traces cleanup failed: {e}")
                result['request_traces_legacy'] = 0

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

    # persona.user_integrations (WakaTime, GitHub OAuth tokens — 12-BC архитектура)
    # Отдельная БД — вне основной транзакции
    try:
        from db.connection import get_persona_pool
        persona_pool = await get_persona_pool()
        async with persona_pool.acquire() as pconn:
            deleted = await pconn.execute(
                'DELETE FROM user_integrations WHERE account_id = (SELECT account_id FROM ory_identity WHERE telegram_id = $1)',
                chat_id
            )
            result['persona_user_integrations'] = _parse_delete_count(deleted)
    except Exception as e:
        if 'does not exist' in str(e):
            result['persona_user_integrations'] = 0
        else:
            logger.warning(f"[DELETE] persona user_integrations cleanup failed: {e}")
            result['persona_user_integrations'] = 0

    # WP-253 Gap C: github_connections живут в secrets Neon БД (после миграции от bot_data)
    # Neon не поддерживает cross-DB JOIN — сначала UUID из persona, потом DELETE в secrets
    try:
        from db.connection import get_secrets_pool, get_persona_pool
        persona_pool = await get_persona_pool()
        async with persona_pool.acquire() as pconn:
            row = await pconn.fetchrow(
                'SELECT id AS account_id FROM ory_identity WHERE telegram_id = $1', chat_id
            )
        if row:
            secrets_pool = await get_secrets_pool()
            async with secrets_pool.acquire() as sconn:
                deleted = await sconn.execute(
                    'DELETE FROM github_connections WHERE user_uuid = $1', row['account_id']
                )
                result['secrets_github_connections'] = _parse_delete_count(deleted)
        else:
            result['secrets_github_connections'] = 0
    except Exception as e:
        if 'does not exist' in str(e):
            result['secrets_github_connections'] = 0
        else:
            logger.warning(f"[DELETE] secrets github_connections cleanup failed: {e}")
            result['secrets_github_connections'] = 0

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
            # feed_session зависит от feed_week (FK week_id) — удалять первой
            deleted = await lconn.execute(
                '''DELETE FROM feed_session
                   WHERE week_id IN (SELECT id FROM feed_week WHERE chat_id = $1)''',
                chat_id
            )
            result['feed_session'] = _parse_delete_count(deleted)
            for table in ('feed_week', 'marathon_content', 'answers', 'activity_log', 'assessments'):
                deleted = await lconn.execute(
                    f'DELETE FROM {table} WHERE chat_id = $1', chat_id
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
            # feed_session зависит от feed_week (FK week_id) — удалять первой
            deleted = await lconn.execute(
                '''DELETE FROM feed_session
                   WHERE week_id IN (SELECT id FROM feed_week WHERE chat_id = $1)''',
                chat_id
            )
            result['feed_session'] = _parse_delete_count(deleted)
            for table in ('feed_week', 'marathon_content', 'answers', 'activity_log', 'assessments'):
                deleted = await lconn.execute(
                    f'DELETE FROM {table} WHERE chat_id = $1', chat_id
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
