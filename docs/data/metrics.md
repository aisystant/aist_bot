# Метрики

> User-facing и engagement метрики бота: источник, формула, процесс-владелец.
>
> ⚠️ **Не путать с observability-метриками** (p95 latency, error rate, RPM) — они в [P-06 Observability](../processes/process-06-observability.md).
>
> ⚠️ **WP-82 Ф3 миграция:** ссылки на `interns.*` устарели. Поля streak/active_days теперь в `development.user_state`, engagement-метрики — в VIEW `development.engagement` (агрегируется из `development.user_events`).

---

## 1. Два слоя метрик

| Слой | Источник | Писатель | Читатель |
|------|---------|---------|---------|
| **Statefull поля user_state** | `development.user_state` | Сам бот (FSM states) | `/progress`, `/profile`, P-04 |
| **Engagement VIEW** | `development.engagement` (агрегация `development.user_events`) | event stream (логирует все хендлеры) | [P-07 DT sync](../processes/process-07-dt-engagement-sync.md) → `digital_twins.2_collected` |

**Различие:** user_state — счётчики обновляются inline (streak++, total++). VIEW — агрегация всего event stream, пересчитывается каждый раз при запросе (или читается DT sync).

---

## 2. User-facing метрики (быстрые, из `development.user_state`)

### 2.1. Активность и streak

| Метрика | Источник | Формула | Где показывается |
|---------|----------|---------|------------------|
| **Активные дни (всего)** | `user_state.active_days_total` | Инкремент при первой активности за день (P-01) | `/progress`, `/twin` |
| **Текущая серия** | `user_state.active_days_streak` | +1 если вчера был активен, иначе 1 | `/progress`, `/twin` |
| **Рекорд серии** | `user_state.longest_streak` | `MAX(streak)` за всё время | `/progress` |
| **Последний активный день** | `user_state.last_active_date` | Обновляется при любой активности | `/progress` |

**Процесс-владелец:** P-01 (отслеживание активности). Обновляется в `activity_log` + `development.user_state` inline при каждом событии.

### 2.2. Марафон

| Метрика | Источник | Формула | Где показывается |
|---------|----------|---------|------------------|
| **День Марафона** | `user_state.marathon_start_date` | `core.topics.get_marathon_day(intern)` — разница с MSK TZ (§13 CLAUDE.md) | `/progress`, `/twin` |
| **Пройдено тем** | `user_state.completed_topics` | `len(completed_topics)` (JSON array) | `/progress` |
| **Текущая тема** | `user_state.current_topic_index` | — | `/progress` |
| **Тем сегодня** | `user_state.topics_today` | Сбрасывается в 00:00 MSK | `/progress` |
| **Завершённые дни** | Вычисляется | `COUNT(дни с topics_today==2)` из `activity_log` | P-04 отчёт |
| **Отставание** | Вычисляется | `marathon_day - completed_days` | P-04 отчёт |
| **Рабочие продукты** | `answers` | `COUNT WHERE answer_type='work_product'` | P-04 отчёт |
| **Бонусные ответы** | `answers` | `COUNT WHERE answer_type='bonus_answer'` | P-04 отчёт |
| **Статус** | `user_state.marathon_status` | `not_started`/`active`/`paused`/`completed` | `/progress` |

**Инвариант:** `marathon_start_date` и `marathon_status` связаны — любой update `marathon_start_date` ОБЯЗАН ставить `marathon_status=ACTIVE` (§13 CLAUDE.md).

### 2.3. Лента

| Метрика | Источник | Формула | Где показывается |
|---------|----------|---------|------------------|
| **Дайджесты** | `feed_sessions` | `COUNT WHERE status='completed'` | P-04 отчёт |
| **Фиксации** | `answers` | `COUNT WHERE answer_type='fixation'` | P-04 отчёт |
| **Темы недели** | `feed_weeks.accepted_topics` | JSON массив | `/progress` |
| **Текущий день недели** | `feed_weeks.current_day` | 1-7 | `/progress` |
| **Статус Ленты** | `user_state.feed_status` | `not_started`/`active`/`paused`/`completed` | `/progress` |

### 2.4. Сложность (Bloom)

| Метрика | Источник | Формула | Где показывается |
|---------|----------|---------|------------------|
| **Уровень сложности** | `user_state.complexity_level` | 1-3 | `/profile` |
| **Тем на уровне** | `user_state.topics_at_current_complexity` | Инкремент при прохождении | — |

См. [P-02 §7 Content Budget Model](../processes/process-02-content-generation.md) — `BLOOM_MULTIPLIER`, `calc_words`.

---

## 3. Engagement метрики (VIEW `development.engagement`)

