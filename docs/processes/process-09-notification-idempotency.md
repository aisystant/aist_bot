# P-09 Notification Idempotency

> Единая idempotency-шина для всех уведомлений бота. `notification_log` с уникальным ключом `{type}:{chat_id}:{date}:{detail}` заменяет 6 разрозненных guard-ов. Паттерн: log-before-send. Правило: если сначала отправили, потом записали — при падении записи следующий прогон отправит дубль.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Тип | Процесс (инфраструктура доставки) |
| Источник | WP-152 Ф3 — унификация идемпотентности; `CLAUDE.md § 10.10` |
| Файлы | `db/queries/notifications.py`, `db/queries/nudges.py` (get_nudge_candidates только) |
| Таблица | `notification_log` (единая) |
| Принцип | **Log-before-send** — запись в БД ПЕРЕД отправкой |

---

## 1. Зачем — история проблемы

До WP-152 в боте было 6+ разрозненных guard-ов против дублей:

| Guard | Таблица / поле | Проблема |
|-------|----------------|----------|
| Reminders | `reminders.sent` | SELECT+UPDATE race condition, scheduler каждую минуту |
| Marathon content | `marathon_content.notification_sent_at` | Отдельное поле, catch-up логика |
| Nudges | `nudge_log` (удалён W15) | Cooldown-based, не полная идемпотентность |
| Conversion events | milestone dedup в `conversion_events` | Inline логика |
| Trial expiry | — | Без guard → возможны дубли |
| Feed digest | — | Без guard → возможны дубли |

**Паттерн ошибки:** send → log. Если send успешен, но log упал → следующий прогон scheduler увидит «не отправлено» и пошлёт повторно. Особенно заметно в catch-up и retry (`_catch_up_missed_deliveries`).

**Решение (WP-152 Ф3):** единая таблица `notification_log` + helper `try_insert_notification()`, возвращающий `False` если ключ уже есть. Порядок действий инвертирован: сначала INSERT, потом send.

---

## 2. Таблица `notification_log`

```sql
CREATE TABLE notification_log (
    id SERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    notification_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload JSONB DEFAULT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(idempotency_key)
);

CREATE INDEX idx_notification_log_chat_type ON notification_log(chat_id, notification_type);
CREATE INDEX idx_notification_log_created ON notification_log(created_at);
```

**Инварианты:**
- `UNIQUE(idempotency_key)` — единственный guard. Race condition на PostgreSQL-уровне: два параллельных INSERT → один success, второй `UniqueViolation`.
- `notification_type` — категория (`marathon_lesson`, `reminder`, `nudge`, `trial_expiry`, `milestone`, `feed_digest`, `event`).
- `payload` (опционально) — метаданные для debugging / Ф4 notification_engagement.
- `created_at` — TIMESTAMP naive (правило §10.6), `DEFAULT NOW()`.

**Создание:** `ensure_notification_log()` вызывается из init-пути бота. Idempotent (`CREATE TABLE IF NOT EXISTS`).

---

## 3. Формат idempotency_key

Правило: `{type}:{chat_id}:{date}:{detail}`. Разделитель `:`, дата `YYYY-MM-DD` в MSK.

**Примеры из `core/scheduler.py`:**

| Notification type | Key shape | Line |
|-------------------|-----------|------|
| `marathon_lesson` | `marathon_lesson:{chat_id}:{date}:{topic_idx}` | `:620` |
| `reminder` | `reminder:{chat_id}:{date}:{reminder_type}` | `:705` |
| `feed_digest` | `feed_digest:{chat_id}:{date}` | `:450` |
| `trial_expiry` | `trial_expiry:{chat_id}:{day_marker}` | `:1350` |
| `milestone` | `milestone:{chat_id}:{milestone_name}` | `:1472` |
| `nudge` | `nudge:{chat_id}:{date}:{nudge_key}` | `:1582` |
| `event` | `event:{chat_id}:{event_id}` | `:1673` |

**Правило выбора detail:**
- **Daily events** (lesson, reminder, digest) → включать дату → ровно одна отправка за сутки
- **One-off events** (milestone, trial expiry markers) → включать семантический маркер → ровно одна отправка за всю жизнь user
- **Parameterized** (event, nudge) → включать ID/ключ правила → одна отправка на уникальное событие

---

## 4. API: три функции

### 4.1. `try_insert_notification(chat_id, type, key, payload) → bool`

**Низкоуровневая.** Попытка INSERT, возвращает:
- `True` — запись успешна, можно отправлять
- `False` — ключ уже существует (дубль), отправлять не нужно

**Обработка исключений:** специфично ловит `UniqueViolation` (SQLSTATE `23505` или substring `unique` в тексте). Другие ошибки БД пробрасываются.

**Используется в scheduler когда нужен manual control:**
```python
if not await try_insert_notification(chat_id, 'feed_digest', feed_key):
    continue  # дубль, пропустить
# ... сборка и отправка
```

### 4.2. `send_idempotent(chat_id, type, key, send_fn, payload) → bool`

**Высокоуровневый wrapper** — log-before-send в одной функции:

```python
inserted = await try_insert_notification(chat_id, type, key, payload)
if not inserted:
    return False
await send_fn()  # ← только после успешного INSERT
return True
```

