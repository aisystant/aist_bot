# P-10 Gateway MCP

> Единый клиент к Gateway MCP (`mcp.aisystant.com/mcp`), который проксирует к трём бэкендам (knowledge / dt / personal) через tool prefix. Per-user Ory Bearer token, per-user refresh lock от thundering herd, global semaphore от Cloudflare 429, circuit breaker. Это процесс — не инфраструктурный layer: здесь живут доменные решения про rate-limiting, auth race и graceful degradation.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Тип | Процесс (outbound integration с доменной логикой) |
| Источник | WP-209 Ф0 (клиент), Ф4 (Cloudflare 429), Ф5 (Ory refresh), WP-212 B4.13 (knowledge через Gateway), WP-5 #16 (tool discovery) |
| Файл | `clients/gateway_mcp.py` |
| Внешний endpoint | `mcp.aisystant.com/mcp` (JSON-RPC `tools/call` + `tools/list`) |
| Auth | Per-user Ory Bearer token, `Authorization: Bearer <access_token>` |
| Архитектурное решение | Заменяет прямые подключения к knowledge-mcp и digital-twin-mcp единой точкой |

---

## 1. Архитектурная граница

```
Бот                                 Gateway MCP                  Backends
┌─────────────────────┐             ┌──────────────┐             ┌──────────────────┐
│ gateway_mcp.py      │             │ mcp.aisystant│             │ knowledge-mcp    │
│  - _call            │  JSON-RPC   │    .com/mcp  │  prefix     │   knowledge_*    │
│  - token cache      │──tools/call │              │──routing    │ digital-twin-mcp │
│  - refresh lock     │  Bearer     │  Ory verify  │             │   dt_*           │
│  - semaphore(12)    │             │  userId RLS  │             │ personal-knowledge│
│  - circuit breaker  │             │              │             │   personal_*     │
└─────────────────────┘             └──────────────┘             └──────────────────┘
```

**Что делает бот:**
- Управляет per-user токенами (`_tokens`, загрузка из БД, refresh)
- Сериализует исходящие вызовы через `_call_semaphore`
- Держит circuit breaker на падения Gateway
- Выставляет wrapper-методы (`knowledge_search`, `dt_read`, `get_instructions`, ...)

**Что НЕ делает бот:**
- ❌ Не валидирует доступ (RLS) — это Gateway через Ory
- ❌ Не знает, какой бэкенд обслуживает запрос — знает только prefix
- ❌ Не хешит токены — передаёт Bearer как получил от Ory

**Что владеет бот:**
- ✅ Кеш per-user токенов в памяти (source-of-truth в БД: `ory_tokens` → WP-212)
- ✅ Политика rate-limiting (semaphore, circuit breaker)
- ✅ Fallback при недоступности Gateway (graceful degradation, return None)
- ✅ Кэш discovered tools (`_tools_cache`, TTL 15 мин, in-memory, DP.SC.129)

---

## 2. Tool prefix routing

Gateway определяет бэкенд по префиксу tool name:

| Prefix | Бэкенд | Wrapper-методы в клиенте |
|--------|--------|--------------------------|
| `knowledge_*` | knowledge-mcp (L2: Pack, guides, DS) | `knowledge_search`, `knowledge_get_document`, `knowledge_list_sources` |
| `dt_*` | digital-twin-mcp (ЦД пользователя) | `dt_read`, `dt_write`, `dt_describe` |
| `personal_*` | personal-knowledge-mcp (L4: личные заметки) | `personal_search`, `personal_write` |
| (без prefix) | unified | `search` (across all backends) |
| `get_instructions` | public tool | `get_instructions` (IWE platform) |
| `get_journey_state`, `get_next_onboarding_step`,<br>`grant_consent`, `request_equipment_upgrade`,<br>`get_onboarding_context` | onboarding-service (T0/T1 public) | прямой вызов через `_call`; `telegram_user_id` опционален (T0 — без токена) |

**Правило:** при добавлении нового tool — подставить wrapper над `_call(tool_name, args, telegram_user_id)`, выбрать правильный префикс, указать нужен ли `telegram_user_id` (для public tools — нет).

---

## 3. Per-user Ory Bearer token

### 3.1. Загрузка

