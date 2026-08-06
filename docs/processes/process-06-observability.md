# P-06 Observability & Error Classification

> Классификация, эскалация и автофикс ошибок бота. Три уровня автоматической реакции (L1 recovery → L2 autofix → L3 health-restart) + TG-алерты + Grafana dashboard.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Тип | Процесс |
| Источник | WP-45 Ф2-Ф4 |
| Файлы | `core/error_classifier.py`, `core/autofix.py`, `core/health_check.py` |
| Dashboard | `monitoring/grafana-dashboard.json` (28 панелей, 3 секции) |
| Таблица | `error_logs` |
| Runbook | DP.RUNBOOK.001 (паттерны ошибок aist_bot) |

---

## 1. Архитектура: три уровня реакции

```
Error logged → error_logs (category=NULL)
    ↓
[5 мин] classify_unprocessed()         ← L1: классификация
    ↓
  regex match? → category + severity
    ↓ нет
  Haiku fallback (≤5/cycle, 8s timeout)
    ↓
[15 мин] _escalate_persistent_l1()     ← L1 → L2 auto-upgrade
          (3+ occurrences, age > 1h, action~"retry")
    ↓
[15 мин] check_escalation()            ← L3/L4/unknown ≥5 → TG alert
    ↓
[15 мин] run_autofix_cycle()           ← L2: Claude diagnosis + PR approval
    ↓
[15 мин] run_l3_health_check()         ← L3 cascade → Railway restart
```

---

## 2. Категории и severity

### 2.1. Восемь категорий (RUNBOOK § 3)

| Категория | Примеры паттернов | Порядок |
|-----------|-------------------|---------|
| **fsm** | `no handler for state`, `stuck.*state`, `Unstick.*Recover` | 1 |
| **claude_api** | `rate_limit`, `OverloadedError`, `APITimeoutError` | 2 |
| **telegram_api** | `ConflictError`, `RetryAfter`, `bot was blocked` | 3 |
| **dt** | `DT.*token.*refresh.*fail`, `DT.*persist.*fail`, `DigitalTwin.*circuit breaker` | 4 |
| **mcp** | `MCP.*connection.*fail`, `MCP.*timeout` | 5 |
| **scheduler** | `[Scheduler].*error`, `offset-naive and offset-aware` | 6 |
| **deployment** | `secret token.*unallowed`, `terminated by.*setWebhook` | 7 |
| **db** | `too many connections`, `statement.*timeout`, `relation.*does not exist` | 8 (last — generic) |

**Важно:** порядок имеет значение. Specific patterns (MCP, Claude, aiogram) идут **до** generic DB patterns — иначе `claude timeout` попадёт в `db` по слову `timeout`.

### 2.2. Severity L1–L4

| Severity | Смысл | Реакция |
|----------|-------|---------|
| **L1** | Transient, auto-recoverable | Retry, skip, auto-handle |
| **L2** | Код-баг, требует PR | AutoFix cycle → TG approval |
| **L3** | Infrastructure, требует restart/check | Health check → Railway restart |
| **L4** | Critical, manual intervention | TG escalation alert |

### 2.3. Logger hints (fallback)

Если regex не сматчился — классификация по имени логгера:

| Logger prefix | Категория |
|---------------|-----------|
| `clients.claude`, `anthropic` | claude_api |
| `aiogram` | telegram_api |
| `core.unstick` | fsm |
| `core.tracing` | fsm |
| `db.`, `asyncpg` | db |
| `clients.mcp` | mcp |
| `core.scheduler` | scheduler |
| `engines.feed` | scheduler |

Severity по умолчанию — L1.

### 2.4. Haiku fallback

Если ни regex, ни logger hint не сработали — категория `unknown`. В конце цикла `classify_unprocessed()` до **5 unknown ошибок** классифицируются через Claude Haiku:
- Budget: `max_tokens=100`, 8s timeout
- Возвращает `{category, severity, action}` с валидацией (whitelist категорий и severity)
- На фейл возвращается в `unknown`, обработается в следующем цикле

---

## 3. Suppression allowlist

Ошибки, которые классифицируются и сохраняются в `error_logs`, но **не эскалируются** (benign noise):

```python
_SUPPRESSED_PATTERNS = [
    r"bot was blocked by the user",
    r"user.*deactivated",
    r"chat not found",
    r"Forbidden.*blocked",
    r"ConflictError.*polling",         # transient Railway redeploy
    r"terminated by other.*getUpdates", # webhook/polling switch
    r"RetryAfter|flood.?control",       # TG rate limit, auto-handled
]
```

**Почему не удалять:** suppressed ошибки нужны для метрик (Grafana "L1 Recoveries 24h"), но бесполезны как алерты.

---

## 4. Scheduler интеграция

`core/scheduler.py` → `run_maintenance_loop()` (cron каждую минуту):

