# P-11 Pre-generation

> Заблаговременная генерация контента марафона (урок + вопрос + практика) за 3 часа до доставки. Плюс look-ahead после доставки — готовим следующую тему. Плюс retry с экспоненциальным backoff при падении. Цель: в момент плановой доставки Claude API не нагружается, пользователь видит мгновенный ответ.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Тип | Процесс (фоновая оптимизация latency) |
| Источник | `CLAUDE.md § 10.19-10.20` (Look-Ahead + Haiku Fallback), WP-209 Ф4 (burst 429) |
| Файл | `core/scheduler.py` (функции: `pre_generate_upcoming`, `pregen_next_for_user`, `_schedule_retry`) |
| Константы | `PREGEN_HOURS_AHEAD = 3`, `_RETRY_DELAYS_MINUTES = [30, 60]`, `Semaphore(20)` |
| Триггеры | APScheduler cron `minute='*'` + event-driven (после доставки lesson/task) |

---

## 1. Три источника pre-gen

### 1.1. `pre_generate_upcoming()` — cron каждую минуту

**Триггер:** APScheduler `cron minute='*'` (см. `init_scheduler()`).

**Алгоритм:**
```
1. target = moscow_now() + 3h
2. users = get_marathon_users_at_time(target.hour, target.minute)
3. Для каждого user (параллельно через Semaphore(20)):
   - intern = get_intern(chat_id); status == 'active' required
   - topic_index = get_next_topic_index(intern)
   - existing = get_marathon_content(chat_id, topic_index)
   - if existing.status == 'pending' → skip (уже есть)
   - else → _generate_and_save_content() → marathon_content (status='pending')
   - on TimeoutError → _schedule_retry(chat_id, 'marathon')
   - on general Exception → лог ERROR, без retry (⚠️ см. ниже)
```

**⚠️ Асимметрия обработки ошибок:** `_schedule_retry` вызывается только при `asyncio.TimeoutError` и при `_generate_and_save_content` returning `False`. Generic `Exception` логируется (`[PreGen] Error for {chat_id}`), но retry НЕ планируется. Это узкое место: падение Claude API по не-timeout причине (5xx, RPC error) оставит пользователя без контента до следующего cron-tick через час. Фикс — отдельным коммитом: расширить `except Exception` на `_schedule_retry`.

**Зачем 3 часа:** buffer между генерацией и доставкой. Позволяет retry (30 + 60 минут) уложиться в окно. Если генерация успешна — контент лежит в БД со `status='pending'`, scheduler в момент доставки просто читает и шлёт.

**Concurrency:** `asyncio.Semaphore(20)` на цикл → до 20 параллельных user'ов. При bust от scheduler → burst в Gateway → см. §4 про rate-limiting.

### 1.2. `pregen_next_for_user()` — look-ahead после доставки

**Триггер:** вызывается из `states/workshops/marathon/lesson.py` и `task.py` после успешной доставки контента (fire-and-forget через `asyncio.create_task`).

**Алгоритм:**
```
1. available = get_available_topics(intern)
2. next_topics = [topic for idx, topic in available if idx > current_topic_index][:2]
3. Для первой ненайденной в marathon_content темы:
   - _generate_and_save_content() → marathon_content
   - break (генерируем только одну тему за вызов, не блокируем API)
```

**Зачем:** user может прийти раньше scheduled delivery (манально зашёл в бот). Look-ahead гарантирует, что next topic готов → мгновенный ответ при повторном входе.

**Правило:** look-ahead НЕ конкурирует с `pre_generate_upcoming`. Порядок: сначала cron (за 3ч), потом look-ahead (после доставки). Дубль предотвращается check `existing.lesson_content and len > 200`.

### 1.3. `_schedule_retry()` — exponential backoff при ошибках

**Триггер:** изнутри `pre_generate_upcoming` при падении или timeout.

**Параметры:**
```python
_RETRY_DELAYS_MINUTES = [30, 60]  # exponential backoff
```