`load_tokens_from_db()` — при старте бота читает `ory_tokens` из Neon в `self._tokens: Dict[int, dict]` (ключ = `telegram_user_id`). Каждый entry: `{access_token, refresh_token, expires_at, ory_id}`.

**Source-of-truth:** таблица `ory_tokens` в Neon. In-memory cache — оптимизация, при рестарте восстанавливается из БД.

### 3.2. Подключение нового user

`set_tokens(telegram_user_id, access_token, refresh_token, expires_at, ory_id)` вызывается:
- Из OAuth callback при первом подключении (WP-187)
- Из proactive refresh после успешного обновления

Параллельно пишет в БД (`save_ory_tokens`) — cache и БД синхронны.

### 3.3. Отключение

`disconnect(telegram_user_id)` очищает in-memory cache И удаляет из БД (`_delete_ory_tokens_from_db`). Вызывается только в контролируемых сценариях (см. §5.3).

### 3.4. `is_connected(telegram_user_id) → bool`

Проверяет наличие свежего токена в cache. Используется UI-слоем: показывать кнопку «Подключить ЦД» или «Мой двойник».

---

## 4. Proactive refresh (scheduler, каждые 10 мин)

`refresh_expiring_tokens(margin_seconds=600)` — cron в `core/scheduler.py`:

```
1. Snapshot candidates: user_id где expires_at < now + 10min AND refresh_token != None
2. Для каждого: await _refresh_single_token(user_id)
3. При неудаче: tokens.pop(user_id) + refresh_locks.pop(user_id)
   (proactive видит invalid_grant → refresh_token реально невалиден)
```

**Почему snapshot перед итерацией:** `self._tokens` может меняться во время refresh — reactive-путь добавляет/удаляет entries.

**Зачем margin 10 минут:** минимизирует окно 401 ошибок у пользователей. Токен обновляется заранее, до фактического истечения.

---

## 5. Reactive refresh (на 401) и thundering herd (WP-209 Ф5)

### 5.1. Проблема

При параллельных запросах одного user все получают 401 → каждый вызывает `_refresh_single_token` → Ory rotation инвалидирует старый `refresh_token` для всех корутин кроме первой → `invalid_grant` → раньше `disconnect()` сносил все токены.

Эмпирика: 8× `invalid_grant` за 55 секунд на одного user при bursting scheduler fan-out.

### 5.2. Решение — per-user lock + double-check

```python
lock = self._refresh_locks.setdefault(telegram_user_id, asyncio.Lock())
async with lock:
    # Double-check: пока ждали lock, другая корутина могла уже обновить
    if expires_at > datetime.utcnow() + timedelta(seconds=60):
        return True  # свежий токен
    # ... реальный POST в Ory ...
```

- `_refresh_locks: Dict[int, asyncio.Lock]` — lock на user
- Первый вызов делает реальный POST, остальные ждут
- После освобождения lock — double-check: if свежий, выходим без запроса

### 5.3. Правило: НЕ disconnect() в reactive пути

В `_do_call` при `refreshed=False`:
```python
self._last_call_error = "token_expired"
return None
# НЕ disconnect() !
```

Почему: raced thundering herd мог успешно обновить токен в другой корутине — `disconnect` всё снесёт. Окончательный `disconnect` — только в proactive cron после явного `invalid_grant` от Ory.

UX следствие: user видит ошибку `token_expired` → кнопка «Переподключить ЦД» (не «отключён навсегда»).

### 5.4. Defensive reload — load_one_ory_token (Block GTW, 2026-06-01)

**Когда вызывается:** внутри `_refresh_single_token`, перед попыткой POST в Ory, если `telegram_user_id` отсутствует в `self._tokens` (in-memory cache пустой).

**Зачем:** два сценария, когда cache пуст, но токен есть в БД:
1. **Перезапуск бота** — `load_tokens_from_db` при старте загружает все строки с `refresh_token IS NOT NULL`, но если строка появилась между стартом и первым запросом пользователя (race), её нет в cache.
2. **Race с OAuth-callback** — OAuth flow записывает токен в БД и обновляет cache, но если reactive путь стартовал на пике, он может проверить cache до обновления.