| Интервал | Действие | Функция |
|----------|----------|---------|
| 5 мин | Классификация новых ошибок | `error_classifier.classify_unprocessed()` |
| 15 мин | L1 → L2 auto-escalate persistent | `error_classifier._escalate_persistent_l1()` |
| 15 мин | L3/L4/unknown escalation alert | `error_classifier.check_escalation()` |
| 15 мин | L2 AutoFix cycle (Claude → PR approval) | `autofix.run_autofix_cycle(bot, dev_chat_id)` |
| 15 мин | L3 Health check (cascade → restart) | `health_check.run_l3_health_check(bot, dev_chat_id)` |
| 24ч (00:00) | Cleanup error_logs старше 7 дней | `db.queries.errors.cleanup_old_errors(days=7)` |

---

## 5. L1 → L2 auto-escalation

**Критерий:** persistent L1 ошибки с действием "retry", которые повторяются ≥3 раз за >1 час — это не transient, это код-баг.

```sql
UPDATE error_logs
SET severity = 'L2',
    suggested_action = 'PR: persistent L1 → auto-escalated to L2'
WHERE severity = 'L1'
  AND occurrence_count >= 3
  AND first_seen_at < NOW() - INTERVAL '1 hour'
  AND last_seen_at > NOW() - INTERVAL '24 hours'
  AND escalated = FALSE
  AND suggested_action ILIKE '%retry%'
```

Такие ошибки попадают в следующий цикл AutoFix (§ 6.2).

---

## 6. Автоматическая реакция

### 6.1. L3/L4 Escalation alert

`check_escalation()` → TG dev chat (HTML форматирование):

```
🚨 ESCALATION (N ошибок требуют внимания)

  🟠 [mcp/L3] MCP connection failed x12
  🔴 [db/L4] relation "interns" does not exist x3
  ...

👉 /errors — полный отчёт
```

Критерии отбора:
- `escalated = FALSE`
- `last_seen_at > NOW() - 1 hour`
- `severity IN ('L3', 'L4')` **OR** (`category = 'unknown' AND occurrence_count >= 5`)
- LIMIT 5, сортировка L4 → L3 → остальные, затем по occurrence_count DESC
- Фильтр `is_suppressed()` перед отправкой

После отправки — `escalated = TRUE`.

### 6.2. L2 AutoFix (WP-45 Phase 3)

`core/autofix.py` → `run_autofix_cycle(bot, dev_chat_id)`:
1. Находит необработанные L2 ошибки в `error_logs`
2. Claude Sonnet анализирует traceback + контекст, предлагает диагноз и PR
3. Proposal отправляется в TG dev chat с кнопками approve/reject
4. Approved → создаётся PR (через GitHub Actions или вручную по инструкции)

### 6.3. L3 Health Check (WP-45 Phase 4)

`core/health_check.py` → `run_l3_health_check(bot, dev_chat_id)`:
1. Детект каскадных ошибок (L3 серия за короткий период)
2. Railway redeploy через API
3. TG уведомление о рестарте

**Различение причин отказа (2026-07-07):** `_get_latest_deployment_id()` различает две причины провала запроса к Railway API — GraphQL `errors` в ответе (токен невалиден/не авторизован → `RailwayAuthError`, TG-текст «токен отклонён») и пустой список `edges` при успешном ответе (сервис реально без активных деплоев, TG-текст «нет активных деплоев»). До этого обе причины репортились одинаковым текстом «не удалось получить deployment ID», что маскировало реальную причину сбоя авто-рестарта.

**Тип токена и заголовок авторизации (2026-07-07):** `RAILWAY_API_TOKEN` — project-scoped токен (Railway Project Settings → Tokens), а не account-токен. Такие токены аутентифицируются заголовком `Project-Access-Token`, не `Authorization: Bearer` (последний — только для account/workspace/OAuth токенов). Неверный заголовок возвращает GraphQL-ошибку `Not Authorized` даже для валидного, корректно заскоуженного токена — это и было первопричиной инцидента 2026-07-07 (L3 не мог перезапустить бот ни на pilot, ни на prod).

---

## 7. Grafana Dashboard

**Файл:** `monitoring/grafana-dashboard.json` (26 data-панелей + 2 row-секции = 28 объектов)
**Datasource:** PostgreSQL → Neon (`error_logs`, `traces`)

Структура: первые 9 панелей — Errors (flat, без row-группировки), затем row **Performance & Throughput (WP-45 Ф2)** → 8 панелей, затем row **Engagement Analytics** → 9 панелей.

### 7.1. Errors (первые 9 панелей, flat)