**Алгоритм:**
```
1. if attempt >= len(_RETRY_DELAYS_MINUTES) → дропаем, лог WARNING
2. job_id = f"retry_{content_type}_{chat_id}"
3. if _scheduler.get_job(job_id) → уже pending, skip (dedup)
4. _scheduler.add_job('date', run_date=now+delay, replace_existing=True)
```

**Исполнение:** `_execute_retry(chat_id, content_type, attempt)` вызывает соответствующий send-method по `content_type`:
- `marathon` → `send_scheduled_topic`
- `feed` → `pre_generate_feed_digest`
- `tailor` → `deliver_tailor_lesson`

При повторном failure → `_schedule_retry(..., attempt + 1)` → `[30, 60]` → после 60 минут дропаем.

**Dedup:** `replace_existing=True` + проверка `_scheduler.get_job(job_id)` не дают множественных retry для одного chat_id+content_type.

---

## 2. Таблица `marathon_content` и статусы

См. [CLAUDE.md § 13 «marathon_content — семантика полей»](../../CLAUDE.md) для полной семантики. Краткая сводка для pre-gen:

| Поле | Значение | Кто ставит |
|------|----------|-----------|
| `status = 'pending'` | Контент сгенерирован, пользователь не открыл | pre-gen insert |
| `status = 'delivered'` | Пользователь открыл урок | `mark_content_delivered()` в `lesson.py` |
| `notification_sent_at` | Когда отправлено уведомление | `mark_notification_sent()` — log-before-send, см. [P-09](process-09-notification-idempotency.md) |

**Инвариант:** pre-gen пишет только `status='pending'`, никогда не ставит `delivered` и не трогает `notification_sent_at`. Это разделение владения: генерация ≠ доставка ≠ открытие.

---

## 3. Haiku Fallback при cache miss (§ 10.20)

Если scheduler не успел пре-генерировать (падение API, retry исчерпан, новый user пришёл мгновенно) — `lesson.py` / `task.py` используют **Haiku** вместо Sonnet:

```python
model=CLAUDE_MODEL_HAIKU  # 3-5s вместо 15-19s Sonnet
```

Правило:
- **Pre-gen scheduler** (cron + look-ahead) → всегда **Sonnet** (качественный happy path)
- **On-the-fly при cache miss** → **Haiku** (latency floor)

Worst case latency для user без pre-gen: <5s. См. [P-02 §7 Content Budget](process-02-content-generation.md) про оси генерации.

---

## 4. Burst и Cloudflare 429 (WP-209 Ф4)

### 4.1. Причина burst

`pre_generate_upcoming` с `Semaphore(20)` → до 20 пользователей параллельно → каждый `_generate_and_save_content` делает N MCP-вызовов через Gateway (context, knowledge, guides):

```
20 users × 2-3 MCP calls = 40-60 параллельных запросов в Gateway → Cloudflare WAF → 429
```

Эмпирика: **64× 429 в сутки** до фикса. Источник локализован через корреляцию scheduler ticks и 429 events.

### 4.2. Защита на стороне Gateway client

См. [P-10 §6](process-10-gateway-mcp.md):
- `_call_semaphore = asyncio.Semaphore(12)` — global на все исходящие запросы
- Retry-After parsing при 429
- Circuit breaker при 2 подряд failures

**Не трогать:** `Semaphore(20)` в `pre_generate_upcoming` остаётся — это ограничение user-fan-out. Защита от Cloudflare — на более низком уровне (Gateway client), чтобы не ломать разумную параллельность scheduler'а.

### 4.3. Мониторинг

`gateway_mcp._rate_limited_total` — счётчик 429 events, читается через dev-команду `/errors`. Утренняя проверка: 0× = норма. Если растёт — проверить новые источники burst (не только pre-gen).

---

## 5. Двойная защита идемпотентности

Генерация и доставка — два независимых слоя:

```
pre_generate_upcoming (cron)                 send_scheduled_topic (delivery)
  ↓                                             ↓
  marathon_content.status = 'pending'           notification_log INSERT
  (не дублируется — existing check)             ↓ UNIQUE violation → skip
                                               bot.send_message()
                                                ↓
                                               mark_content_delivered()
                                               marathon_content.status = 'delivered'
```