> Агрегированные метрики из event stream. Пересчитываются по SELECT. Читаются [P-07 DT sync](../processes/process-07-dt-engagement-sync.md) (cron 04:30 MSK) → пишутся в `digital_twins.data['2_collected']`.

**Источник:** `development.user_events` GROUP BY `user_id, user_uuid`.

### 3.1. Основные счётчики (WP-85 Phase 4)

| Метрика в VIEW | Event type | Описание |
|----------------|-----------|----------|
| `sessions_total` | `session_start` | Кол-во сессий |
| `ai_chats_total` | `ai_chat` | Консультационных запросов |
| `marathon_steps_total` | `marathon_step` | Шагов марафона (урок/вопрос) |
| `marathon_tasks_total` | `marathon_task` | Сданных практик |
| `feed_completed_total` | `feed_completed` | Завершённых дайджестов |
| `training_attempts_total` | `training_attempt` | Попыток тренировки |
| `training_passed_total` | `training_attempt` (payload.passed=true) | Успешных попыток |
| `assessments_total` | `assessment_completed` | Пройденных assessment |

### 3.2. Операционные метрики (WP-151 Ф3)

| Метрика в VIEW | Event type | Описание |
|----------------|-----------|----------|
| `onboarding_completed_total` | `onboarding_completed` | Сколько раз завершал онбординг |
| `mode_changes_total` | `mode_changed` | Переключений между режимами |
| `settings_changes_total` | `settings_changed` | Изменений настроек |
| `reminders_delivered_total` | `reminder_delivered` | Доставленных reminders |
| `reminders_opened_total` | `reminder_opened` | Открытых reminders |
| `errors_shown_total` | `error_shown` | Ошибок показанных user'у |
| `help_views_total` | `help_viewed` | Просмотров `/help` |
| `progress_views_total` | `progress_viewed` | Просмотров `/progress` |
| `marathon_completions_total` | `marathon_completed` | Завершённых марафонов |

### 3.3. Временные агрегации

| Метрика в VIEW | Формула | Описание |
|----------------|---------|----------|
| `events_total` | `COUNT(*)` | Всего событий |
| `first_event_at` | `MIN(created_at)` | Первое событие |
| `last_event_at` | `MAX(created_at)` | Последнее событие |
| `active_days` | `COUNT(DISTINCT created_at::date)` | Дней с событиями |
| `events_last_7d` | `COUNT FILTER (created_at > NOW() - INTERVAL '7 days')` | За 7 дней |
| `events_last_30d` | `COUNT FILTER (created_at > NOW() - INTERVAL '30 days')` | За 30 дней |

**Итого:** 20 engagement-метрик в VIEW.

### 3.4. Identity фильтр для DT sync

```sql
WHERE user_uuid IS NOT NULL  -- T1+
```

T0 пользователи копят события по `user_id = telegram_id`, но DT sync их не подхватывает пока нет `user_uuid` (Ory UUID). При OAuth привязке `user_uuid` backfilled → sync подхватывает автоматически. См. [P-07 §12b «Identity model»](../processes/process-07-dt-engagement-sync.md).

---

## 4. Notification engagement (VIEW `development.notification_engagement`)

> WP-152 Ф4. Агрегация `notification_log` для секции `2_5_notifications` в digital twin.

**Источник:** `notification_log nl JOIN public.users u ON u.telegram_id = nl.chat_id` GROUP BY `u.telegram_id, u.ory_id`.

| Метрика | Формула | Описание |
|---------|---------|----------|
| `notifications_total` | `COUNT(*)` | Всего отправлено |
| `notifications_7d` | `COUNT FILTER (created_at > NOW() - 7d)` | За 7 дней |
| `notifications_30d` | `COUNT FILTER (created_at > NOW() - 30d)` | За 30 дней |
| `notification_types` | `COUNT DISTINCT notification_type` | Разнообразие типов |
| `lesson_notifications` | `COUNT FILTER (type='marathon_lesson')` | Уроков |
| `reminder_notifications` | `COUNT FILTER (type='reminder')` | Напоминаний |
| `nudge_notifications` | `COUNT FILTER (type='nudge')` | Nudges |
| `trial_expiry_notifications` | `COUNT FILTER (type='trial_expiry')` | Истечение триала |
| `feed_digest_notifications` | `COUNT FILTER (type='feed_digest')` | Дайджестов |
| `milestone_notifications` | `COUNT FILTER (type='milestone')` | Milestones |
| `first_notification_at` | `MIN(created_at)` | Первое уведомление |
| `last_notification_at` | `MAX(created_at)` | Последнее |

**Итого:** 11 notification-метрик в VIEW.

**Graceful fallback:** если VIEW не существует — `sync_engagement_to_dt()` логирует warning и продолжает без notifications (см. [P-07 §12c](../processes/process-07-dt-engagement-sync.md)).