**Использовать когда:** простая схема «записал → отправил», без промежуточной логики. Callers передают `send_fn` как closure.

**Логирование:** `DEBUG` для skip, `INFO` для sent.

### 4.3. `was_notification_sent(key) → bool`

Read-only проверка. Используется редко — обычно достаточно `try_insert_notification`, которое атомарно и checks+inserts.

### 4.4. `get_notification_stats(chat_id, days) → dict`

Агрегация: `{notification_type: count}` за N дней. Используется в **WP-152 Ф4** для `development.notification_engagement` VIEW → синхронизация в `digital_twins.data['2_collected']['2_5_notifications']` (см. [P-07 § 12b](process-07-dt-engagement-sync.md)).

---

## 5. Log-before-send паттерн (§10.10)

**Правило:** СНАЧАЛА записать в БД факт отправки, ПОТОМ `bot.send_message()`.

**Почему не send→log:**
```
send OK → log FAIL → next scheduler tick → dup send
```

**Почему log→send безопасен:**
```
log OK → send FAIL → user не получит → retry через manual trigger
log OK → send OK → норма
log FAIL → send не выполняется → retry в next tick
```

Цена: при send failure запись остаётся, пользователь не получает сообщение. Mitigation: error_classifier видит `telegram_api` ошибку → алерт через [P-06 Observability](process-06-observability.md), дальше manual intervention (TD1 команда `/delivery`). Это осознанный trade-off: лучше потерянное сообщение, чем дубль.

---

## 6. Подтаблица: reminders (FOR UPDATE SKIP LOCKED)

Отдельно от notification_log — для reminders используется concurrent-safe pattern с `FOR UPDATE OF r SKIP LOCKED` (`core/scheduler.py:770`):

```sql
SELECT ... FROM reminders r
WHERE ... AND sent = FALSE
FOR UPDATE OF r SKIP LOCKED
```

**Почему не только notification_log:** reminders — это рабочая очередь, не только guard. `SKIP LOCKED` нужен, чтобы предыдущий scheduler tick не держал строки под lock, когда следующий уже стартует (apscheduler запускается каждую минуту).

**Двойная защита:** `reminders.sent = TRUE` (локальный guard) + `notification_log` (глобальный idempotency). При падении между двумя записями — notification_log выигрывает, дубль не уйдёт.

---

## 7. `nudge_log` — удалён (W15)

**Статус:** DONE. Удалён в W15 (WP-7 сессия 6, 12 апр).

**Cooldown через notification_log:** `notifications.was_nudge_sent_recently(chat_id, nudge_key, cooldown_days)` — LIKE-запрос по `idempotency_key LIKE 'nudge:{chat_id}:%:{nudge_key}'` за последние `cooldown_days` дней. Семантика cooldown сохранена.

**`get_nudge_candidates()`** остаётся в `nudges.py` — выборка T1+ пользователей с engagement/derived данными для nudge-анализа. К идемпотентности не относится.

---

## 8. Callers в `core/scheduler.py`

| Callsite | Тип | Что защищает |
|----------|-----|--------------|
| `:450` | `feed_digest` | Дневной digest в Ленте |
| `:620` | `marathon_lesson` | Доставка урока марафона |
| `:705` | `reminder` | Напоминания пользователю |
| `:1350` | `trial_expiry` | Уведомления об истечении триала |
| `:1472` | `milestone` | Достижение milestones |
| `:1582` | `nudge` | Nudge-система |
| `:1673` | `event` | Событийные уведомления |

**Правило добавления нового notification type:**
1. Придумать стабильный `idempotency_key` по правилам §3
2. Импортировать `try_insert_notification` или `send_idempotent`
3. Гарантировать порядок: log → send
4. Обновить таблицу в §3 и §8 этого документа

---

## 9. Антипаттерны (что делать нельзя)

- ❌ Отправлять сообщение ДО `try_insert_notification` — теряется идемпотентность
- ❌ Использовать UUID / timestamp в `idempotency_key` — каждый вызов создаст новую запись, guard бесполезен
- ❌ Не ловить `UniqueViolation` вручную — это уже делает `try_insert_notification`, пробрасывать стек наружу не надо
- ❌ Писать в `notification_log` без уникального ключа, полагаясь на `UPDATE ... WHERE NOT EXISTS` — race condition; только INSERT + UNIQUE constraint
- ❌ Смешивать naive и aware datetime в `created_at` — notification_log использует `NOW()` (naive UTC). Не менять без синхронизации с §10.6
- ❌ Запускать scheduler цикл `for days_ahead in [1, 0]` без `sent_chat_ids: set()` — защита от дублей на стыке условий внутри одного tick (см. §10.10 правило dedup)

---

## 10. Связанные процессы

- **[P-06 Observability](process-06-observability.md)** — error_classifier ловит `telegram_api` ошибки при send failure после успешного log
- **[P-07 DT Engagement Sync](process-07-dt-engagement-sync.md)** — `notification_engagement` VIEW → `2_5_notifications` в digital twins, использует `notification_log` как источник
- **P-11 Pre-generation** (TODO) — планировщик контента, использует `try_insert_notification('marathon_lesson', ...)` при уведомлении
- **CLAUDE.md § 10.10** — первоисточник правил log-before-send, dedup, status-before-message, concurrent access