**Поведение:**
```python
# db/queries/ory_tokens.py:62
row = await load_one_ory_token(chat_id)
if row:
    self._tokens[telegram_user_id] = row  # repopulate cache
    logger.warning("Gateway: token cache miss, reloaded from DB for user %s", telegram_user_id)
    return True  # токен свежий — не делаем POST в Ory
```

**Что возвращает:** `{chat_id, access_token, refresh_token, expires_at, ory_id}` или `None` (пользователь не подключён к Gateway).

**Мониторинг:** `WARNING "Gateway: token cache miss"` в логах — нормально при единичных событиях. >10/час = аномалия (см. GTW6 в WP-7).

---

## 6. Cloudflare 429 rate limiting (WP-209 Ф4)

### 6.1. Проблема

До WP-212 B4.13 knowledge-запросы шли напрямую. После — все через Gateway → Cloudflare WAF лимитирует. Эмпирика: **64× 429 в сутки**, burst от scheduler pre-gen fan-out (см. [P-11 Pre-generation](process-11-pre-generation.md)): `Semaphore(20)` × N MCP-вызовов на user → 40-60 параллельных запросов в Gateway → WAF срабатывает.

### 6.2. Решение — global semaphore + Retry-After

```python
self._call_semaphore = asyncio.Semaphore(12)

async def _call(self, ...):
    async with self._call_semaphore:
        return await self._do_call(...)
```

- **12 concurrent** — компромисс: пропускает user-facing запросы, сглаживает scheduler fan-out
- Все вызовы `_call` проходят через semaphore — один раз для wrapper, не на каждом retry

При 429 ответе:
```python
retry_after_raw = resp.headers.get("Retry-After", "1")
retry_after = min(float(retry_after_raw), 5.0)  # cap 5s
self._rate_limited_total += 1
await asyncio.sleep(retry_after)
continue  # max 1 retry
```

### 6.3. Мониторинг

`self._rate_limited_total` — счётчик 429 events. Читается через dev-команду `/errors`. Мониторится утром: 0× за сутки = норма, всё что выше = регрессия.

---

## 7. Circuit breaker

| Параметр | Значение |
|----------|----------|
| `FAILURE_THRESHOLD` | 2 подряд ошибки |
| `RECOVERY_TIME` | 60 секунд |

```
OPEN   ← 2 failures     CLOSED ← success
   │                         ↑
   └── 60s → half-open ──────┘
```

**Что считается failure:** HTTP не-200/401/403/429, timeout, exception. НЕ считаются: 401 (token issue), 403 (subscription issue), 429 retry success, RPC error в JSON.

**Что делает open circuit:** `_call` возвращает `None` с `_last_call_error = "circuit_open"`, не делает HTTP request. Caller получает `None` → graceful fallback.

**Recovery:** после 60 секунд следующий вызов пропускается (`_is_circuit_open()` returns `False`), при успехе — `_record_success()` → `CLOSED`.

---

## 8. Error taxonomy (`_last_call_error`)

После каждого `_call` caller может прочитать `gateway_mcp._last_call_error` для точного UX:

| Код | Когда | UX показ |
|-----|-------|----------|
| `ok` | Успех | — |
| `circuit_open` | Circuit breaker открыт | «MCP временно недоступен» |
| `not_authorized` | Нужен telegram_user_id, но токенов нет | Кнопка «Подключить ЦД» |
| `token_expired` | 401 после failed refresh (без disconnect) | Кнопка «Переподключить» |
| `no_subscription` | 403 от Gateway (только для dt_*/personal_*/search/github_*; knowledge_* бесплатен) | Paywall |
| `rate_limited` | 429 после retry | «Много запросов, попробуйте позже» |
| `timeout` | `asyncio.TimeoutError` после retry | «Долгий ответ, повторите» |
| `http_error` | Другой HTTP / exception | Generic error |
| `rpc_error` | JSON-RPC error в ответе | Лог + generic error |

**⚠️ Несоответствие docstring и кода:** docstring `_last_call_error` (строка 95-96) перечисляет `"disconnected"` как один из кодов, но в реальности код его нигде не выставляет. Это артефакт ранней версии WP-209 Ф5 — сейчас disconnect перенесён в proactive cron, reactive путь использует `token_expired`. Callers, проверяющие `== "disconnected"`, не сработают. Фикс docstring — отдельным коммитом при следующем касании файла.

