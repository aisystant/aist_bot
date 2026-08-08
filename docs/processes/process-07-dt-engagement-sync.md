# P-07 DT Engagement Sync

> Ежедневная синхронизация engagement-данных пользователя из Neon views + LMS DB → `digital_twins.data['2_collected']`. Бот = collector. Вычисления (`3_derived`) выполняются **вне бота** — в R28 Profiler.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Тип | Процесс (collector) |
| Источник | WP-85 Phase 4, WP-151, WP-218 Ф2, WP-109 Ф3+Ф7, WP-152 Ф4, WP-175 Ф5 |
| Файл | `db/queries/dt_sync.py` (769 строк) |
| Таблица | `digital_twins` (Neon, shared с DT MCP worker) |
| Расписание | cron `hour=4, minute=30` ежедневно (+ hourly retry для подключённых) |
| Архитектурное решение | WP-218 Ф2 — calculator вынесен в отдельный сервис |

---

## 1. Архитектурная граница (WP-218 Ф2)

**Бот — collector, не calculator.**

```
                    Neon (shared БД)
                    ┌─────────────────────┐
LMS DB   ───┐       │ digital_twins       │◄──── R28 Profiler
(suser)     │       │   .data             │      (standalone runtime)
            ▼       │     ['2_collected'] │      recalculate_derived.py
Бот  ───► sync_engagement_to_dt()────► WRITE ◄───READ
(aist_bot)          │     ['3_derived']   │      WRITE ────────┐
                    │   updated_at        │                     │
                    └─────────────────────┘                     │
                                                                │
DT MCP worker ◄──READ (по запросу пользователя)               ──┘
(cloudflare)                                                  Профиль → /twin
```

**Что делает бот (collector):**
- Читает `development.engagement`, `development.notification_engagement`, `development.learning_history`, `development.user_events` (VIEW + таблицы)
- Подтягивает квалификацию из LMS DB (отдельное подключение)
- Пишет deep merge в `digital_twins.data['2_collected']` по ключу `user_id`

**Что НЕ делает бот:**
- ❌ Не вычисляет `3_derived` (student_stage, agency_index, slot_regularity, mastery_by_area)
- ❌ Не содержит `dt_calc.py` — удалён в WP-218 Ф2, calculator теперь в `DS-ai-systems/profiler/scripts/recalculate_derived.py`

**Что владеет бот:**
- ✅ Определения VIEW `development.engagement` и `development.notification_engagement` — создаются при инициализации БД из `db/models.py`. Другие потребители (DT MCP worker, Profiler) читают эти VIEW, но не определяют их.

---

## 2. Расписание (актуальное состояние на 01.08.2026)

| Триггер | Момент срабатывания | Функция | Что делает | Статус |
|---------|---------------------|---------|-----------|--------|
| APScheduler cron | ежедневно 04:30 MSK (закомментировано) | `sync_engagement_to_dt()` | Полный collector-проход по всем пользователям с `user_uuid`. | **Отключено** в `core/scheduler.py` (WP-268 Phase 4+): collector читает старые `development.*` views, отсутствующие в Railway Postgres `bot_data`. |
| scheduler loop | в начале каждого часа (`minute == 0`) | `_sync_dt_connected_users()` | Retry для подключённых через OAuth (`dt_tokens`). | **Не обнаружен** в текущем `core/scheduler.py`; ранее документированный hourly retry не активен. |
| Telegram-команда (developer-only) | по запросу разработчика | `sync_engagement_to_dt()` | Ручной запуск полного collector. | Активно: `/dt_sync` в `handlers/dev.py`. |
| GitHub webhook | после push в `workbook/` | `sync_one_user_to_dt(user_id)` | On-demand collector для одного пользователя. | Активно: `oauth_server.py` (`github_workbook_webhook_handler`). |

**Целевая архитектура (WP-270):** projection-worker → `indicators.calculated_profile` (Neon). Старый collector (`digital_twins.data['2_collected']`) сохраняется как реализованный код, но daily batch-trigger отключён до миграции/решения о замене.