| Панель | Тип | Метрика |
|--------|-----|---------|
| Errors (24h) | stat | `COUNT(*) FROM error_logs WHERE last_seen_at > NOW() - 24h` |
| L3+ Errors | stat | `COUNT(*) WHERE severity IN ('L3','L4')` |
| Unknown Errors | stat | `COUNT(*) WHERE category = 'unknown'` |
| Unique Errors (24h) | stat | `COUNT(DISTINCT message)` |
| L1 Recoveries (24h) | stat | suppressed errors count |
| Error Rate by Category | time series | `GROUP BY hour, category` |
| Severity Distribution | pie | `GROUP BY severity` |
| Recent Classified Errors | table | последние 20 с category/severity |
| Unknown Errors (Triage) | table | `category = 'unknown' ORDER BY occurrence_count DESC` |

### 7.2. Row: Performance & Throughput (WP-45 Ф2)

| Панель | Метрика |
|--------|---------|
| Throughput (RPM by command) | `COUNT(*) / 60 GROUP BY command` |
| Response Latency (p50/p95/p99) | `percentile_cont` на `traces.duration_ms` |
| Requests (24h) | `COUNT(*) FROM traces` |
| Median Latency (ms) | `percentile_cont(0.5)` |
| p99 Latency (ms) | `percentile_cont(0.99)` |
| Error Rate % | `errors / total * 100` |
| Slowest Spans (avg ms by span type) | `AVG(duration_ms) GROUP BY span_type` |

### 7.3. Row: Engagement Analytics

DAU, WAU, MAU, sessions (24h), avg session (min), QA helpful %, DAU trend (30d), sessions per day, retention cohorts (D1/D7/D30), session entry points.

**Источник:** `traces` table + агрегаты из `db/queries/` (см. P-04 Stats Collection).

---

## 8. Schema `error_logs`

Фактическая схема (`db/models.py` + ALTER миграции):

| Поле | Тип | Назначение |
|------|-----|-----------|
| `id` | SERIAL PK | — |
| `logger_name` | TEXT NOT NULL | Источник ошибки (для logger hints) |
| `message` | TEXT NOT NULL | Основное сообщение |
| `traceback` | TEXT | Stack trace (опционально) |
| `context` | JSONB | Дополнительный контекст (user_id, handler, state) |
| `category` | TEXT | `NULL` = unprocessed, иначе одна из 8 категорий |
| `severity` | TEXT | `L1`..`L4` |
| `suggested_action` | TEXT | Из паттерна или Haiku |
| `occurrence_count` | INT DEFAULT 1 | Инкремент при duplicate detection |
| `first_seen_at` | TIMESTAMPTZ | Для L1→L2 escalation (age > 1h) |
| `last_seen_at` | TIMESTAMPTZ | Последнее появление, для retention |
| `alerted` | BOOLEAN DEFAULT FALSE | **Legacy** (до WP-45), ещё используется в индексе |
| `escalated` | BOOLEAN DEFAULT FALSE | **Актуальное** поле — ставится после TG escalation (`check_escalation`, `_escalate_persistent_l1`) |

**⚠️ Несогласованность имён:** таблица содержит оба поля `alerted` и `escalated` — первое legacy (индекс `idx_error_logs_alerted` пока существует), второе актуальное. Чистка legacy — отдельный техдолг (WP-7).

Retention: 7 дней (midnight cleanup в scheduler → `cleanup_old_errors(days=7)`).

Ошибки ограничения частоты Discourse (HTTP 429) относятся к планировщику, а
не к Claude API. После исчерпания локальных повторов текущий пакет опроса
останавливается до следующего часового запуска; счётчик отсутствующих топиков
при этом не изменяется. Одновременно работает не больше одного пакета; пакет
ограничен десятью топиками, между запросами выдерживается одна секунда. После
трёх подтверждённых HTTP 404 топик проверяется раз в неделю, а не исключается
навсегда.

---

## 9. Dev команды

См. [docs/dev-commands.md](../dev-commands.md):

- `/errors` — полный отчёт об ошибках
- `/health` — состояние всех подсистем
- `/latency` — p50/p95/p99 в реальном времени
- `/analytics` — расширенная аналитика с throughput и error rate

---

## 10. Связанные документы

- **DP.RUNBOOK.001-aist-bot-errors.md** — источник паттернов, severity, действий
- **P-04 Stats Collection** — метрики, которые Grafana показывает в Engagement Analytics
- **P-09 Notification idempotency** — notification_log как отдельный механизм (не ошибки)

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-08-06 | WP-330 Ф13: HTTP 429 Discourse отделён от 404; пакетный опрос останавливается без исключения доступных топиков |
| 2026-04-11 | Создан документ (WP-7 DOC1.A-1) |
| 2026-04-06 | WP-45 Ф2: throughput/SLA panels, error suppression (df0a353) |
| 2026-04-04 | WP-45 Phase 4: L3 Health Check → Railway restart |
| 2026-04-02 | WP-45 Phase 3: L2 AutoFix cycle (Claude diagnosis + PR approval) |
| 2026-03-28 | WP-45 Ф1: базовая классификация + escalation |