**Pre-gen dedup:**
- `existing = get_marathon_content(chat_id, topic_index)`
- `if existing and existing.status == 'pending' → return` (ничего не делаем)

**Delivery dedup** (см. [P-09 § 3-4](process-09-notification-idempotency.md)):
- `idempotency_key = f"marathon_lesson:{chat_id}:{date}:{topic_idx}"`
- `try_insert_notification()` → False при дубле

Даже если pre-gen выполнится дважды (race в scheduler) — контент не дублируется. Даже если delivery выполнится дважды (catch-up) — уведомление не дублируется.

---

## 6. Scheduler = read-only для user state (§10.10b)

**Критичное правило:** pre-gen **НЕ ИМЕЕТ ПРАВА** менять:
- `current_topic_index`
- `completed_topics`
- `bloom_level`

Эти поля — собственность FSM states (`lesson.py`, `question.py`, `task.py`). Pre-gen только читает состояние и генерирует контент в отдельную таблицу `marathon_content`. Запись в прогресс — только при реальном взаимодействии пользователя со стейтом.

**Почему:** если scheduler меняет `current_topic_index`, look-ahead и real delivery могут рассинхронизироваться → пользователь получит тему, которую ещё не проходил, или пропустит текущую.

---

## 7. Антипаттерны

- ❌ **Не использовать `_schedule_retry`** — падение в `pre_generate_upcoming` без retry → пользователь не получит контент
- ❌ **Ставить `status='delivered'` в pre-gen** — это собственность delivery path (`lesson.py` при открытии)
- ❌ **Менять `current_topic_index` из scheduler** — нарушение §10.10b, рассинхронизация с FSM
- ❌ **Подниматьs `Semaphore(20)` без замера** — увеличит burst в Gateway, больше 429
- ❌ **Не проверять `existing.lesson_content` в look-ahead** — дубль генерации, лишняя трата Claude API
- ❌ **Использовать Sonnet on-the-fly при cache miss** — 15-19s vs 3-5s Haiku → user страдает
- ❌ **Генерировать больше 1 темы за look-ahead вызов** — блокирует API и вымывает cache для других user'ов

---

## 8. Наблюдаемость

| Сигнал | Источник | Как читать |
|--------|----------|-----------|
| `[PreGen] Found N marathon users for HH:MM` | scheduler log | Видно ли plan: сколько user'ов обслуживается |
| `[PreGen] Content ready for {chat_id}` | scheduler log | Успех генерации |
| `[PreGen] Error for {chat_id}` | scheduler log + error_logs | Падение — retry запустится |
| `[LookAhead] Pre-generated topic N for {chat_id}` | scheduler log | Успех look-ahead |
| `[Scheduler] Retry #N scheduled for {chat_id}` | scheduler log | Запланирован retry |
| `[Scheduler] Max retries exhausted` | scheduler log (WARNING) | Дропаем — нужен manual trigger |
| `_rate_limited_total` | `/errors` dev-команда | 429 от Cloudflare — смотреть корреляцию со scheduler ticks |

Error classifier ([P-06](process-06-observability.md)) категоризирует ошибки pre-gen как `claude_api` / `mcp` / `scheduler` с severity L2-L3 — эскалация через `check_escalation` каждые 15 минут.

---

## 9. Связанные процессы

- **[P-02 Content Generation](process-02-content-generation.md)** — `_generate_and_save_content` вызывает `generate_content/question/practice_intro` (Content Budget Model)
- **[P-09 Notification Idempotency](process-09-notification-idempotency.md)** — `idempotency_key` для `marathon_lesson` при доставке
- **[P-10 Gateway MCP](process-10-gateway-mcp.md)** — источник 429 при burst, `Semaphore(12)` защищает от Cloudflare
- **[P-06 Observability](process-06-observability.md)** — error_classifier ловит `[PreGen] Error`, метрики p95 latency pre-gen
- **CLAUDE.md § 10.19, 10.20, 13** — первоисточник правил look-ahead, Haiku fallback, marathon_content семантики