Регистрация cron: `core/scheduler.py` — строка `_scheduler.add_job(_dt_sync_engagement, ...)` закомментирована (`# WP-268 Phase 4+ ...`).

---

## 3. Источники данных

### 3.1. `development.engagement` VIEW

Основной источник, 25+ полей на пользователя. **Владение VIEW:** определение хранится в `db/models.py` (строки 1108+, `CREATE VIEW development.engagement ...` при инициализации/миграции БД) — бот является owner этого VIEW. Другие потребители (DT MCP worker, R28 Profiler) читают VIEW, но не определяют его.

| Поле | Используется в секции |
|------|----------------------|
| `user_uuid`, `user_id` | ключ + telegram_id |
| `sessions_total`, `events_total`, `first_event_at`, `last_event_at` | `2_1_account` |
| `marathon_steps_total`, `feed_completed_total` | `2_2_courses` |
| `training_attempts_total`, `training_passed_total`, `assessments_total`, `marathon_tasks_total` | `2_3_practice` |
| `active_days`, `events_last_7d`, `events_last_30d`, `ai_chats_total` | `2_4_time` |
| `onboarding_completed_total`, `mode_changes_total`, `settings_changes_total`, `reminders_delivered_total`, `reminders_opened_total`, `errors_shown_total`, `help_views_total`, `progress_views_total`, `marathon_completions_total` | `2_8_operations` |

> Сама таблица `development.user_events` (данные, на которых VIEW агрегирует) наполняется несколькими сервисами — бот пишет события о взаимодействии пользователя, экзокортекс-collector пишет события с `source='iwe'`/`source='exocortex'`. Бот не единственный producer данных, но единственный owner определения VIEW.

### 3.2. `development.notification_engagement` VIEW

Источник для `2_5_notifications` (WP-152 Ф4):

- `notifications_total`, `notifications_7d`, `notifications_30d`
- `notification_types` (JSONB breakdown)
- Типы: `lesson_*`, `reminder_*`, `nudge_*`, `trial_expiry_*`, `feed_digest_*`, `milestone_*`
- `first_notification_at`, `last_notification_at`

Предзагрузка одним запросом в `notif_map: dict`.

### 3.3. `development.learning_history` (WP-175 Ф5)

Источник для BKT (`mastery_by_area`, `worldview_gaps`). Фильтр `schema_version = 2`. Предзагрузка в `learning_map: dict[user_uuid → list]`. Используется в Profiler (не в collector), бот передаёт её транзитом.

### 3.4. `development.user_events` (ADR-009)

Источник для `2_6_coding`, `2_7_iwe`, `2_8_decisions` — **только для source='iwe' и source='exocortex'**:

| event_type | Агрегация | Секция |
|-----------|-----------|--------|
| `coding_time` | `SUM(payload->>'total_seconds'::int)` за today/7d/30d | `2_6_coding` |
| `coding_time` | `COUNT(DISTINCT DATE)` за 30d | `2_6_coding.coding_active_days_30d` |
| `commit_created` | COUNT за today/7d/30d | `2_7_iwe` |
| `day_open` | `COUNT(DISTINCT DATE)` за 30d | `2_7_iwe.day_opens_30d` |
| `decision_*` (WP-109 Ф7) | COUNT сегодня + `SUM(weight::int)` + 7d avg | `2_8_decisions` |

**Fallback:** если `user_events` пусты для этого юзера — подтягивается из существующего `digital_twins.data['2_collected']` (snapshot от `dt-collect.sh`, переходный период).

### 3.5. LMS DB (прямое подключение, WP-151 fix)

`LMS_DATABASE_URL` → отдельное подключение к PostgreSQL LMS (отдельная инфраструктура).

**Цель:** квалификация пользователя = **source-of-truth Методсовет МИМ**, не вычисляется в боте.

```sql
SELECT DISTINCT ON (s.id)
    s.id AS suser_id, qle.level, qle.event_date, qle.reason
FROM suser s
JOIN contact c ON c.value = s.email AND c.contact_type = 0
JOIN qualification_level_event qle ON qle.kontragent_id = c.kontragent_id
WHERE s.id = ANY($1::bigint[])
ORDER BY s.id, qle.event_date DESC, qle.id DESC
```

