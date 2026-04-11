# Таблицы базы данных

> Полный реестр таблиц и VIEW. Source-of-truth: `db/models.py`.
>
> ⚠️ **Миграция WP-82 Ф3 (2026-03-17, применена):** таблица `interns` полностью **удалена**. Профиль и state пользователя теперь живут в двух отдельных таблицах: `public.users` (identity + профиль) и `development.user_state` (bot state, прогресс, streaks). Миграция автоматическая, запускается при первом старте после обновления (`db/models.py:1213-1333`).

---

## Обзор

**38 таблиц + 3 VIEW** распределены по двум схемам:

| Схема | Назначение | Таблицы |
|-------|-----------|---------|
| **public** (явно) | Identity + bot state backbone | `public.users` |
| **development** (явно) | Bot state + engagement stream | `development.user_state`, `development.user_events`, VIEW `development.engagement`, VIEW `development.notification_engagement` |
| **default (public implied)** | Всё остальное | 35 таблиц: answers, reminders, feed_weeks, feed_sessions, marathon_content, notification_log, activity_log, qa_history, assessments, feedback_reports, feedback_triage, service_usage, subscriptions, fsm_states, request_traces, error_logs, pending_fixes, content_cache, user_sessions, conversion_events, ory_tokens, dt_tokens, tier_events, training_settings, training_progress, training_attempts, training_children, channel_monitors, channel_mentions_log, github_connections, google_calendar_connections, discourse_accounts, published_posts, scheduled_publications, oauth_pending_states + VIEW `user_knowledge_profile` |

**Важно:** `digital_twins` НЕ в bot DB. Таблица живёт в shared Neon, writer — Profiler (WP-218 Ф2), бот читает через Gateway MCP (`dt_read`). См. [P-07 § 12b](../processes/process-07-dt-engagement-sync.md).

---

## 1. Identity & Profile

### 1.1. `public.users` (идентичность + профиль)

> Единая таблица идентичности. Ключ связывает Telegram ID → Ory UUID → DT user ID.

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | UUID | `gen_random_uuid()` | **PK**, универсальный user ID |
| `ory_id` | UUID | — | UNIQUE, Ory auth identity (T1+) |
| `telegram_id` | BIGINT | — | UNIQUE NOT NULL, Telegram chat ID (T0+) |
| `dt_user_id` | TEXT | — | UNIQUE, Digital Twin identity (backfilled по WP-82) |
| `email` | TEXT | — | опционально |
| `name` | TEXT | `''` | Имя пользователя |
| `occupation` | TEXT | `''` | Род деятельности |
| `role` | TEXT | `''` | Роль в работе |
| `domain` | TEXT | `''` | Область деятельности |
| `interests` | TEXT | `'[]'` | Интересы (JSON массив) |
| `motivation` | TEXT | `''` | Мотивация к обучению |
| `goals` | TEXT | `''` | Цели обучения |
| `language` | TEXT | `'ru'` | Язык интерфейса (ru/en/es/fr) |
| `timezone` | TEXT | `'Europe/Moscow'` | Таймзона |
| `experience_level` | TEXT | `''` | Уровень опыта |
| `difficulty_preference` | TEXT | `''` | Предпочитаемая сложность |
| `learning_style` | TEXT | `''` | Стиль обучения |
| `study_duration` | INTEGER | `15` | Длительность занятия (мин) |
| `current_problems` | TEXT | `''` | Текущие проблемы |
| `desires` | TEXT | `''` | Пожелания |
| `tg_username` | TEXT | — | Telegram username |
| `aisystant_id` | TEXT | — | Привязка к Aisystant LMS |
| `aisystant_linked_at` | TIMESTAMP | — | Когда привязан Aisystant |
| `dt_connected_at` | TIMESTAMP | — | Когда подключён ЦД |
| `tier` | TEXT | `'T0'` | UITier (T0-T4, см. CLAUDE.md §12) |
| `created_at`, `updated_at` | TIMESTAMP | `NOW()` | UTC, naive |

**Constraints:** PK(id), UNIQUE(ory_id), UNIQUE(telegram_id), UNIQUE(dt_user_id)