---

## 9. Trace correlation

Если есть активный trace (`core/tracing`), `_do_call` добавляет `x-trace-id` header. Gateway пробрасывает в бэкенды → unified trace через Neon/Langfuse. См. [P-06 Observability](process-06-observability.md) для Langfuse dual-write.

---

## 10. Singleton и session management

`gateway_mcp` — модульный singleton:
```python
gateway_mcp = GatewayMCPClient(url=GATEWAY_MCP_URL)
```

- **Session lazy:** `_get_session()` создаёт `aiohttp.ClientSession` при первом вызове, переиспользует connection pool
- **Shutdown:** `await gateway_mcp.close()` вызывается из bot.py shutdown hook
- **Impact:** один session на процесс — keep-alive соединения, без overhead на handshake

---

## 11. Публичные wrapper-методы

### 11.1. Knowledge (L2 — Pack, guides, DS)

- `knowledge_search(query, limit=5, telegram_user_id)` — полнотекстовый поиск; используется в [P-08 pre-search](process-08-self-knowledge.md)
- `knowledge_get_document(filename, telegram_user_id)` — получить документ по имени
- `knowledge_list_sources(source_type, telegram_user_id)` — список доступных источников

### 11.2. Digital Twin (ЦД)

- `dt_read(path, telegram_user_id)` — читать секцию ЦД (напр. `"1_declarative"`)
- `dt_write(path, data, telegram_user_id)` — записать в ЦД (deep merge со стороны бэкенда)
- `dt_describe(path, telegram_user_id=None)` — описание структуры (public-ish)

### 11.3. Personal (L4 — личные заметки)

- `personal_search(query, telegram_user_id)` — поиск по личным знаниям user
- `personal_write(source, path, content, telegram_user_id)` — запись в personal-knowledge-mcp

### 11.4. Composite

- `search(query, telegram_user_id=None)` — unified поиск по всем бэкендам
- `get_instructions(telegram_user_id=None)` — IWE platform instructions (public tool, token не обязателен)
- `read(path, telegram_user_id)` — алиас (роутит по префиксу path)
- `write(path, data, telegram_user_id)` — алиас

### 11.5. Sync helpers

- `get_user_profile(telegram_user_id)` — профиль из ЦД, используется `/twin`, `/me`, insights
- `get_user_profile_ex(telegram_user_id)` — расширенная версия (tuple с доп. данными)
- `sync_profile(telegram_user_id, intern_data)` — массовая синхронизация полей профиля (bot DB → ЦД)
- `sync_fields(telegram_user_id, fields)` — гранулярная синхронизация конкретных полей
- `get_connected_user_ids()` — список user IDs с активным токеном

### 11.6. Онбординг — Journey API (WP-349 Ф28, gateway-mcp a378127)

> **Уровень доступа:** T0/T1 (публичные, token не обязателен для T0). Реализованы в gateway-mcp.
> **Используются:** `/setup`, `handlers/onboarding.py`, `handlers/hermes.py` (бот → проекция, Ф30 WP-349).

- `get_journey_state(telegram_user_id=None)` → `JourneyState` — обе координаты пути пилота:
  - **Технологическая:** тир T0-T4 (Ory sub? подписка? ЦД? managed-репо? GitHub?)
  - **Содержательная:** cp_stage (ступень 1-5), bottleneck, has_diagnosis, программа
  - Источники: `learning.onboarding_state` + `learning.cp_assessments` + контракт подписки
  - Режим отказа: если Neon недоступен → вернуть T0-состояние с флагом `degraded=true`

- `get_next_onboarding_step(telegram_user_id=None, channel_hint=None)` → `OnboardingStep` — один приоритетный шаг:
  - Эвристика v2: ступень неизвестна → диагностика; tier < мин-тир-программы → оснащение; иначе → контент
  - `channel_hint`: `"bot"` | `"browser"` | `"vscode"` — для подбора CTA-текста канала
  - Возвращает: `{step_type, message_key, cta_label, cta_action, coordinate}`

- `grant_consent(telegram_user_id, scope, granted, interface)` — запись consent из любого канала:
  - UPSERT `consent_grant` (scoped), COALESCE opted_at
  - `interface`: `"telegram"` | `"browser"` | `"vscode"` — аудитовый след
  - Free tool (T0/T1, без проверки подписки)