Связка: `public.users.aisystant_id` = `suser.id` → email → kontragent → qualification_level_event.
Маппинг названий уровней:

| LMS level | Code | Numeric |
|-----------|------|---------|
| Интересант | L05 | 5 |
| Определяющийся | L08 | 8 |
| Первокурсник | L1 | 10 |
| Ученик | L2 | 20 |
| Работник | L25 | 25 |
| Стратег | L3 | 30 |
| Специалист | L4 | 40 |
| Практик | L5 | 50 |
| Мастер | L6 | 60 |
| Реформатор | L7 | 70 |
| Деятель (революционер) | L8 | 80 |

Graceful degradation: если `LMS_DATABASE_URL` не задан → квалификация пропускается, остальной sync продолжается.

### 3.6. Дефолтная квалификация при завершении онбординга (WP-406 Ф31)

`ensure_default_qualification(chat_id)` — вызывается из точек логирования
`onboarding_completed` (`core/onboarder/x2.py:_finish_x2`,
`handlers/onboarding.py:on_x3_confirm`). Правило (решение пилота 08.08.2026):

- уже есть `2_collected.2_2_courses.qualification_level` в ЦД **или** живой
  LMS-уровень Работник+ (numeric ≥ 25) → не трогаем и не понижаем;
- квалификации нет → пишется «Ученик» (`{level, code=L2, numeric=20, event_date,
  reason=onboarding_completed_default_wp406_f31}`) тем же upsert-путём в
  `digital_twins`; живая шкала: Ученик = 4, автоназначение всегда < Работник(5);
- fail-open: любая ошибка логируется и глотается, онбординг не ломается.

