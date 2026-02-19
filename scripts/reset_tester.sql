-- Полный сброс учебных данных тестера.
-- Сохраняет: профиль, язык, подписку, GitHub/DT подключения.
-- Сбрасывает: марафон, лента, ответы, контент, активность, оценки.
--
-- Использование: заменить :chat_id на реальный ID тестера.
-- psql $DATABASE_URL -v chat_id=123456789 -f scripts/reset_tester.sql

BEGIN;

-- 1. Feed sessions (зависят от feed_weeks через week_id)
DELETE FROM feed_sessions
WHERE week_id IN (SELECT id FROM feed_weeks WHERE chat_id = :chat_id);

-- 2. Учебные таблицы
DELETE FROM answers WHERE chat_id = :chat_id;
DELETE FROM feed_weeks WHERE chat_id = :chat_id;
DELETE FROM marathon_content WHERE chat_id = :chat_id;
DELETE FROM activity_log WHERE chat_id = :chat_id;
DELETE FROM assessments WHERE chat_id = :chat_id;
DELETE FROM fsm_states WHERE chat_id = :chat_id;

-- 3. Сброс прогресса в interns (профиль сохраняется)
UPDATE interns SET
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
WHERE chat_id = :chat_id;

COMMIT;

-- Проверка
SELECT chat_id, name, marathon_status, feed_status,
       current_topic_index, completed_topics, active_days_total
FROM interns WHERE chat_id = :chat_id;