---

## 5. Derived метрики (НЕ в боте!)

> **Важно:** следующие метрики вычисляются вне бота — в `DS-ai-systems/profiler/scripts/recalculate_derived.py`. Бот = collector, не calculator (WP-218 Ф2).

Эти поля живут в `digital_twins.data['3_derived']` и читаются ботом только для отображения (`handlers/twin.py` — pure reader).

| Метрика | Где вычисляется | Описание |
|---------|-----------------|----------|
| **slot_regularity** | Profiler | Регулярность попадания в расписание |
| **student_stage** | Profiler | Ступень мастерства (0-4, модель R28) |
| **integral_agency_index** | Profiler | Интегральный индекс Builder path (WP-218) |
| **mastery_by_area** | Profiler | Освоенность по predметным областям |
| **learning (компонент agency)** | Profiler | Из qualifications/publications/knowledge/decisions |
| **longevity** | Profiler | Длительность присутствия (из `first_event_at`) |
| **activity** | Profiler | Интенсивность (из `events_last_30d`) |

**Правило:** если нужно добавить/исправить derived метрику — работа в `profiler/scripts/dt_calc.py`, НЕ в боте. Бот не трогаем, просто следующий cron подхватит изменения.

**Каждое поле в UI бота `/twin`** должно иметь `IND-комментарий` (ссылка на метамодель) для трассируемости. См. WP-218 Ф3 + WP-221 (генератор DTIndicators констант).

---

## 6. Агрегации по периодам (P-04 отчёты)

### 6.1. Недельный отчёт

**Период:** понедельник текущей недели → сегодня (`week_start = today - timedelta(days=today.weekday())`).

| Метрика | SQL |
|---------|-----|
| Активные дни | `SELECT COUNT(DISTINCT activity_date) FROM activity_log WHERE chat_id=$1 AND activity_date >= date_trunc('week', NOW())` |
| Рабочие продукты | `SELECT COUNT(*) FROM answers WHERE chat_id=$1 AND answer_type='work_product' AND created_at >= date_trunc('week', NOW())` |
| Дайджесты | `SELECT COUNT(*) FROM feed_sessions fs JOIN feed_weeks fw ON fs.week_id=fw.id WHERE fw.chat_id=$1 AND fs.status='completed' AND fs.completed_at >= date_trunc('week', NOW())` |

### 6.2. Полный отчёт

**Период:** дата регистрации → сегодня.

| Метрика | SQL |
|---------|-----|
| Дней с регистрации | `(NOW() - u.created_at)::INTERVAL FROM public.users u WHERE telegram_id=$1` |
| Всего активных | `user_state.active_days_total` |
| Всего тем | `len(user_state.completed_topics)` |

---

## 7. Визуализация (в `/progress`)

### 7.1. Прогресс-бары

```
День Марафона: ████████████░░░░░░░░░░░░░░░░ 12/28
Неделя 1:      ██████████████ 14/14 ✅
Неделя 2:      ████░░░░░░░░░░ 4/14
```

### 7.2. Статусы дней

| Иконка | Условие |
|--------|---------|
| ✅ | `topics_completed == 2` |
| 🔄 | `topics_completed == 1` |
| 📍 | `topics_completed == 0 AND day_available` |
| 🔒 | `day > current_day` |

---

## 8. Связь с процессами

| Процесс | Роль |
|---------|------|
| [P-01 Activity Tracking](../processes/process-01-activity-tracking.md) | Записывает streak, active_days в `development.user_state` + `activity_log` |
| [P-04 Stats Collection](../processes/process-04-stats-collection.md) | Агрегирует user-facing метрики для `/progress`, `/profile` |
| [P-06 Observability](../processes/process-06-observability.md) | Отдельный слой: error rate, p95 latency, DAU/WAU/MAU, retention (Grafana) |
| [P-07 DT Engagement Sync](../processes/process-07-dt-engagement-sync.md) | Читает VIEW `development.engagement` + `notification_engagement` → пишет в `digital_twins.2_collected` |
| [P-09 Notification Idempotency](../processes/process-09-notification-idempotency.md) | Источник `notification_log` для VIEW `notification_engagement` |

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Расширен VIEW `development.engagement` (20 метрик) + VIEW `development.notification_engagement` (11 метрик). Добавлена §5 про derived метрики в Profiler (НЕ в боте, WP-218 Ф2). Убраны ссылки на `interns.*` после WP-82 Ф3. |
| 2026-02-03 | Исправлена формула «Активные дни (неделя)»: теперь с понедельника текущей недели, а не за последние 7 дней |
| 2026-01-25 | Добавлены метрики «Завершённые дни» и «Отставание», исправлена формула «День Марафона» |
| 2026-01-23 | Создание документа |