**Merge углублён до `2_2_courses`:** оба upsert'а sync'а (bulk + single) сливают
`2_2_courses` поэлементно (`старое || новое`), а не заменяют секцию целиком —
иначе ночной sync затирал бы назначенную при онбординге квалификацию (payload
sync'а несёт `qualification_level` только при LMS numeric ≥ 25).

---

## 4. Секции `2_collected` (финальная схема)

JSONB в `digital_twins.data['2_collected']`:

| Ключ | Источник | Что содержит |
|------|----------|--------------|
| `2_1_account` | engagement | Активность (sessions, events, first/last timestamps) |
| `2_2_courses` | engagement + LMS | Марафон/Лента прогресс + квалификация (level, code, numeric, event_date, reason) |
| `2_3_practice` | engagement | Training/assessment/tasks |
| `2_4_time` | engagement | Активные дни, события за 7/30 дней, AI chats |
| `2_5_notifications` | notification_engagement | Доставленные уведомления по типам (WP-152 Ф4) |
| `2_6_coding` | user_events (iwe) | Coding time today/7d/30d + active days |
| `2_7_iwe` | user_events (iwe) | Commits + day opens |
| `2_8_decisions` | user_events (exocortex) | decision events + weight sum/avg (WP-109 Ф7) |
| `2_8_operations` | engagement | Onboarding, mode changes, errors, progress views и т.д. |

> **⚠️ Коллизия ключей:** в коде `2_8_operations` и `2_8_decisions` — разные секции с одинаковым префиксом `2_8`. Технически не проблема (разные вложенные ключи), но это **несогласованность именования** — отдельный техдолг. Схема, определённая изначально, предполагала одно «8-e семейство», но фактически их два.

---

## 5. Deep merge запись

Бот никогда не перезаписывает `digital_twins.data` целиком — только `2_collected`:

```sql
INSERT INTO digital_twins (user_id, data, created_at, updated_at)
VALUES ($1, $2::jsonb, NOW(), NOW())
ON CONFLICT (user_id) DO UPDATE SET
    data = COALESCE(digital_twins.data, '{}'::jsonb)
        || jsonb_build_object('2_collected',
            COALESCE(digital_twins.data->'2_collected', '{}'::jsonb)
            || ($2::jsonb->'2_collected')
        ),
    updated_at = NOW()
```

Это гарантирует:
- `3_derived` (вычисляется Profiler'ом) не затирается
- `1_declarative` (задаётся пользователем через /profile) не затирается
- Старые секции `2_collected`, не обновлённые в этом проходе, сохраняются

---

## 6. Идентификация пользователя

Выбор ключа для записи в `digital_twins.user_id`:
1. Предпочтительно — `dt_tokens.dt_user_id` (Ory OAuth `sub`, T1+) — DT MCP worker ищет именно по нему
2. Fallback — `engagement.user_uuid` (T0, пока не подключился)

Причина: DT MCP worker (Cloudflare) при запросе `/twin` читает по `dt_user_id`. Если бот напишет по `user_uuid`, а пользователь после OAuth, worker не найдёт — нужен consistent key.

**Затрагивается в WP-227** (отдельная БД для digital_twins + unified user_id policy: T0→users.id, T1+→ory_id, один ряд на человека). До завершения WP-227 действует гибридный режим выше.

---

## 7. Graceful degradation

Collector продолжает работу при отказе любого одного источника:

| Источник | Fallback при отказе |
|----------|---------------------|
| `development.engagement` | ❌ Весь sync невозможен (основной источник) |
| `development.notification_engagement` | `2_5_notifications` пропускается, warning в лог |
| `development.learning_history` | BKT данные не передаются, warning в лог |
| LMS DB | `qualification_level` пропускается, warning |
| `development.user_events` (iwe) | `2_6_coding`/`2_7_iwe` берутся из существующих `2_collected` (snapshot) |
| `development.user_events` (exocortex) | `2_8_decisions` пропускается |

Ошибка на одном юзере → `stats.errors++`, sync продолжается для остальных.

---

## 8. Функции

| Функция | Роль |
|---------|------|
| `sync_engagement_to_dt()` | Основной entry point, полный проход по всем пользователям (batch) |
| `sync_one_user_to_dt(user_id)` | Одиночная синхронизация (используется в webhook после push в `workbook/`; ранее упоминался в OAuth/hourly retry, сейчас не активен) |
| `_preload_lms_qualifications(lms_user_ids)` | Прямое подключение к LMS DB, batch fetch квалификаций |
| `_ts(val)` | Сериализация datetime → ISO 8601 |

---

## 9. Связанные документы

- **WP-218 Ф2** — архитектурное решение вынести calculator из бота (контекст в `DS-my-strategy/inbox/WP-218-canonical-metric-chain.md`)
- **WP-227** — unified user_id + отдельная БД для digital_twins (в работе)
- **WP-268** — полный перелив Aisystant Neon БД в новую архитектуру; в рамках него отключён daily cron P-07
- **WP-270** — projection-workers для cut-over Neon; целевая замена старого batch-collector → `indicators.calculated_profile`
- **ADR-009** — Personal Knowledge MCP → Activity Hub напрямую (источник `source='iwe'`)
- **P-06 Observability** — ошибки dt_sync попадают в `error_logs` с категорией `dt`
- **P-09 Notification idempotency** — источник `notification_engagement`, которую потребляет P-07

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-08-08 | WP-406 Ф31: `ensure_default_qualification` (дефолт «Ученик» при завершении онбординга) + deep merge `2_2_courses` в обоих upsert'ах |
| 2026-08-01 | WP-502 A2.19: повторный триаж — актуализировано расписание (cron отключён, manual/webhook triggers), добавлены WP-268/WP-270, уточнена роль `sync_one_user_to_dt` |
| 2026-04-11 | Создан документ (WP-7 DOC1.A-2) |
| 2026-04-10 | WP-109 Ф7: decision weight aggregation → 2_8_decisions (0ffd1ef) |
| 2026-04-08 | WP-151 fix: qualification_level из LMS DB (d0c8bf3) |
| 2026-04-07 | WP-218 Ф2: удалён dt_calc.py из бота → Profiler (c91cb59) |
| 2026-04-05 | WP-109 Ф3: coding events из user_events (5f6311e) |
| 2026-04-02 | WP-151 Ф3: 10 новых типов событий + engagement VIEW расширен |
| 2026-03-28 | WP-152 Ф4: notification_engagement + 2_5_notifications |