**Правило identity (HD #29):** `telegram_id` — основной ключ для T0 (анонимных). При OAuth через Ory → появляется `ory_id`, становится стабильным ключом для T1+. `dt_user_id` = Ory UUID при T1+ (backfill в WP-82).

### 1.2. `development.user_state` (bot state, прогресс)

> Per-user настройки бота, режим, прогресс марафона/ленты, streaks. FSM writes.

**Профиль обучения (перенесены из interns):**

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `user_id` | UUID | — | **PK**, FK → `public.users.id` |
| `chat_id` | BIGINT | — | UNIQUE NOT NULL, денормализация для скорости |
| `mode` | TEXT | `'marathon'` | `marathon` / `feed` / `training` |
| `current_context` | TEXT | `'{}'` | FSM context (JSON) |
| `current_state` | TEXT | `NULL` | Текущий стейт State Machine |
| `topic_order` | TEXT | `'default'` | Порядок тем |
| `schedule_time` | TEXT | `'09:00'` | HH:MM (zero-padded, см. §13 CLAUDE.md) |
| `schedule_time_2` | TEXT | `NULL` | Второе время занятия |
| `feed_schedule_time` | TEXT | `NULL` | Время дайджестов Ленты |

**Марафон:**

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `marathon_status` | TEXT | `'not_started'` | `not_started` / `active` / `paused` / `completed` |
| `marathon_start_date` | DATE | `NULL` | Дата начала (связана с `marathon_status`, см. §13) |
| `marathon_paused_at` | DATE | `NULL` | Дата паузы |
| `current_topic_index` | INTEGER | `0` | Текущий индекс темы |
| `completed_topics` | TEXT | `'[]'` | Пройденные темы (JSON array) |
| `topics_today` | INTEGER | `0` | Тем пройдено сегодня |
| `last_topic_date` | DATE | `NULL` | Дата последней темы |
| `complexity_level` | INTEGER | `1` | Bloom 1-3 |
| `topics_at_current_complexity` | INTEGER | `0` | Тем на уровне |

**Лента:**

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `feed_status` | TEXT | `'not_started'` | `not_started` / `active` / `paused` / `completed` |
| `feed_started_at` | DATE | `NULL` | Дата начала |

**Активность (для streak):**

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `active_days_total` | INTEGER | `0` | Всего активных дней |
| `active_days_streak` | INTEGER | `0` | Текущая серия |
| `longest_streak` | INTEGER | `0` | Рекорд серии |
| `last_active_date` | DATE | `NULL` | Последний активный день |

**Статусы и блокировки:**

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `onboarding_completed` | BOOLEAN | `FALSE` | Онбординг завершён |
| `bot_blocked` | BOOLEAN | `FALSE` | Пользователь заблокировал бота |
| `bot_blocked_at` | TIMESTAMP | `NULL` | Когда заблокировано |
| `trial_started_at` | TIMESTAMP | `NULL` | Старт триала |
| `assessment_state` | TEXT | `NULL` | State assessment-flow |
| `assessment_date` | DATE | `NULL` | Дата прохождения |
| `stats_reset_date` | DATE | `NULL` | Дата сброса статистики |
| `notify_template_updates` | BOOLEAN | `FALSE` | Подписка на обновления |
| `notify_nudges` | BOOLEAN | `TRUE` | Подписка на nudges |
| `created_at`, `updated_at` | TIMESTAMP | `NOW()` | |

**Constraints:** PK(user_id), UNIQUE(chat_id), FK(user_id → public.users.id)

---

## 2. Контент и ответы

### 2.1. `answers` (ответы пользователей)

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | NOT NULL |
| `mode` | TEXT | `'marathon'` | marathon / feed |
| `topic_index` | INTEGER | — | Индекс темы |
| `topic_id` | TEXT | — | ID темы |
| `feed_session_id` | INTEGER | — | FK → feed_sessions |
| `answer_type` | TEXT | `'theory_answer'` | См. ниже |
| `answer` | TEXT | — | Текст ответа |
| `work_product_category` | TEXT | — | Категория РП |
| `complexity_level` | INTEGER | — | Bloom level |
| `feedback` | TEXT | — | LLM оценка (опционально) |
| `created_at` | TIMESTAMP | `NOW()` | |

**Типы ответов (`answer_type`):**

| Значение | Термин онтологии | Режим |
|----------|-----------------|-------|
| `theory_answer` | Ответ на урок | Марафон |
| `work_product` | Ответ на задание (РП) | Марафон |
| `bonus_answer` | Ответ на бонус | Марафон |
| `fixation` | Фиксация | Лента |

### 2.2. `marathon_content` (pre-generated контент)

> Кэш сгенерированных уроков/вопросов/практик. Writer: scheduler (pre-gen), reader: FSM (lesson/task states). Детали: [P-11 Pre-generation](../processes/process-11-pre-generation.md).

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | NOT NULL |
| `topic_index` | INTEGER | — | NOT NULL |
| `lesson_content` | TEXT | — | Сгенерированный урок |
| `question_content` | TEXT | — | Сгенерированный вопрос |
| `practice_content` | TEXT | — | Сгенерированная практика |
| `bloom_level` | INTEGER | — | На каком уровне генерировалось |
| `status` | TEXT | `'pending'` | `pending` (не открыт) / `delivered` (открыт) |
| `created_at` | TIMESTAMP | — | Когда сгенерирован |
| `delivered_at` | TIMESTAMP | — | Когда открыт |
| `notification_sent_at` | TIMESTAMP | — | Когда отправлено уведомление (idempotency guard) |

**Constraints:** UNIQUE(chat_id, topic_index)

**Семантика status:** см. `CLAUDE.md §13 «marathon_content — семантика полей»`. Pre-gen пишет только `'pending'`, `'delivered'` ставится в `lesson.py` при открытии.

### 2.3. `content_cache` (TTL-кэш генерации)

> Кэш persistent-контента (practice intro, questions) с 7-дневным TTL. Писатель: scheduler pre-gen + on-the-fly Haiku. Читатель: FSM.

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `cache_key` | TEXT | — | PK, см. форматы ниже |
| `content_type` | TEXT | — | NOT NULL: `lesson_content` / `question_content` / `practice_intro` |
| `content` | TEXT | — | NOT NULL |
| `created_at`, `expires_at` | TIMESTAMPTZ | — | TZ-aware |

**Формат `cache_key`:**
- `practice:{topic_id}:{lang}:{chat_id}` — per-user (персонализированный)
- `question:{topic_id}:{bloom}:{lang}:{occupation}` — по профессии

**Индексы:** `idx_content_cache_expires` — для cleanup

**Правило §10.15:** персонализированный контент ОБЯЗАН кэшироваться per-user (`:{chat_id}`). Глобальный кэш — только без персонализации.

### 2.4. `feed_weeks` (недели Ленты)

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | |
| `week_number` | INTEGER | — | Номер недели |
| `week_start` | DATE | — | Начало недели |
| `suggested_topics` | TEXT | `'[]'` | JSON |
| `accepted_topics` | TEXT | `'[]'` | JSON (выбор пользователя) |
| `current_day` | INTEGER | `0` | Текущий день (1-7) |
| `status` | TEXT | `'planning'` | `planning` / `active` / `completed` |
| `ended_at` | TIMESTAMP | — | |
| `created_at` | TIMESTAMP | `NOW()` | |

### 2.5. `feed_sessions` (дайджесты Ленты)

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `week_id` | INTEGER | — | FK → feed_weeks |
| `day_number` | INTEGER | — | 1-7 |
| `topic_title` | TEXT | — | |
| `content` | TEXT | `'{}'` | JSON дайджеста |
| `session_date` | DATE | — | |
| `status` | TEXT | `'active'` | `active` (ждёт фиксации) / `completed` |
| `fixation_text` | TEXT | — | Фиксация пользователя |
| `completed_at`, `created_at` | TIMESTAMP | `NOW()` | |

**Constraints:** UNIQUE(week_id, session_date) → `uq_feed_sessions_week_date`

### 2.6. `qa_history` (Q&A консультации)

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | |
| `mode` | TEXT | — | |
| `context_topic` | TEXT | — | |
| `question` | TEXT | — | |
| `answer` | TEXT | — | Ответ бота (консультация) |
| `mcp_sources` | TEXT | `'[]'` | JSON tool results |
| `helpful` | BOOLEAN | — | Оценка пользователя (thumbs up/down) |
| `user_comment` | TEXT | — | Комментарий к оценке |
| `created_at` | TIMESTAMP | `NOW()` | |

**Индексы:** `idx_qa_history_chat_id`

---

## 3. Планирование и идемпотентность

### 3.1. `notification_log` (единая idempotency-шина)

> WP-152: заменяет 6 разрозненных guard'ов. См. [P-09 Notification Idempotency](../processes/process-09-notification-idempotency.md).

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | NOT NULL |
| `notification_type` | TEXT | — | NOT NULL: `marathon_lesson` / `reminder` / `nudge` / `trial_expiry` / `milestone` / `feed_digest` / `event` |
| `idempotency_key` | TEXT | — | NOT NULL, формат `{type}:{chat_id}:{date}:{detail}` |
| `payload` | JSONB | `NULL` | Метаданные |
| `created_at` | TIMESTAMP | `NOW()` | Naive (§10.6) |

**Constraints:** UNIQUE(idempotency_key)

**Индексы:** `idx_notification_log_chat_type`, `idx_notification_log_created`

### 3.2. `reminders` (legacy очередь)

> Legacy (до WP-152). Сохраняется для backwards compat. Reminders используют `FOR UPDATE OF r SKIP LOCKED` (см. P-09 §6) + двойную защиту через `notification_log`.

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | |
| `reminder_type` | TEXT | — | |
| `scheduled_for` | TIMESTAMP | — | |
| `sent` | BOOLEAN | `FALSE` | |
| `fail_count` | INTEGER | `0` | |
| `created_at` | TIMESTAMP | `NOW()` | |

### 3.3. `activity_log` (лог активности для streak)

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | |
| `activity_date` | DATE | — | |
| `activity_type` | TEXT | — | `theory_answer` / `work_product` / `bonus_answer` / `feed_fixation` |
| `mode` | TEXT | — | |
| `reference_id` | INTEGER | — | ID связанной записи |
| `created_at` | TIMESTAMP | `NOW()` | |

**Constraints:** UNIQUE(chat_id, activity_date, activity_type) — одна запись типа в день

**Индексы:** `idx_activity_date ON (chat_id, activity_date)`

---

## 4. Аутентификация и интеграции

### 4.1. `ory_tokens` (WP-209: Gateway auth)

> Per-user Ory Bearer token для Gateway MCP. Подробности: [P-10 Gateway MCP § 3](../processes/process-10-gateway-mcp.md).

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `chat_id` | BIGINT | — | PK |
| `access_token` | TEXT | — | NOT NULL |
| `refresh_token` | TEXT | — | NOT NULL |
| `expires_at` | TIMESTAMP | — | NOT NULL |
| `ory_id` | TEXT | — | Ory identity ID |
| `updated_at` | TIMESTAMP | `NOW()` | |

### 4.2. `dt_tokens` (Digital Twin access)

> Отдельный токен для digital-twin-mcp (исторически до Gateway). В процессе консолидации с `ory_tokens` через Gateway routing.

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `chat_id` | BIGINT | — | PK |
| `access_token` | TEXT | — | NOT NULL |
| `refresh_token` | TEXT | — | NOT NULL |
| `expires_at` | TIMESTAMP | — | NOT NULL |
| `dt_user_id` | TEXT | — | |
| `updated_at` | TIMESTAMP | `NOW()` | |

### 4.3. `oauth_pending_states` (transient OAuth state)

> Хранит `state` для OAuth redirect между start и callback. **Database-backed** (не memory) — переживает redeploy Railway (правило §10.35 — не хранить в fsm_states).

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `state` | TEXT | — | PK, random UUID |
| `provider` | TEXT | — | NOT NULL: `github` / `ory` / `linear` |
| `telegram_user_id` | BIGINT | — | NOT NULL |
| `created_at` | TIMESTAMP | `NOW()` | |

**Индексы:** `idx_oauth_states_created` — для cleanup

### 4.4. `github_connections` (GitHub OAuth + knowledge repo)

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `chat_id` | BIGINT | — | PK |
| `access_token` | TEXT | — | NOT NULL |
| `token_type` | TEXT | `'bearer'` | |
| `scope` | TEXT | — | |
| `github_username` | TEXT | — | |
| `target_repo` | TEXT | — | Knowledge repo (где сохранять заметки) |
| `notes_path` | TEXT | `'inbox/fleeting-notes.md'` | Путь для заметок |
| `strategy_repo` | TEXT | — | Опционально: strategy document repo |
| `knowledge_repo` | TEXT | — | Явно привязанный knowledge repo |
| `default_branch` | TEXT | `'main'` | Определяется через GitHub API (§10.5) |
| `strategy_default_branch` | TEXT | `'main'` | Для strategy repo |
| `created_at`, `updated_at` | TIMESTAMP | `NOW()` | |

### 4.5. `google_calendar_connections` (Google OAuth)

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `chat_id` | BIGINT | — | PK |
| `access_token` | TEXT | — | NOT NULL |
| `refresh_token` | TEXT | — | NOT NULL |
| `expires_at` | TIMESTAMP | — | |
| `email` | TEXT | — | |
| `created_at`, `updated_at` | TIMESTAMP | `NOW()` | |

### 4.6. `discourse_accounts` (Discourse публикации)

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `chat_id` | BIGINT | — | PK |
| `discourse_username` | TEXT | — | NOT NULL |
| `blog_category_id` | INTEGER | — | |
| `blog_category_slug` | TEXT | — | |
| `connected_at` | TIMESTAMP | `NOW()` | |

---

## 5. Наблюдаемость и ошибки

### 5.1. `error_logs` (WP-45)

> Централизованный error monitoring с классификацией по 8 категориям. См. [P-06 Observability](../processes/process-06-observability.md).

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `error_key` | TEXT | — | NOT NULL, `md5(message)` для дедупа |
| `level`, `logger_name` | TEXT | — | NOT NULL |
| `message` | TEXT | — | NOT NULL |
| `traceback` | TEXT | — | |
| `context` | JSONB | `'{}'` | user_id, state, command etc. |
| `occurrence_count` | INTEGER | `1` | Инкремент при повторе |
| `first_seen_at`, `last_seen_at` | TIMESTAMPTZ | `NOW()` | TZ-aware (исключение из §10.6) |
| `category` | TEXT | — | 8 категорий RUNBOOK: `fsm` / `db` / `claude_api` / `telegram_api` / `mcp` / `scheduler` / `deployment` / `dt` |
| `severity` | TEXT | — | `L4_critical` / `L3_high` / `L2_medium` / `L1_info` |
| `suggested_action` | TEXT | — | LLM-подсказка |
| `alerted` | BOOLEAN | `FALSE` | Legacy поле |
| `escalated` | BOOLEAN | `FALSE` | Актуальное поле для алертов |

**Индексы:** `idx_error_logs_last_seen`, `idx_error_logs_alerted`, `idx_error_logs_category`

**⚠️ Техдолг:** два поля `alerted` (legacy) + `escalated` (актуальное). Индекс построен на `alerted`. Нужна миграция: поменять индекс на `escalated`, удалить `alerted`.

### 5.2. `request_traces` (latency monitoring)

> Трассировка запросов для Grafana (p50/p95/p99 latency). См. [P-06 § Performance](../processes/process-06-observability.md).

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `trace_id` | TEXT | — | NOT NULL |
| `user_id` | BIGINT | — | NOT NULL |
| `command` | TEXT | — | |
| `state` | TEXT | — | FSM state |
| `total_ms` | REAL | — | NOT NULL |
| `spans` | JSONB | `'[]'` | Nested timing data |
| `created_at` | TIMESTAMPTZ | `NOW()` | TZ-aware (исключение из §10.6) |

**Индексы:** `idx_traces_created`, `idx_traces_user`

### 5.3. `pending_fixes` (WP-45 Phase 2: Auto-Fix)

> L2 автоматический fix workflow. LLM предлагает diff, админ approves → PR в GitHub.

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `error_log_id` | INTEGER | — | NOT NULL |
| `error_key` | TEXT | — | NOT NULL |
| `status` | TEXT | `'pending'` | `pending` / `approved` / `merged` / `rejected` |
| `diagnosis` | TEXT | — | NOT NULL, LLM анализ причины |
| `archgate_eval` | TEXT | — | NOT NULL, архитектурная оценка |
| `proposed_diff` | TEXT | — | NOT NULL, unified diff |
| `file_path` | TEXT | — | NOT NULL |
| `pr_url` | TEXT | — | GitHub PR URL |
| `branch_name` | TEXT | — | |
| `tg_message_id` | BIGINT | — | Telegram message с кнопками approve/reject |
| `created_at`, `resolved_at` | TIMESTAMPTZ | — | |

**Constraints:** UNIQUE(error_key) WHERE status IN ('pending', 'approved')

**Индексы:** `idx_pf_error_key_active`

### 5.4. `feedback_reports` (пользовательские bug reports)

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | NOT NULL |
| `category` | TEXT | `'bug'` | `bug` / `feature` / `other` |
| `scenario` | TEXT | `'other'` | |
| `severity` | TEXT | `'yellow'` | `red` / `yellow` / `green` |
| `message` | TEXT | — | NOT NULL |
| `status` | TEXT | `'new'` | `new` / `acknowledged` / `resolved` |
| `notified_at` | TIMESTAMP | — | |
| `created_at` | TIMESTAMP | `NOW()` | |

**Индексы:** `idx_feedback_reports_severity_status`

### 5.5. `feedback_triage` (авто-триаж Q&A)

> LLM классифицирует Q&A пары из `qa_history` по тем же 8 категориям что и `error_logs`. Позволяет найти problematic сценарии.

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `qa_id` | INTEGER | — | NOT NULL, FK → qa_history |
| `chat_id` | BIGINT | — | NOT NULL |
| `question` | TEXT | — | NOT NULL |
| `answer_snippet` | TEXT | — | |
| `category` | TEXT | `'unknown'` | 8 категорий |
| `severity` | TEXT | `'low'` | `critical` / `high` / `medium` / `low` |
| `cluster` | TEXT | — | Группировка похожих |
| `reason` | TEXT | — | Почему классифицировано так |
| `has_comment` | BOOLEAN | `FALSE` | |
| `user_comment` | TEXT | — | |
| `status` | TEXT | `'new'` | |
| `created_at`, `notified_at` | TIMESTAMP | — | |

**Constraints:** UNIQUE(qa_id), FK(qa_id → qa_history.id)

**Индексы:** `idx_feedback_triage_severity_status`, `idx_feedback_triage_category`, `idx_feedback_triage_qa_id`

---

## 6. Аналитика и сессии

### 6.1. `user_sessions` (сессии для engagement)

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | NOT NULL |
| `started_at` | TIMESTAMPTZ | `NOW()` | |
| `ended_at` | TIMESTAMPTZ | — | |
| `duration_seconds` | INTEGER | — | |
| `request_count` | INTEGER | `1` | |
| `commands` | JSONB | `'[]'` | Команды в сессии |
| `entry_point` | TEXT | — | Первая команда |
| `exit_point` | TEXT | — | Последняя команда |

**Индексы:** `idx_sessions_chat_id`, `idx_sessions_started`

### 6.2. `service_usage` (использование сервисов)

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `user_id` | BIGINT | — | NOT NULL |
| `service_id` | TEXT | — | NOT NULL: `marathon` / `feed` / `training` |
| `action` | TEXT | `'enter'` | `enter` / `exit` |
| `created_at` | TIMESTAMP | `NOW()` | |

**Индексы:** `idx_service_usage_user`, `idx_service_usage_service`

### 6.3. `conversion_events` (funnel tracking)

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | NOT NULL |
| `trigger_type` | TEXT | — | NOT NULL: `tier_upgrade_shown` / `dt_onboard_shown` / `subscription_renewal` / etc. |
| `milestone` | TEXT | — | Опционально |
| `shown_at` | TIMESTAMPTZ | `NOW()` | |
| `action` | TEXT | `'shown'` | `shown` / `clicked` / `dismissed` |

**Индексы:** `idx_conversion_chat_id`, `idx_conversion_trigger`

### 6.4. `tier_events` (audit log тиров)

> WP-52: лог переходов между UITier для analytics.

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | NOT NULL |
| `from_tier`, `to_tier` | INTEGER | — | NOT NULL, 0-4 |
| `reason` | TEXT | — | NOT NULL: `subscription_active` / `trial_start` / `subscription_expired` / etc. |
| `created_at` | TIMESTAMP | `NOW()` | |

**Индексы:** `idx_tier_events_chat`

### 6.5. `subscriptions` (Telegram Stars)

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | NOT NULL |
| `telegram_payment_charge_id` | TEXT | — | NOT NULL |
| `status` | TEXT | `'active'` | `active` / `cancelled` / `expired` |
| `stars_amount` | INTEGER | — | NOT NULL |
| `started_at`, `expires_at` | TIMESTAMP | — | NOT NULL |
| `cancelled_at` | TIMESTAMP | `NULL` | |
| `is_first_recurring` | BOOLEAN | `FALSE` | |
| `created_at` | TIMESTAMP | `NOW()` | |

**Индексы:** `idx_subscriptions_chat_id`, `idx_subscriptions_active`

### 6.6. `assessments` (ответы assessment flow)

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | |
| `assessment_id` | TEXT | — | NOT NULL |
| `answers` | TEXT | `'{}'` | JSON |
| `scores` | TEXT | `'{}'` | JSON |
| `dominant_state` | TEXT | — | Inferred learning state |
| `self_check` | TEXT | — | |
| `open_response` | TEXT | — | |
| `created_at` | TIMESTAMP | `NOW()` | |

**Индексы:** `idx_assessments_chat_id`

---

## 7. State Machine и публикации

### 7.1. `fsm_states` (aiogram FSM persistence)

> PostgreSQL backend для aiogram FSM storage. Не хранить персистентный контекст SM-стейтов здесь (см. §10.35) — используй `development.user_state.current_context`.

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `chat_id` | BIGINT | — | PK |
| `state` | TEXT | — | Текущий FSM state |
| `data` | TEXT | `'{}'` | FSM local context (JSON) |
| `updated_at` | TIMESTAMP | `NOW()` | |

### 7.2. `published_posts` (публикации в Discourse)

> WP-53. Tracking постов, опубликованных ботом в Discourse.

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | NOT NULL |
| `discourse_topic_id` | INTEGER | — | NOT NULL |
| `discourse_post_id` | INTEGER | — | |
| `title` | TEXT | — | NOT NULL |
| `source_file` | TEXT | — | GitHub path |
| `category_id` | INTEGER | — | |
| `posts_count` | INTEGER | `1` | |
| `comment_check_failures` | INTEGER | `0` | |
| `last_checked_at`, `published_at` | TIMESTAMP | `NOW()` | |

**Constraints:** UNIQUE(discourse_topic_id)

**Индексы:** `idx_published_posts_topic`, `idx_published_posts_chat`

### 7.3. `scheduled_publications` (очередь публикаций)

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | NOT NULL |
| `title`, `raw` | TEXT | — | NOT NULL |
| `category_id` | INTEGER | — | NOT NULL |
| `tags` | TEXT | `'[]'` | JSON |
| `schedule_time` | TIMESTAMP | — | NOT NULL |
| `status` | TEXT | `'pending'` | `pending` / `published` / `cancelled` |
| `discourse_topic_id` | INTEGER | — | |
| `source_file` | TEXT | — | |
| `created_at` | TIMESTAMP | `NOW()` | |

**Индексы:** `idx_scheduled_pubs_pending` (partial, WHERE status='pending')

---

## 8. Тренировки (WP-55)

### 8.1. `training_settings`

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `chat_id` | BIGINT | — | PK |
| `cognitive_level` | TEXT | `'postformal'` | Уровень развития |
| `enabled_principles` | TEXT | `'["ZP.1",...,"ZP.6"]'` | JSON принципов |
| `training_mode` | TEXT | `'shuffle'` | `shuffle` / `sequential` |
| `single_principle` | TEXT | — | Опционально: фокус на одном |
| `created_at`, `updated_at` | TIMESTAMP | `NOW()` | |

### 8.2. `training_progress`

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | NOT NULL |
| `principle_id` | TEXT | — | NOT NULL (например, `ZP.1`) |
| `child_id` | INTEGER | `NULL` | FK → training_children (family mode) |
| `current_depth` | INTEGER | `0` | 0-3 |
| `attempts_at_depth` | INTEGER | `0` | |
| `last_completed_at` | TIMESTAMP | — | |
| `created_at` | TIMESTAMP | `NOW()` | |

**Constraints:** UNIQUE(chat_id, principle_id, child_id) when child_id IS NOT NULL, ELSE UNIQUE(chat_id, principle_id)

**Индексы:** `idx_training_progress_chat`, `idx_training_progress_child`

### 8.3. `training_attempts`

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | NOT NULL |
| `principle_id` | TEXT | — | NOT NULL |
| `child_id` | INTEGER | `NULL` | |
| `depth` | INTEGER | — | NOT NULL |
| `assignment_text` | TEXT | — | |
| `answer_text` | TEXT | — | |
| `passed` | BOOLEAN | `FALSE` | |
| `feedback` | TEXT | — | |
| `created_at` | TIMESTAMP | `NOW()` | |

**Индексы:** `idx_training_attempts_chat`

### 8.4. `training_children` (WP-55 Phase 2: family mode)

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | NOT NULL |
| `name` | TEXT | — | NOT NULL |
| `cognitive_level` | TEXT | `'concrete_operational'` | |
| `created_at` | TIMESTAMP | `NOW()` | |

**Индексы:** `idx_training_children_chat`

---

## 9. Engagement stream и каналы

### 9.1. `development.user_events` (event stream для ЦД)

> WP-85 Phase 4. Один ряд = одно действие пользователя. Читается агрегатором `development.engagement` VIEW и синхронизируется в `digital_twins` через [P-07 DT Engagement Sync](../processes/process-07-dt-engagement-sync.md).

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | BIGSERIAL | — | PK |
| `user_id` | BIGINT | — | NOT NULL, `telegram_id` (денормализация) |
| `user_uuid` | UUID | — | `public.users.id` (backfill, T1+) |
| `event_type` | TEXT | — | NOT NULL: `session_start` / `marathon_step` / `marathon_task` / `feed_completed` / `training_attempt` / `assessment_completed` / `onboarding_completed` / `mode_changed` / `error_shown` / etc. |
| `source` | TEXT | `'bot'` | `bot` / `iwe` / `lms` / `club` (WP-217 Ф8) |
| `payload` | JSONB | `'{}'` | Event-specific context |
| `confidence` | REAL | `1.0` | 0-1 confidence score |
| `skill_ids` | TEXT[] | `'{}'` | Skill identifiers (для training) |
| `created_at` | TIMESTAMPTZ | `NOW()` | |

**Индексы:** `idx_user_events_user_id`, `idx_user_events_type`

**Фильтр для DT sync:** `WHERE user_uuid IS NOT NULL` (T1+). T0 копит события по `user_id=telegram_id` — при появлении UUID sync подхватит автоматически.

### 9.2. `channel_monitors` (SC.118)

> Каналы, которые бот мониторит на упоминания пользователя.

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `channel_id` | BIGINT | — | NOT NULL |
| `channel_title` | TEXT | — | |
| `user_id` | UUID | — | NOT NULL, FK → public.users.id |
| `chat_id` | BIGINT | — | NOT NULL |
| `is_admin` | BOOLEAN | `FALSE` | Пользователь — админ канала |
| `track_username` | BOOLEAN | `TRUE` | Детект по @username |
| `track_reply` | BOOLEAN | `TRUE` | Детект по reply |
| `track_name` | BOOLEAN | `TRUE` | Детект по имени |
| `active` | BOOLEAN | `TRUE` | |
| `created_at`, `updated_at` | TIMESTAMP | `NOW()` | |

**Constraints:** UNIQUE(channel_id, chat_id), FK(user_id → public.users.id)

**Индексы:** `idx_channel_monitors_channel` (partial, WHERE active=TRUE)

### 9.3. `channel_mentions_log` (SC.118)

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `channel_id`, `message_id`, `mentioned_chat_id` | BIGINT | — | NOT NULL |
| `mention_type` | TEXT | — | NOT NULL: `username` / `reply` / `name` |
| `draft_sent` | BOOLEAN | `FALSE` | Был ли отправлен черновик админу |
| `created_at` | TIMESTAMP | `NOW()` | |

**Constraints:** UNIQUE(channel_id, message_id, mentioned_chat_id)

---

## 10. VIEW (агрегации)

### 10.1. `development.engagement`

> WP-85 Layer 2. Per-user агрегация 15+ engagement метрик из `development.user_events`. Читается синхронизатором DT (`sync_engagement_to_dt()`, cron 04:30 MSK).

**Источник:** `development.user_events` агрегируется по `(user_id, user_uuid)`.

**Колонки:** 20+ `COUNT FILTER` метрик:
- sessions, AI chats, marathon steps/tasks, training attempts
- assessments, onboarding, mode/settings changes
- reminders received, errors shown, help asked
- progress shown, completions
- MIN/MAX timestamps
- `active_days`, `events_last_7d`, `events_last_30d`

**Критично для:** P-07 DT sync. Без этой VIEW sync не работает.

**Правило DROP + CREATE (§10.22):** `CREATE OR REPLACE VIEW` запрещён в PostgreSQL — не позволяет менять порядок/имена колонок. Всегда `DROP VIEW IF EXISTS` + `CREATE VIEW`.

### 10.2. `development.notification_engagement` (WP-152 Ф4)

> Per-user агрегация метрик уведомлений. Мерджится в `2_5_notifications` секцию digital twin.

**Источник:** `notification_log JOIN public.users` (по `telegram_id`).

**Колонки:**
- `notifications_total`, `notifications_7d`, `notifications_30d`
- `notification_types` (array)
- `lesson_count`, `reminder_count`, `nudge_count`, `trial_count`, `feed_count`, `milestone_count`
- `first_notification_at`, `last_notification_at`

**Graceful fallback:** если VIEW не существует — `sync_engagement_to_dt()` логирует warning и продолжает без notifications (см. P-07 §12c).

### 10.3. `user_knowledge_profile` (default schema)

> Legacy-подобная агрегация: снапшот knowledge state пользователя (учёба + engagement).

**Источник:** `development.user_state` + `public.users` + подзапросы на `answers`, `qa_history`, `feed_sessions`.

**Колонки:** 29 — identity, learning state, профиль, агрегированные counts. Используется некоторыми queries как оптимизированный view.

---

## 11. Связи между таблицами

```
public.users (id UUID)
    │
    ├── development.user_state (user_id FK)
    │       └── chat_id (UNIQUE денорм.)
    │
    ├── development.user_events (user_uuid, user_id=telegram_id денорм.)
    │       └── development.engagement VIEW
    │
    ├── channel_monitors (user_id FK)
    │
chat_id (BIGINT) ← используется повсеместно как вторичный ключ
    │
    ├── answers, activity_log, qa_history
    │
    ├── marathon_content (UNIQUE chat_id, topic_index)
    │
    ├── feed_weeks
    │       └── feed_sessions (week_id FK, UNIQUE week_id+session_date)
    │
    ├── reminders, notification_log
    │
    ├── ory_tokens, dt_tokens, github_connections,
    │   google_calendar_connections, discourse_accounts
    │
    ├── assessments, feedback_reports, user_sessions
    │
    ├── service_usage, conversion_events, tier_events, subscriptions
    │
    ├── training_settings
    │       ├── training_children (FK)
    │       ├── training_progress (FK на principle_id + optional child_id)
    │       └── training_attempts
    │
    ├── published_posts, scheduled_publications
    │
    ├── fsm_states (aiogram persistence)
    │
    └── error_logs (opt. chat_id в context JSONB)

qa_history (id) ←── feedback_triage (qa_id FK)

error_logs (id) ←── pending_fixes (error_log_id)

oauth_pending_states — standalone (transient OAuth state)

channel_mentions_log — standalone (по channel_id + message_id)
```

---

## 12. Инварианты и правила

### 12.1. Timestamp semantics (§10.6)

- **Naive TIMESTAMP** (без таймзоны): `public.users`, `development.user_state`, большинство таблиц. Пишем `datetime.utcnow()`, НЕ `datetime.now(timezone.utc)`. asyncpg с `statement_cache_size=0` падает на aware datetime.
- **TIMESTAMPTZ** (aware): `error_logs.first_seen_at/last_seen_at`, `request_traces.created_at`, `content_cache.expires_at`, `user_sessions.started_at`, `development.user_events.created_at`, `conversion_events.shown_at`. Исключение из правила — там нужна TZ.

### 12.2. Идентичность

- T0: только `telegram_id` → `public.users.id` UUID, `ory_id = NULL`
- T1+: есть `ory_id` = Ory UUID, `dt_user_id` backfilled
- Правило: всегда писать через `ensure_user_exists(telegram_id)` — создаёт запись если нет, возвращает `user_id`

### 12.3. VIEW pattern

`CREATE OR REPLACE VIEW` запрещён (§10.22) — PostgreSQL не меняет порядок/имена колонок через REPLACE, бот падает в crash loop при старте. Всегда `DROP VIEW IF EXISTS` + `CREATE VIEW`. VIEW stateless — данные не теряются.

### 12.4. Миграции

`db/models.py:init_db()` идемпотентен — `CREATE TABLE IF NOT EXISTS`, проверки до `ALTER TABLE`. Одноразовые миграции (WP-82 Ф3) используют `has_interns = await conn.fetchval(...)` перед запуском.

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | **Переписан полностью:** 38 таблиц + 3 VIEW вместо 7 из 2026-01-23. Отражена миграция WP-82 Ф3 (`interns` → `users` + `user_state`), добавлены P-09 `notification_log`, P-10 `ory_tokens`/`dt_tokens`, WP-45 `error_logs`/`pending_fixes`/`feedback_triage`, WP-55 training_*, SC.118 channel_*, WP-52 tier_events, P-11 marathon_content, content_cache, request_traces, user_sessions, conversion_events, feedback_reports. Digital twins убраны — они НЕ в bot DB. |
| 2026-02-03 | Добавлено поле `current_state` для State Machine |
| 2026-01-23 | Создание документа. Полный реестр из `db/models.py` |