- `request_equipment_upgrade(telegram_user_id, target_tier)` — инициировать апгрейд оснащения:
  - T1→T2: вернуть ссылку оплаты; T2→T3: вызвать `create_repository` managed; T3→T4: вызвать `github_connect`
  - Заменяет бот-only tier_upgrade.py CTA (используется из любого MCP-клиента)

- `get_onboarding_context(telegram_user_id=None)` → `OnboardingContext` — полный снимок онбординг-контекста пилота:
  - Объединяет `JourneyState` + историю шагов + активные consent + `program_hint`
  - Предназначен для Портного (DP.ROLE.030) и Герменевта — отдаёт всё за один вызов вместо N отдельных
  - **Нет обёртки в `gateway_mcp.py`** (добавить отдельной задачей): вызывается напрямую через `_call("get_onboarding_context", {...})`
  - Режим отказа: degraded-флаг при недоступности Neon

**Добавление в tool prefix table** (§2): все пять инструментов — без prefix (public tools, как `get_instructions`). Backend: onboarding-service. `telegram_user_id` опционален (T0 не требует токена).

### 11.7. Tool discovery (DP.SC.129, DP.ROLE.038)

- `list_tools()` — загружает `tools/list` с Gateway, кэш 15 мин (`TOOLS_CACHE_TTL`), fallback на stale-кэш при ошибке. Bootstrap-вызов в `bot.py` при старте процесса.
- `get_discovered_tools()` / `is_tools_cache_fresh()` — читатели кэша, используются `consultation_tools.get_tools_for_tier()` для объединения захардкоженных tool с найденными.
- **Б.x tool-descriptor validation** (ArchGate 2026-07-07): `_mcp_to_anthropic_tool()` отбрасывает (не санитизирует) tool целиком, если description содержит role-break/delimiter маркер (bilingual, ru+en) или превышает 1024 символа. Fail-secure — компрометированное описание значит остальной descriptor тоже не доверяем.
- **Л2.2 tool-call audit** (ArchGate 2026-07-07): `db.queries.traces.log_tool_call_audit()` пишет в `domain_event` (`event_type='tool_call_audit'`) query + снапшот доступных tool + выбранный tool + результат при каждом вызове через `tool_executor` (`question_handler.py`), fire-and-forget. Разблокирует расследование регресса точности выбора tool — discovery убирает единственный раньше существовавший сигнал (deploy-корреляция).
- Источник решения: `DS-my-strategy/sessions/2026-07/2026-07-07-09-archgate-mcp-tool-discovery/report.md` (охват ArchGate) + `2026-07-07-13-mcp-tool-discovery-impl/report.md` (реализация).

---

## 12. Антипаттерны

- ❌ **Обходить semaphore** — делать HTTP напрямую, минуя `_call`. Ломает rate-limiting → Cloudflare 429
- ❌ **Disconnect в reactive пути** при 401 — теряются токены при thundering herd
- ❌ **Хранить токены вне `_tokens`** — теряется синхронизация с refresh lock
- ❌ **Полагаться на кеш после рестарта** — source-of-truth в БД, in-memory — производное
- ❌ **Игнорировать `_last_call_error`** — caller должен реагировать на `not_authorized`, `no_subscription`, `token_expired` разным UX
- ❌ **Увеличивать semaphore без замера** — 12 выбрано эмпирически на burst scheduler'а, при изменении нужен замер 429

---

## 13. Связанные процессы

- **[P-08 Self-knowledge](process-08-self-knowledge.md)** — `collect_pre_search` использует `knowledge_search`, `collect_iwe_instructions` использует `get_instructions`
- **[P-06 Observability](process-06-observability.md)** — error_classifier категоризирует Gateway ошибки как `mcp`/`claude_api`, trace correlation через `x-trace-id`
- **P-11 Pre-generation** (TODO) — источник burst 429, из-за которого введён semaphore
- **WP-212 B4.13** — архитектурное решение: все knowledge-запросы через Gateway (не прямо в knowledge-mcp)
- **WP-187** — OAuth flow, source для `set_tokens` через callback
