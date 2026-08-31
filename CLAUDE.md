# CLAUDE.md — AIST_me_bot (new-architecture)

> **Общие инструкции:** см. [`/Users/tserentserenov/IWE/CLAUDE.md`](../../CLAUDE.md) (загружается Claude Code автоматически как parent).
> Этот файл содержит только специфику данного репозитория.
>
> **Last sync review:** 2026-04-26 (WP-265 audit). Универсальные правила root § 2 применимы и здесь: **WP Gate, Pilot-First Push, IntegrationGate, LegacyPortGate, Автономность (БЛОКИРУЮЩЕЕ — не спрашивать подтверждений)**. См. также Pilot-First в `MEMORY.md`.

---

## 1. Тип репозитория

**DS/instrument** — Telegram-бот марафона личного развития.

**НЕ является source-of-truth** — определения в Pack'ах.

**Ветки:** `pilot` (разработка) → `new-architecture` (прод). Правило Pilot-First — см. MEMORY.md.

**Хуки:** `.githooks/pre-push` блокирует push в `new-architecture` без `FORCE_PROD=1`. `.githooks/pre-commit` проверяет docs-sync (warning-only). После clone: `git config core.hooksPath .githooks`.

---

## 2. Терминология

**ВСЕГДА используй термины из [ontology.md](ontology.md).**

### 2.1. Краткая справка

| Термин | Что это |
|--------|---------|
| **Участник** | Пользователь Марафона |
| **Читатель** | Пользователь Ленты |
| **Марафон** | 14-дневная программа |
| **Лента** | Гибкое развитие по дайджестам |
| **Занятие** | Теория в Марафоне |
| **Задание** | Практика в Марафоне |
| **Дайджест** | Ежедневный материал в Ленте |
| **Фиксация** | Личный вывод Читателя |

### 2.2. Соответствие кода и терминов

| Термин | В коде сейчас | Целевое имя |
|--------|---------------|-------------|
| Занятие | `theory` | `lesson` |
| Задание | `practice` | `task` |
| Дайджест | `feed_session` | `digest` |

---

## 3. Структура проекта (модульная архитектура)

```
aist_bot/
├── bot.py                    # Тонкий клиент (~246 строк): config + imports + main()
├── core/
│   ├── machine.py            # Движок State Machine (без изменений)
│   ├── dispatcher.py         # Центральный роутинг (mode-aware /learn)
│   ├── topics.py             # Доменная логика тем: TOPICS, get_marathon_day, save_answer
│   ├── scheduler.py          # Планировщик отправки тем
│   ├── storage.py            # PostgresStorage (FSM persistence)
│   ├── middleware.py          # LoggingMiddleware
│   ├── helpers.py            # MODE_STATE_MAP, get_user_mode_state
│   ├── intent.py             # Определение намерения пользователя
│   └── knowledge.py          # Поиск по MCP
├── handlers/
│   ├── __init__.py           # setup_handlers(dp, dispatcher), setup_fallback(dp)
│   ├── commands.py           # Тонкие обёртки: /learn, /feed, /mode → dispatcher
│   ├── callbacks.py          # Callback queries → dispatcher
│   ├── onboarding.py         # /start + OnboardingStates
│   ├── settings.py           # /profile, /help, /update, /language + UpdateStates
│   ├── progress.py           # /progress + full report
│   ├── linear.py             # /linear интеграция
│   ├── twin.py               # /twin цифровой двойник
│   └── fallback.py           # Catch-all: SM routing
├── states/                   # State Machine стейты
│   ├── common/               # start, mode_select, settings
│   ├── workshops/marathon/   # lesson, question, bonus, task
│   ├── feed/                 # topics, digest
│   └── utilities/            # progress
├── engines/                  # Режимы (mode_selector, feed)
├── config/
│   ├── settings.py           # Все константы
│   └── transitions.yaml      # Таблица переходов SM
├── clients/                  # Claude API, MCP клиенты
├── db/                       # PostgreSQL queries
├── i18n/                     # Локализация
├── integrations/telegram/    # Клавиатуры
└── docs/                     # Документация
```

### 3.1. Правила архитектуры

**Порядок роутеров (критично!):** engines → handlers → fallback ПОСЛЕДНИМ.

**Импорты — откуда что брать:**

| Что нужно | Откуда | НЕ из |
|-----------|--------|-------|
| Доменные функции (get_marathon_day, TOPICS, save_answer) | `core.topics` | ~~bot~~ |
| Константы (BLOOM_AUTO_UPGRADE_AFTER, STUDY_DURATIONS) | `config` | ~~bot~~ |
| Клавиатуры (kb_update_profile) | `integrations.telegram.keyboards` | ~~bot~~ |
| FSM стейты (UpdateStates) | `handlers.settings` | ~~bot~~ |
| `claude`, `state_machine` | `bot` | Единственные легитимные импорты из bot.py |

**Lazy imports (`_bot_imports()`)** — используются в handlers/ для разрыва circular dependencies. Внутри функций, не на уровне модуля.

**bot.py — re-exports:** bot.py импортирует всё из core/topics, handlers/ для обратной совместимости. Новый код должен импортировать из правильного источника напрямую.

---

## 4. Уровни документации

### 4.1. Три категории

| Категория | Описывает | Папка |
|-----------|-----------|-------|
| **Сценарий** | Взаимодействие с ботом (пользовательское) | `docs/scenarios/` |
| **Процесс** | Внутренняя логика (наблюдаема как процесс, не как экран) | `docs/processes/` |
| **Данные** | Структура БД, метрики | `docs/data/` |

### 4.2. Сценарии — четыре вида команд

Slash-команды делятся на 4 вида. Вид определяет, куда кладётся документация.

| Вид | Критерий | Где лежит | Формат |
|-----|---------|-----------|--------|
| **A. Основные** | Центральный обучающий режим (Марафон, Лента, Тренировка) | `docs/scenarios/01-основные/` | `scenario-01-NN-<name>.md` |
| **B. Вспомогательные** | Многошаговый поток (FSM, wizard, несколько экранов) | `docs/scenarios/02-вспомогательные/` | `scenario-02-NN-<name>.md` |
| **C. Микро** | Одна команда — один экран (показ / переключение / маленькое действие) | `docs/scenarios/03-микро/` | `scenario-03-NN-<name>.md` |
| **D. Dev** | TD1-only, внутренняя отладка, не для пользователей | `docs/dev-commands.md` (единый файл) | Раздел по команде |
| **Admin** | Только для администраторов бота (не TD1) | `docs/admin-commands.md` (единый файл) | Раздел по команде |

**Граница B↔C:** если команда запускает FSM-состояния или ведёт через 2+ экранов подряд — вид B. Если отвечает одним сообщением — вид C. Пример: `/start` → B (онбординг), `/twin` → C (показ dashboard).

### 4.3. Соответствие кода и документации

| Изменение кода | Категория документации |
|----------------|------------------------|
| `handlers/*.py` (пользовательские) | Сценарий (вид A/B/C) |
| `handlers/dev.py` | `docs/dev-commands.md` |
| `states/*.py`, FSM | Сценарий (обычно B) |
| `core/*.py`, `engines/*.py` | Процесс |
| `db/models.py`, миграции | Данные |
| `db/queries/*.py` | Процесс или Данные (в зависимости от сути) |

---

## 5. Правила разработки

### 5.1. При изменении кода — СПРОСИ

**Любое изменение кода требует:**
1. Определить категорию (Сценарий [вид A/B/C/D/Admin] / Процесс / Данные) по §4.3
2. Если Сценарий — определить вид по §4.2 (граница B↔C)
3. Спросить: "Это изменение затронет [категория/вид]. Подтвердите?"
4. Дождаться подтверждения
5. Код + документация в одном коммите

**Новая slash-команда:** всегда создаётся новый файл документации (или раздел для вида D/Admin).

### 5.2. README.md

Изменять **только с явного согласия пользователя**.

---

## 6. Работа с ветками

| Ветка | Назначение |
|-------|------------|
| `main` | Production |
| `new-architecture` | State Machine (эта ветка) |

### Синхронизация с main

- Периодически делать `rebase` на main
- Не мержить в main до полной готовности
- Новые модули изолированы за feature flags

### Критерии готовности к мержу

- [ ] Все стейты реализованы
- [ ] Feature flag работает
- [ ] Smoke test проходит
- [ ] E2E тесты в Telegram пройдены

---

## 7. Чеклист перед коммитом

### Терминология
- [ ] Термины соответствуют `ontology.md`
- [ ] Сообщения пользователю на русском
- [ ] Код использует английские имена

### Документация
- [ ] Пользователь подтвердил изменения
- [ ] Затронутые Сценарии обновлены
- [ ] Затронутые Процессы обновлены
- [ ] Затронутые Данные обновлены

---

## 8. State Machine — правила

- **enter()** ДОЛЖЕН возвращать строку-событие во ВСЕХ ветках выхода. `return` без значения = стейт застревает.
- Каждое возвращаемое событие ДОЛЖНО быть определено в `config/transitions.yaml`.
- **handle()** НЕ должен возвращать событие на любой произвольный текст — проверяй ожидаемый ввод.
- **go_to() silent return:** проверяй только явный `context`, не `full_context` (с exit_context). Иначе exit() модального стейта (consultation_complete) блокирует команды (/learn и др.) при выходе из модального стейта.
- **go_to() + _previous:** при входе в модальный стейт через go_to() — сохраняй previous_state. Иначе _previous при возврате из callback-вызова (✏️/🔍) использует устаревшее значение.

---

## 9. Claude API — правила

- **Streaming SSE** (`_api_call_streaming`) для `generate()`. Non-streaming (`_api_call`) — только для `generate_with_tools()`.
- **Inactivity timeout** вместо total: `sock_read = max(15, max_tokens / 200)`. Total timeout (45s) не масштабируется с длиной вывода.
- **Adaptive max_tokens** в `generate_content`: `min(words × 4.5, 8192)`. Haiku вербознее Sonnet — нужен 50% буфер. Не hardcode. При `stop_reason=max_tokens` + `allow_partial=False` → `None` → Sonnet fallback.
- **Force-text fallback** в `generate_with_tools()`: при исчерпании `max_tool_rounds` без текстового ответа — финальный запрос БЕЗ tools (контекст из tool_use уже в conversation). Гарантирует ответ вместо `None`. Не увеличивает latency на happy path.
- **Scheduler retry**: при фейле пре-генерации → `_schedule_retry()` ставит one-off job на +30 мин (APScheduler `date` trigger, dedup по job_id).
- **Content Budget Model (DP.D.027)** — 3 независимые оси генерации контента:
  - **Ось 1 (Длина):** `words = duration × WPM_BASE(60) × BLOOM_MULTIPLIER[bloom]` (множители: 1→1.0, 2→1.3, 3→1.7). Функция: `config.calc_words()`.
  - **Ось 2 (Глубина):** `BLOOM_INSTRUCTION[bloom]` — отдельная инструкция в system prompt, управляющая стилем (доступный → профессиональный → экспертный).
  - **Ось 3 (Персонализация):** профиль пользователя + assessment state + DT (tier context).
  - **Правило:** Длина и глубина НЕ ДОЛЖНЫ смешиваться. Bloom влияет на объём через множитель (Ось 1) И на стиль через инструкцию (Ось 2) — раздельно.

---

## 10. Частые ошибки

| Неправильно | Правильно |
|-------------|-----------|
| "контент" (в Ленте) | Дайджест |
| "тема" | Занятие / Задание / Дайджест |
| "сессия" | Дайджест / День |
| "рефлексия" | Фиксация |
| "пользователь" | Участник / Читатель |

### Anti-hallucination в промптах консультанта

1. **Граница знаний = правило #1** (question_handler.py). Инструкция «НЕ додумывай» должна быть первой, не третьей — иначе конкурирует с другими правилами.
2. **Depth instruction → «используй ВСЕ из контекста»**, а НЕ «объясни механизмы, приведи примеры» — второе провоцирует генерацию из параметрической памяти.
3. **Structured data > MCP** для точных ответов: `previous_days_connection` из topic YAML → `format_structured_context()` → модель не выдумывает связи.
4. **get_bot_info tool bloat** (WP-7 W12): полный self-knowledge (~4000 chars со списком сценариев) → Claude цитирует команды вместо ответа. Fix: compact output (identity + FAQ, без сценариев) + description tool запрещает использование для предметных вопросов.
5. **Пустой профиль → галлюцинация** (WP-7 W12): `_build_user_profile()` при пустых данных ОБЯЗАН возвращать явный текст «профиль не заполнен, НЕ выдумывай». Пустая строка → Claude выдумывает.
6. **Нулевые метрики ≠ отсутствие** (WP-139): Если данные = 0 (fallback из-за отсутствия коллектора), промпт ОБЯЗАН содержать инструкцию «скажи что данные не подключены, НЕ угадывай». Иначе LLM выводит числа из косвенных метрик (git activity → WP count). Затронуто: `twin.py` — оба промпта insights.

---

## 10. Ловушки i18n и UI

### 10.1. Markdown-краш при отсутствии ключа

`t()` при отсутствии ключа возвращает строку ключа (напр. `"help.about_marathon"`).
Если в ней `_` — Telegram интерпретирует как курсив → `TelegramBadRequest: can't parse entities`.

**Правило:** при добавлении нового `t()` вызова — убедись, что ключ существует в schema.yaml + es.yaml + fr.yaml.

### 10.2. Markdown fallback для Claude-контента

При отправке LLM-генерированного текста — **всегда** передавай `parse_mode="Markdown"`. SafeBot перехватывает вызов только при наличии этого параметра: без него `**text**` отображается буквально (звёздочки видны пользователю). SafeBot конвертирует через `md_to_html()` и ставит `parse_mode="HTML"`.

Дополнительно — **всегда** оборачивай в `try/except` с fallback без форматирования. Claude может генерировать незакрытые сущности (`*`, `_`, `[`), которые ломают Telegram API (`TelegramBadRequest: can't parse entities`).

### 10.3. State не должен модифицировать DB-указатель чужого типа контента

Lesson state (theory) **не должен** менять `current_topic_index` в БД, перешагивая practice-темы. Если current topic — practice, lesson маршрутизирует на task state через `return "already_completed"`. Иначе work products сохраняются под неправильным topic_index и пропадают из прогресса.

### 10.4. Заметки (fleeting-notes.md)

Формат заметок и логика вставки в `clients/github_api.py` должны соответствовать структуре `fleeting-notes.md`.
При изменении структуры файла (шапка, описание, разделители) — обновить `_find_insert_position`.

### 10.5. GitHub Integration

- `GITHUB_BOT_PAT` — только для AutoFix (org `aisystant/aist_bot`). Fine-grained PAT = single owner.
- Publisher (R21) — per-user OAuth tokens из `github_connections.knowledge_repo`. Singleton deprecated.
- Добавление нового repo-поля: DB migration → queries → OAuth cache → Settings UI → consumers.
- **default_branch:** НЕ хардкодить `"main"`. `set_target_repo()` определяет ветку через `GET /repos/{owner}/{repo}` → `default_branch`. Хранится в `github_connections.default_branch`. Retry заметок ограничен 3 попытками (`_MAX_RETRIES`). Аналогично для strategy_repo: `strategy_default_branch` + lazy backfill в `get_strategy_default_branch()` (если дефолт `"main"` и repo задан → API → обновить БД).

### 10.6. Naive datetime для TIMESTAMP колонок

**Правило:** Все колонки в DB (кроме `error_logs` и `request_traces`) используют `TIMESTAMP` (naive). При записи — только `datetime.utcnow()`, **НЕ** `datetime.now(timezone.utc)`. asyncpg с `statement_cache_size=0` (Neon) не может кодировать aware datetime в naive колонку → `DataError`.

### 10.7. Keyboard Management Policy (WP-52)

**Принцип:** SM НЕ удаляет ReplyKeyboard, а ЗАМЕНЯЕТ. Tier-based KB из mode_select персистит через inline-стейты. SM-contextual стейты (Phase 2) заменят её на контекстную.

**Два слоя ReplyKeyboard:**
1. **mode_select KB** (2×2) — tier-based, отправляется при входе в mode_select
2. **SM-contextual KB** (Phase 2) — Row 1: действия стейта, Row 2: `[🏠 Меню] [⚙️]`

**Keyboard Registry (19 стейтов):**

| State | keyboard_type | Кнопки |
|-------|:---:|---|
| common.start | `none` | Текстовый онбординг |
| common.mode_select | **`reply`** | Tier-based 2×2 ReplyKeyboard (tier_ui.py) |
| common.settings | `inline` | Настройки (edit_text sub-nav) |
| common.profile | `inline` | Профиль (edit_text sub-nav) |
| common.consultation | `none` | Модальный, inline-фидбек |
| common.plans | `inline` | Day/Week план (edit_text) |
| common.error | **`reply`** | Повторить / Назад |
| workshop.marathon.lesson | `inline` | Retry / Back (автопереход) |
| workshop.marathon.question | **`reply`** | Пропустить тему |
| workshop.marathon.bonus | **`reply`** | Да / Достаточно |
| workshop.marathon.task | **`reply`** | Пропустить практику |
| workshop.assessment.flow | `inline` | Да/Нет, self-check (edit_text) |
| workshop.assessment.result | `inline` | Марафон / Настройки / Назад |
| feed.topics | `inline` | Чекбоксы тем |
| feed.digest | `inline` | Подробнее / Фиксация / Назад |
| utility.progress | `inline` | 6 секций (edit_text hub) |
| utility.mydata | `inline` | Hub (5 секций) + delete confirm (text input) |
| utility.feedback | `inline` | Баг/Предложение → severity |

**SM keyboard persistence:** SM больше не удаляет ReplyKeyboard автоматически. `_pending_keyboard_cleanup` в base.py оставлен для backwards compat но не заполняется. ReplyKeyboard из mode_select персистит через все inline-стейты. Reply-стейты (question, bonus, task) заменяют tier-KB на свою; при возврате в mode_select tier-KB восстанавливается.

**Правила:**

1. **Reply-стейт**: `keyboard_type = "reply"` + на каждом exit-пути `send_remove_keyboard()` (очистка своей KB перед возвратом в mode_select).
2. **Callback-переход**: handler ОБЯЗАН вызвать `callback.message.edit_reply_markup()` перед `go_to()`.
3. **Inline sub-навигация**: `edit_text()` — клавиатура заменяется, stale кнопок нет.
4. **Stale inline кнопки**: допустимы. Fallback handler → `fsm.button_expired` toast.
5. **Новый стейт**: установи `keyboard_type`, обнови эту таблицу.
6. **keyboards.py**: shared-билдеры → сюда. Контекстные inline (1-2 кнопки) — допустимо inline.

**Процесс:** см. `PROCESSES.md § 5. Keyboard Lifecycle`.

### 10.6. Publisher: cancel = revert frontmatter + slot uniqueness

При отмене запланированной публикации (cancel) — **ОБЯЗАТЕЛЬНО** revert frontmatter `status → draft` через GitHub API. Иначе `_smart_publisher_scan` (05:07 МСК) повторно обнаружит `status: ready` и пере-запланирует.

Слоты генерируются с проверкой `occupied_dates` — одна дата = один пост. Startup scan вызывает `_smart_publisher_scan(notify=False)` — только scheduling, без queue-watch уведомлений.

**Итоги недели** (`итоги-недели` тег) публикуются **сразу** (schedule_time = utcnow+1min → ближайший цикл :07/:37).
**Интервал:** `PUBLISHER_INTERVAL` (env, default=2) — минимум N дней между обычными публикациями. Влияет на smart_publisher_scan и reschedule_all_pending.

### 10.7. CJK-строки: outer single quotes

Fullwidth quotes `"..."` (U+201C/U+201D) внутри Python `"..."` → `SyntaxError`. CJK-контент оборачивать в single quotes: `'来自"个人发展"项目的主题。'`.

### 10.7. Эмодзи: только в i18n, не в коде

Если перевод в schema.yaml уже содержит эмодзи (`"⏳ Генерирую..."`, `"🔍 Ищу..."`), **не добавляй** эмодзи в Python-коде (`f"⏳ {t(key)}"`). Результат — двойная эмодзи. Правило: эмодзи в UI → только в schema.yaml.

### 10.8. YAML schema.yaml: запрет дублирования top-level ключей

`yaml.safe_load()` при дублировании top-level ключа молча затирает первый → ключи пропадают → `t()` возвращает сырые ключи. Проверяй: `grep -n '^[a-z_]*:$' schema.yaml | sort | uniq -d` должен возвращать пустой результат. Аналогично для `translations/*.yaml`.

### 10.9. Back в inline sub-навигации: delete + enter

Кнопка "Назад" из подменю (edit_text) НЕ должна вызывать голый `self.enter(user)` — это отправляет НОВОЕ сообщение, оставляя старое. Паттерн: `callback.message.delete()` → `self.enter(user)`.

### 10.10. Scheduler notifications: log-before-send + dedup

**Правило idempotent notifications:** При отправке любого уведомления из scheduler — СНАЧАЛА записать факт отправки в БД (`log_conversion_event`, `log_nudge_sent`, `UPDATE sent=TRUE`), ПОТОМ `send_message`. Иначе: send OK → log fail → next run отправит дубль.

**Правило dedup:** Если scheduler обрабатывает пользователей в цикле с несколькими итерациями (напр. `for days_ahead in [1, 0]`) — вести `sent_chat_ids: set()` для исключения дублей на стыке условий.

**Правило concurrent access:** Для таблиц с `sent=FALSE` (reminders) — использовать `UPDATE...RETURNING` + `FOR UPDATE SKIP LOCKED`, а не SELECT+loop+UPDATE. Scheduler запускается каждую минуту, предыдущий цикл может ещё обрабатывать.

**Правило status-before-message:** При завершении марафона (или аналогичном изменении статуса) — `update_intern(status=COMPLETED)` ДО `send_message(поздравление)`. Иначе catch-up (каждые 30 мин) найдёт user с active status и отправит повторно.

### 10.10c. При удалении функции из scheduler.py — сразу убрать add_job вызов

При удалении любой `async def _fn()` из `core/scheduler.py` — немедленно найти и удалить `_scheduler.add_job(_fn, ...)` в блоке `init_scheduler`. Иначе бот падает при старте с `NameError` (инцидент 2026-06-09: три функции подряд — `_ensure_reminder_text_column`, `_gateway_proactive_refresh`, `_notify_github_relink`).

Быстрая проверка перед push: `python3 -c "import re,sys; code=open('core/scheduler.py').read(); calls=set(re.findall(r'_scheduler\.add_job\((\w+),', code)); defs=set(re.findall(r'^(?:async )?def (\w+)', code, re.M)); missing=calls-defs; print('MISSING:', missing) if missing else print('OK')"`.

### 10.10b. Scheduler = read-only для user state

Scheduler (`core/scheduler.py`) **НЕ ИМЕЕТ ПРАВА** менять поля прогресса пользователя: `current_topic_index`, `completed_topics`, `bloom_level`. Эти поля — собственность FSM states (lesson/question/task). Scheduler может читать состояние и генерировать контент, но запись в прогресс — только при реальном взаимодействии.

### 10.11. Marathon day — только `core.topics.get_marathon_day(intern)`

SM states **ОБЯЗАНЫ** использовать `core.topics.get_marathon_day(intern)` для расчёта дня марафона. Нельзя реализовывать свою версию — поле `marathon_start_date` + Moscow TZ (МСК) обязательны. Своя реализация использовала `marathon_started_at` + UTC → расхождение на 1 день.

### 10.12. dict.get() с None-значением в JSONB

`session.get('content', {})` возвращает `None` (не `{}`), когда ключ `content` существует со значением `None`. **Паттерн:** `session.get('content') or {}`. Всегда использовать `or {}` / `or []` при доступе к JSONB-полям, которые могут быть `None`.

### 10.13. delete_webhook при старте (Railway)

При редеплое Railway старый инстанс ещё polling, а новый уже стартует → `TelegramConflictError`. **Обязательно:** `await bot.delete_webhook(drop_pending_updates=False)` перед `dp.start_polling(bot)`.

### 10.14. Model Routing — Haiku для простых, Sonnet для сложных

Два Claude-модели: `CLAUDE_MODEL_SONNET` (default) и `CLAUDE_MODEL_HAIKU` (config/settings.py). Роутинг по сложности задачи:

| Модель | Когда | Почему |
|--------|-------|--------|
| **Haiku** | feed «why» (planner.py), /mydata объяснения | Структурированный вывод, latency <3с, стоимость ×10 ниже |
| **Sonnet** | Занятия, практика, консультации (L3 + tool_use), /twin insights | Креативный/сложный вывод, нужен reasoning, следование FORBIDDEN-правилам |

> **Unified L3 (2026-02-28):** L2 bot-question path удалён. Все вопросы идут через единый L3 путь (tool_use). LLM сам выбирает tool: `search_knowledge`, `search_guides`, `get_bot_info`. Keyword classifier `_BOT_KEYWORDS` удалён — вызывал ложные срабатывания на доменных вопросах.

**Паттерн:** все `generate*()` методы принимают `model=` параметр. По умолчанию Sonnet. Вызывающий код передаёт `model=CLAUDE_MODEL_HAIKU` явно для простых задач.

### 10.15. Content Cache — DB-кеш для practice intro и questions

`db/queries/cache.py` — кеш сгенерированного контента в таблице `content_cache`. TTL = 7 дней.

| Ключ | Формат | Где используется |
|------|--------|------------------|
| `practice:{topic_id}:{lang}:{chat_id}` | Практическое введение (per-user) | `generate_practice_intro()` |
| `question:{topic_id}:{bloom}:{lang}:{occupation}` | Вопросы | `generate_question()` |

**Правило:** Персонализированный контент (содержит имя, профессию, цели пользователя) ОБЯЗАН кэшироваться per-user (`:{chat_id}`). Глобальный кэш допустим только для контента без персонализации.

`cache_cleanup()` запускается из scheduler ежедневно.

### 10.16. Slot suggestion при перегрузке (≥50 users)

`MAX_USERS_PER_SLOT = 50` (db/queries/users.py). При выборе времени доставки (onboarding + settings):
1. `get_slot_load(time)` → считает пользователей в окне ±5 мин (11 слотов)
2. Если count ≥ 50 → `kb_slot_suggestions()` показывает до 3 🟢 свободных слотов + 🟡 «оставить как есть»
3. Это **мягкое** ограничение — пользователь может настоять на перегруженном слоте

**Зачем:** рассредоточение нагрузки на scheduler pre-generation. Без staggering — все 50 users = 50 concurrent Claude API вызовов в одну минуту.

### 10.17. config/__init__.py — barrel file sync

При добавлении новой константы в `config/settings.py` — **ОБЯЗАТЕЛЬНО** добавить её в оба места в `config/__init__.py`: блок `from .settings import (...)` И список `__all__`. Без этого — `ImportError` crash loop на деплое (IDE не ловит, потому что `from config.settings import X` работает, а `from config import X` — нет).

### 10.18. Scheduler Log Noise — подавлен

apscheduler INFO-логи (`Running job`, `executed successfully`) подавлены до WARNING в `bot.py`. FSM `get_state`/`set_state` переведены на DEBUG. Для отладки scheduler — временно вернуть INFO.

### 10.19. Look-Ahead Pre-Gen

После доставки занятия/практики `_pregen_next_topic_bg()` генерирует следующую тему в фоне (`asyncio.create_task`, fire-and-forget). Покрывает случай: пользователь пришёл до scheduled delivery.

### 10.20. Haiku On-The-Fly Fallback

При cache miss lesson.py и task.py используют `model=CLAUDE_MODEL_HAIKU` (3-5s вместо 15-19s Sonnet). Pre-gen scheduler и look-ahead всегда Sonnet (default). Worst case latency <5s.

### 10.21. Message Splitting для LLM-контента

`self.send()` с LLM-generated контентом (вопросы, задания, ответы ИИ) → **всегда** `prepare_html_parts()`, **не** `md_to_html()` напрямую. Telegram молча обрезает сообщения >4096 символов без ошибки. Паттерн: `parts = prepare_html_parts(text)` → loop → keyboard на последнем part.

### 10.22. PostgreSQL Views: DROP + CREATE, не REPLACE

`CREATE OR REPLACE VIEW` **запрещён**. PostgreSQL не позволяет менять порядок или имена колонок через REPLACE — бот падает в crash loop при старте. Всегда: `DROP VIEW IF EXISTS` + `CREATE VIEW`. View stateless — данные не теряются.

---

## 11. Error Classification & Observability (WP-45, DP.RUNBOOK.001)

**Модуль:** `core/error_classifier.py` — классифицирует `error_logs` по 8 категориям RUNBOOK (fsm, db, claude_api, telegram_api, mcp, scheduler, deployment, dt) + severity (L1-L4).

**Порядок паттернов:** специфичные (MCP, Claude, TG) → generic (DB). First match wins. При добавлении нового паттерна — проверяй, не перекрывает ли generic (тест: 13 cases в комментарии к WP-45 коммиту).

**Suppression allowlist:** `is_suppressed()` — benign ошибки (user blocked bot, chat not found, flood control). Классифицируются и хранятся, но НЕ попадают в escalation алерты. При добавлении нового benign паттерна — добавить в `_SUPPRESSED_PATTERNS`.

**Scheduler:** classify_unprocessed() каждые 5 мин + check_escalation() каждые 15 мин.

**Grafana dashboard** (`monitoring/grafana-dashboard.json`): 3 секции:
- **Error Monitoring:** error count, L3+, unknown, severity pie, error rate by category
- **Performance & Throughput (Ф2):** RPM by command, p50/p95/p99 latency, request count, error rate %, slowest spans
- **Engagement Analytics:** DAU/WAU/MAU, sessions, QA helpful %, retention cohorts

**Langfuse (опционально):** `core/langfuse_client.py` — dual-write traces в Neon + Langfuse Cloud. Env: `LANGFUSE_SECRET_KEY`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_HOST`.

---

## 12. Progressive UI per Tier (WP-52 v4)

**Файлы:** `core/tier_config.py`, `core/tier_detector.py`, `core/tier_ui.py`, `handlers/reply_keyboard.py`

**Дизайн-документ:** `DS-my-strategy/inbox/WP-52-progressive-ui-tiers.md`
**Pack-сущность:** DP.ARCH.002 § 13

**Два слоя ReplyKeyboard:**
1. **mode_select KB** (2×2) — при возврате в главное меню, tier-dependent
2. **SM-contextual KB** — внутри reply-стейтов: Row 1 = действия, Row 2 = `[🏠 Меню] [⚙️]`

**mode_select layouts:**

```
T0:  [📚 Марафон] [🧪 Тест]      / [📊 Прогресс]  [⚙️ Настройки]
T1:  [📚 Марафон] [🧪 Тест]      / [📊 Прогресс]  [⚙️ Настройки]
T2:  [📖 Лента]   [🧪 Тест]      / [📊 Прогресс]  [⚙️ Настройки]
T3:  [🧬 ЦД]      [📖 Лента]     / [📊 Прогресс]  [⚙️ Настройки]
T4:  [📋 Мой план] [🏛 Клуб]     / [🧬 ЦД]        [⚙️ Настройки]
TD1: = T{N} keyboard + dev-commands в menu (bot.py)
```

**Tier detection (payment-first, WP-85):**
- T0: анонимный / без профиля (UITier.T0)
- T1: привязан к Aisystant, нет активной подписки БР (UITier.T1)
- T2: подписка «Инженерия интеллекта» на Aisystant (UITier.T2_LEARNING)
- T3: T2 + подключён любой AI-клиент (claude.ai / Claude Code / VS Code / Telegram) — WP-406 Ф13. **НЕ** «ЦД подключён»: сигнал AI-клиента бот читает через `_is_ai_client_connected` (Telegram OAuth ИЛИ persona traits `mcp_connected`/`tier>=T3`, выставляет шлюз при claude.ai OAuth)
- T4: T3 + GitHub подключён (требует T3 — `is_github AND is_ai_client`)
- TD1: DEVELOPER_CHAT_ID
- TG Stars = донаты (благодарность), НЕ влияют на тир/доступ
- Тир падает до T1 при истечении подписки БР (WP-210 Ф2a)

**Правила:**
- ⚙️ Настройки = universal settings (Language first, Profile link)
- SM НЕ удаляет KB, а ЗАМЕНЯЕТ на контекстную
- TD1 menu = dev-commands (stats, usage, qa, ...) — set в bot.py, НЕ в tier_config
- Menu ☰ per-user через `BotCommandScopeChat`
- Все команды работают на любом тире (видимость ≠ доступность)
- Paywall text НЕ должен обещать функциональность, которой нет у целевой команды (прецедент: `/start` не показывает тир-инфо)
- **Tier в хэндлерах:** использовать `detect_ui_tier(chat_id)`, НЕ `get_intern().tier`. Поле `public.users.tier` — кэш, обновляется только при вызове `detect_ui_tier()`; если тир менялся (подключили GitHub/ЦД) между деплоями и `detect_ui_tier()` не вызывался — поле устарело (инцидент 2026-06-09: Hermes отказывал T4-пользователям, видел T2 из DB).

---

## 12b. DT Engagement Sync (WP-85 Phase 4)

**Файлы:** `db/queries/dt_sync.py`, `core/scheduler.py` (cron 04:30 MSK)

**Принцип:** Бот пишет события в `development.user_events` → SQL View `development.engagement` агрегирует 15 метрик → `sync_engagement_to_dt()` записывает в `digital_twins` JSONB (INSERT ON CONFLICT, deep merge) → DT MCP читает при запросе.

**5 групп в `2_collected/`:** `2_1_account` (сессии), `2_2_courses` (развитие), `2_3_practice` (практика), `2_4_time` (ритм), `2_5_notifications` (уведомления, WP-152 Ф4).

**Notification engagement (WP-152 Ф4):** SQL View `development.notification_engagement` агрегирует `notification_log` (JOIN `users` по `telegram_id`). `sync_engagement_to_dt()` preload-ит view в `notif_map` и merge-ит в `2_5_notifications` при наличии данных. Graceful fallback: если view не существует — warning в лог, sync продолжается без notifications.

**Identity model:** `digital_twins.user_id` = Ory UUID. Sync фильтрует `WHERE user_uuid IS NOT NULL` (T1+). T0 копят события по chat_id — при появлении UUID sync подхватит автоматически.

**Dev-команда:** `/dt_sync` — ручной запуск sync (TD1 only).

## 12c. Бот как collector ЦД (WP-218 Ф2)

> **Архитектурное правило (WP-218 принцип №1):** бот НЕ содержит calculator.
> Расчёт `3_derived` — ответственность R28 Profiler (AISYS.018), который живёт
> в `DS-IT-systems/DS-ai-systems/profiler/scripts/recalculate_derived.py`
> (standalone Python runtime, подключается к Neon напрямую через psycopg2).

**Файл в боте:** `db/queries/dt_sync.py` — **только collector**, пишет в `2_collected`.

**Что делает бот (collector):**
- `sync_engagement_to_dt()` — читает `development.engagement` + `notification_engagement` views + LMS `qualification_level_event` + `user_events source='iwe'` → пишет в `digital_twins.data['2_collected']` через deep merge. Cron 04:30 MSK + команда `/dt_sync`.
- `sync_one_user_to_dt(user_id)` — on-demand collector для одного user_id (триггерится после GitHub webhook учебного занятия).

**Что делает profiler (calculator, отдельно от бота):**
- Читает полный `digital_twins.data['2_collected']` (включая секции от всех writers: бот, `dt-collect-neon.py`, `collectors.d/*.sh`).
- Вызывает чистые функции из `dt_calc.py`.
- Пишет ТОЛЬКО в `3_derived` через SQL deep merge. Cron запускается после всех collectors (TODO: scheduling).

**Чтение `3_derived` в боте:**
- `handlers/twin.py` → pure reader: SELECT `data->'3_derived'` из Neon. НИКАКИХ вызовов calculator.
- Для каждого числа в UI — IND-комментарий (трассируемость к метамодели).

**Рекомендация разработчикам:** если нужно что-то расчётное добавить (новый IND, фикс формулы) — работа в `DS-ai-systems/profiler/scripts/dt_calc.py`, не в боте. Бот не меняется.

---

## 13. Данные: schedule_time и marathon_content

### schedule_time — строго HH:MM (zero-padded)

Scheduler сравнивает `schedule_time = f"{hour:02d}:{minute:02d}"` (exact match). Данные без ведущего нуля (`'7:30'` вместо `'07:30'`) → silent failure (0 результатов, нет ошибки).

**Нормализация на записи:** `zfill(5)` в `update_intern()`, `settings.py`, `profile.py`. Integrity check: `_check_schedule_integrity()` в `core/scheduler.py`, ежедневно 08:00 MSK.

### marathon_status lifecycle — ИСПРАВЛЕНО (2026-02-27, дополнено 2026-03-17)

**Правило:** `marathon_start_date` и `marathon_status` — связанная пара. Любой `update_intern(marathon_start_date=...)` ОБЯЗАН также ставить `marathon_status=MarathonStatus.ACTIVE`. Исправлено в 5 местах: onboarding.py, settings.py, mode_selector.py (2x), legacy/learning.py. `_check_schedule_integrity()` auto-fix расширен: фиксит пользователей с `start_date <= today` даже без прогресса.

**Завершение марафона (4 code paths):** При `completed_topics >= total` ОБЯЗАН ставить `marathon_status=MarathonStatus.COMPLETED` + `mode=derive_mode(COMPLETED, feed_status)`. Без этого scheduler повторно отправляет поздравление. Все 4 пути: (1) `core/scheduler.py` — scheduled delivery, (2) `handlers/legacy/learning.py` — legacy, (3) `states/.../task.py` — SM основной путь, (4) `states/.../lesson.py` — SM guard при повторном входе.

### marathon_content — семантика полей

| DB поле | Значение | Кто ставит |
|---------|---------|-----------|
| `status = 'pending'` | Контент сгенерирован, пользователь **не открыл** | pre-gen (insert) |
| `status = 'delivered'` | Пользователь **открыл** занятие | `mark_content_delivered()` в lesson.py |
| `notification_sent_at` | Когда уведомление отправлено пользователю | `mark_notification_sent()` в scheduler.py (log-before-send) |

**Idempotency:** `notification_sent_at` — guard против повторной отправки. Scheduler проверяет `notification_sent_at >= today` ДО отправки. Catch-up (`_catch_up_missed_deliveries`) ищет пользователей без `notification_sent_at` за сегодня (не `created_at` — контент может быть пре-генерирован заранее).

`/delivery` dev-команда показывает эту разницу: 🟢 прочитано / 🟡 отправлено, не открыт.

### data/marathon-content.json — READ-ONLY (sync-managed)

⛔ **ЗАПРЕЩЕНО редактировать `data/marathon-content.json` напрямую.** Файл управляется скриптом sync и будет перезаписан при следующем запуске.

**Source-of-truth:** `DS-marathon-v2-tseren/materials/participants/marathon-content.json` (авторский репо).

**Правильный поток для любых правок текста марафона:**
1. Редактировать ТОЛЬКО авторский файл в `DS-marathon-v2-tseren/`
2. Закоммитить авторский файл ДО запуска sync (`git add <file> && git commit && git push`)
3. Sync: `bash scripts/sync-marathon-content.sh`
4. Закоммитить результат: `git add data/marathon-content.json && git commit && git push`

**Почему важно:** 9 июня 2026 sync (коммит `138a760`) перезаписал правильные правки бота (`db243c0`: IWE→ИИ-помощник) незакоммиченным авторским файлом. Незакоммиченный авторский файл = undefined state при sync.

### Catch-up (нагонять пропущенное занятие)

- Если `topic['day'] < marathon_day` → catch-up notification (вместо обычного).
- После прохождения пропущенного → предложить сегодняшнее занятие (генерация на лету по кнопке).
- Ограничение: **max 1 день** catch-up. `MAX_TOPICS_PER_DAY = 4` (2 yesterday + 2 today).

### Cutover legacy ↔ новый движок марафона (Block MAR, 6 июня)

> **Инвариант:** для пользователя на новом движке (есть строка `learning.marathon_progress`) ОБА legacy-пути обязаны молчать. Иначе два движка шлют параллельно: занятия «День 2» новым + напоминания «День 1 не начат» старым (два независимых счётчика дня).

**Единый предикат:** `db.queries.marathon_newcomer.is_on_newcomer_marathon(user_id)` — наличие строки в `learning.marathon_progress`. Гейтит:
1. **Доставку** — `get_all_scheduled_interns` (`db/queries/users.py`) исключает таких из legacy-рассылки (cutover `34dcb6f`).
2. **Напоминания +1h/+3h** — `send_reminder` (`core/scheduler.py`) гасит legacy-напоминание (это и был хвост: cutover закрыл только доставку, таблица `reminder` слала дальше). Custom-напоминания (DP.SC.134, `send_user_reminder`) НЕ гейтятся.

**Правило:** при добавлении нового legacy-пути доставки/напоминаний — гейтить `is_on_newcomer_marathon`. Тест: `tests/test_marathon_reminder_gate.py`.

---

### Новый набор учебных ячеек (child/kids curriculum) — чеклист wire-up

**Паттерн:** data-файл + load-функция ≠ работающая фича. При добавлении нового набора ячеек в training flow обновить ВСЕ:

1. `engines/training/engine.py` — импорт `load_*_cells`, `*_PRINCIPLES`, `*_MAX_DEPTH`
2. `generate_child_assignment()` — `load_*_cells()` + правильный MAX_DEPTH
3. `report_child_result()` — `load_*_cells()`
4. `get_child_dashboard_data()` — `*_PRINCIPLES` + `*_MAX_DEPTH` + `get_*_principle_name()`
5. `get_next_child_principle()` — `*_PRINCIPLES` + `*_MAX_DEPTH`
6. `states/training/child_assignment.py` — импорт `KID_MAX_DEPTH`, `get_kid_principle_name`

Без wire-up всех 6 мест → фича молча не работает (бот возвращает None и не показывает принципы).

### 10.23. f-string + regex quantifier

`rf'^#{1,3}\s*{en}\b'` — Python интерпретирует `{1,3}` как f-string expression (tuple `(1, 3)`), не regex quantifier. Фикс: `rf'^#{{1,3}}\s*{en}\b'` — двойные скобки экранируют.

### 10.24. re.sub не поддерживает \u в replacement

`re.sub(r'...', r'\1\u200B.\2', text)` → `re.error: bad escape \u`. Фикс: вынести символ в переменную: `ZWS = '\u200B'` → `rf'\1{ZWS}.\2'`.

### 10.25. TG auto-linking filenames (sanitize_file_extensions)

Telegram авто-линкует `word.ext` как URL (`.md`, `.sh`, `.py` и т.д.). `helpers/message_split.py:sanitize_file_extensions()` вставляет ZWS перед расширением. Применяется в `base.py:send()` для всех HTML-сообщений. Внутри `<code>`/`<pre>` — не трогает.

---

### 10.26. Typing indicator для долгих операций

**Модуль:** `helpers/typing_indicator.py` — context manager `keep_typing(message)`.

**Правило:** При долгой операции (Claude API, MCP search, external API) — **ОБЯЗАТЕЛЬНО** обернуть в `keep_typing`:

```python
from helpers.typing_indicator import keep_typing

await message.answer(t('loading_key', lang))
async with keep_typing(message):
    result = await claude.generate(...)  # typing виден всё время
# typing автоматически прекращается при выходе
```

**Как работает:** фоновая задача отправляет `send_chat_action("typing")` каждые 4 сек. При выходе из `async with` (ответ готов / ошибка / early return) — задача отменяется, индикатор пропадает.

**Не нужен:** если операция <3 сек (простой DB-запрос) или если есть свой `_keep_typing()` цикл (consultation.py).

### 10.27. FSM-стейты блокируют SM global events

aiogram FSM-хендлеры (`@router.message(State)`) перехватывают ВСЕ сообщения, блокируя SM global events (включая `?`-консультацию). `ConsultationPassthroughMiddleware` (core/middleware.py) решает это на middleware-уровне: очищает FSM state + сбрасывает `data['raw_state']` для `?`-сообщений ДО роутинга.

**Важно:** aiogram кэширует FSM state в `data['raw_state']` до outer middleware. `state.clear()` обновляет DB, но `StateFilter` использует кэш. Обязательно: `data['raw_state'] = None` после `state.clear()`.

---

### 10.28. Webhook allowed_updates (SC.118)

`set_webhook()` по умолчанию НЕ включает `channel_post` и `my_chat_member`. Без явного `allowed_updates=[..., "channel_post", "my_chat_member"]` Telegram не отправляет эти updates боту. При добавлении нового типа update — обновить список в bot.py (оба места: основной + re-register).

### 10.29. Fallback не обрабатывает каналы/группы

`fallback.py:on_unknown_message` игнорирует `chat.type in ('channel', 'group', 'supergroup')`. Без этого fallback создаёт «пользователя» для chat_id канала и запускает onboarding в канал.

### 10.30. SC.118 Channel Mentions Assistant

**Модуль:** `handlers/channels.py`, `core/mention_detector.py`, `db/queries/channels.py`

**Как работает:** Бот как админ в группе → `channel_post`/`message` → `detect_mentions()` (username, reply, имя) → admin: черновик через Opus + knowledge-mcp → личка. Участник: простое уведомление.

**Auto-discovery:** При первом сообщении из канала без мониторов → `getChatMember` для всех пользователей бота → создание мониторов для найденных админов. Кэш `_discovered_channels` предотвращает повторный перебор.

**Контекст черновика (3 источника):**
1. **Владелец** — профиль из ЦД (`1_declarative`): имя, занятие, роли, интересы. Graceful fallback при отсутствии ЦД.
2. **Канал** — `config/channel_contexts.yaml`: описание, аудитория, тон, темы. Матчинг по `title_pattern` (regex). Fallback на `default`.
3. **Knowledge** — расширенный поиск: до 5 результатов по 500 символов + дополнительный поиск по темам канала.

**Правила:** Cooldown 30 сек. Log-before-send дедупликация. `is_onboarded()` — async, вызывать с await. При добавлении нового канала — добавить запись в `config/channel_contexts.yaml`.

### 10.31. GitHub Contents API: файлы >1 MB

`read_binary_file()` (`clients/github_content.py`): GitHub Contents API возвращает `content` (base64) только для файлов **≤1 MB**. Для крупных файлов (cover.png ~2 MB) — `content` отсутствует, только `download_url`. Обязательно: `data.get("content")` → fallback на `data.get("download_url")`. Без fallback — `KeyError` молча ловится в `except` → функция пропускается.

---

## SOTA: Context Engineering (DP.SOTA.002)

> Бот — surface view над Pack и DDT. Контекст бота = проекция, не копия.

- Каждый state machine state = bounded context с собственным контекстом
- Ответы бота = view over DDT/Pack, не хранение знаний
- При добавлении нового state → определить: что в always-in-context? что on-demand?
- MCP tools = select-стратегия: агент выбирает минимальный контекст для задачи

### 10.32. Inline drill-down: не удалять сообщение, route через dispatcher

**Правило 1:** Callback из inline-кнопки НЕ должен удалять исходное сообщение (`callback.message.delete()`). TG-паттерн: drill-down = новое сообщение ниже, старое остаётся. Delete путает пользователя и ломает `callback.message.answer()`.

**Правило 2:** Если callback должен запустить SM state (mydata, progress, plans) — вызывать через `dispatcher.route_command('mydata', intern)`, НЕ отправлять текстовую подсказку «Используйте /mydata». Текст не запускает SM.

### 10.33. Railway env vars: UI обрезает длинные значения

Railway UI отображает значения переменных с обрезанием (~39 символов). Реальное значение в контейнере может быть тоже обрезано, если вводить через UI с длинной строкой. **Правило:** для секретов (HMAC, токены) использовать `railway variable set KEY=VALUE` через CLI или короткий чистый hex (`secrets.token_hex(32)` = 64 символа). Не добавлять суффиксы вроде `_uuid4` — это часть значения, не метка.

### 10.34. HMAC-диагностика webhook: логировать prefix без раскрытия секрета

При отладке HMAC 403 логировать: `sig_header[:16]`, `expected[:16]`, `match=bool`. Этого достаточно для диагностики расхождения. Удалять после fix — не оставлять в проде.

### 10.35. fsm_states.data затирается fallback state.clear()

`fallback.py:on_unknown_message` вызывает `state.clear()` перед маршрутизацией в SM. Это **затирает `fsm_states.data`**. НЕ хранить персистентный контекст SM-стейтов в `fsm_states.data` — использовать `current_context` в `development.user_state` (через `update_intern`). Пример: `mydata.py` — контекст `awaiting_delete`.

### 10.36. SM-стейты: persistence только через BaseState API

**Запрещено** хранить прогресс стейта в class-level dict: `_user_data: Dict[int, Dict] = {}`. При Railway redeploy процесс умирает → dict обнуляется → LLM оценивает ответ против пустой строки → тихая порча БД.

**Паттерн:** использовать `BaseState.save_state(user, data)` / `load_state(user)` / `clear_state(user)`. Данные хранятся в `development.user_state.current_context` (JSONB) под namespace-ключом стейта — переживают redeploy.

**Исключение:** UI-флаги без побочных эффектов (например, `waiting_fixation` в `digest.py`) допустимы в памяти, если потеря флага при redeploy приводит только к повторному показу интерфейса (не к порче данных).

### 10.37. Middleware: запрет lazy imports, обязательный smoke-тест

**Инцидент-источник:** B4.3 (12 апр 2026) добавил `RateLimitMiddleware` с `from config.settings import DEVELOPER_CHAT_ID` внутри `__call__`. Константа не существовала → `ImportError` на каждом сообщении → aiogram глотал молча → бот не отвечал 14 часов. Webhook возвращал 200, Railway показывал SUCCESS — проблема не была видна снаружи.

**Правило 1 — Все imports на уровне модуля:**
```python
# ❌ Запрещено
async def __call__(self, handler, event, data):
    from config.settings import SOME_CONSTANT  # lazy import в __call__

# ✅ Правильно
from config.settings import SOME_CONSTANT  # на уровне модуля

async def __call__(self, handler, event, data):
    ...  # использовать SOME_CONSTANT напрямую
```

**Правило 2 — Smoke-тест обязателен:**
При добавлении нового middleware → добавить в `tests/smoke/test_middleware.py`:
- `test_import_<name>` — проверяет что класс импортируется
- `test_<name>_call_does_not_crash` — вызывает `__call__` с fake Message

**Правило 3 — config/__init__.py barrel sync (правило 10.17):**
Любая новая константа из `config/settings.py`, используемая в middleware, должна быть добавлена в оба места `config/__init__.py` (import + `__all__`).

### 10.38. `___` (тройной underscore) в статическом контенте Markdown v1

Telegram Markdown v1 парсит `_` как маркер курсива. Три подряд `___` создают: парный `_`+`_` (пустой курсив) + непарный `_` (ищет закрывающую пару до конца текста). Если последний `_` в тексте — это открывающий маркер → ошибка `Can't find end of entity at byte offset N`.

**Источник (2026-06-07):** `practice_short_simple` Day 1 марафона — 988 байт, `___` как fill-in prompt на байтах 545-547, последний `_` на байте 987 → именно эта ошибка у пользователя babais.

**Правило:** в текстах `marathon-content.json` и любом статическом контенте с `parse_mode="Markdown"`:
- `___` как заполнитель → заменить на `(Хаос / Тупик / Поворот)` или конкретный текст
- После правки — проверить все варианты (practice_short_simple, practice_long_simple и т.д.)
- Добавить `try/except` с fallback без parse_mode в каждый handler, отправляющий статический контент

**SoT контента:** `DS-marathon-v2-tseren/materials/participants/marathon-content.json` → sync → `data/marathon-content.json` (bot runtime). Dockerfile не включает DS-marathon-v2-tseren → fallback path в prod недоступен.

### 10.39. T4-full тестировать нельзя через тир-специфичный код консультации

`handlers/fallback.py:100` — T4-аккаунты (`tier_num >= 4`) при обычном сообщении уходят целиком в Hermes (`gateway_mcp.hermes_chat()`), минуя `handle_question_with_tools()`/`consultation.py`. Живой E2E-прогон фич, завязанных на консультацию (tool_use, discovery), через T4-аккаунт технически не проверяет их — трафик идёт другим кодом.

**Источник (2026-07-07, WP-5):** попытка живого прогона generic MCP tool discovery через реальный аккаунт пилота (T4) не дошла до кода, который проверялась.

**Правило:** для E2E теста consultation-специфичных фич нужен онбордированный тестовый аккаунт тира T1-T3, не T4.

### 10.40. `gateway_mcp.list_tools()` берёт произвольный токен без retry-on-401

`clients/gateway_mcp.py::list_tools()` для discovery-запроса (`tools/list`) использует `next(iter(self._tokens.values()))` — первый попавшийся из всех загруженных Ory-токенов, без проверки срока годности. При 401 нет повторной попытки с другим токеном — весь discovery молча возвращает stale/пустой кэш до следующего успешного вызова (TTL 15 мин или следующий рестарт).

**Источник (2026-07-07, WP-5):** на пилотном боте при старте 08:19 UTC (после деплоя `f7ddb12`) — `Gateway: tools/list HTTP 401` → 0 discovered tools, без последующих попыток в логах.

**Правило:** при диагностике «бот не видит новый MCP-инструмент» — сначала проверить лог строки `Gateway: discovery loaded`/`✅ Gateway MCP: discovery N tools` при старте, не сам код фильтрации инструментов.

### 10.41. WP-253 lift-and-shift: проверка по `db/models.py` недостаточна, нужен query-layer

Множество таблиц мигрировали (WP-253, 8 мая 2026) из основного пула в отдельные Neon-БД — часто под именем в единственном числе (`channel_monitors`→`channel_monitor`, `discourse_accounts`→`club_account`, `training_settings`→`training_setting` и т.д.). Легаси-таблица во множественном числе продолжает существовать в основном пуле (`db/models.py` её не удаляет) и обычно пуста, но код, ориентирующийся только на `db/models.py`, этого не видит.

**Источник (2026-08-06, ревью фикса `delete_all_user_data`):** список таблиц для GDPR-удаления собирался по `CREATE TABLE` в `db/models.py` — прошёл мимо 8 таблиц в 4 отдельных пулах (publication/lead/reference/learning), включая `training_child` с именем ребёнка. Каждый модуль `db/queries/*.py`, чья таблица мигрировала, несёт в docstring строку вида `WP-253 lift-and-shift: X → Y (Z pool)` — этого достаточно, чтобы найти реальное расположение, но только если туда заглянуть.

**Правило:** для любой таблицы, участвующей в кросс-пуловой операции (удаление, экспорт, аудит) — сверять не только `db/models.py`, но и модуль `db/queries/*.py`, который её реально читает/пишет. Признак миграции — комментарий `WP-253 lift-and-shift` в начале файла.

### 10.42. Живая доставка (`/learn`) обязана гасить очередь — иначе дубль Дня N

`_deliver_marathon_lesson()` (on-demand путь: `/learn`, кнопка «Учиться», catch-up) отдаёт текст занятия напрямую из `get_day_text()` и НЕ трогает `learning.marathon_queue`. Scheduler (`_process_marathon_queue`, `core/scheduler.py`, каждые 10 мин) не знает об этой живой выдаче и независимо шлёт тот же `lesson_practice`, когда наступает его `scheduled_at`.

**Когда стреляет:** участник стартует марафон вечером после 18:00 МСК (`MARATHON_SAME_DAY_CUTOFF_HOUR`) — День 1 планируется на завтра 09:00 МСК, а не сразу. Если сразу после старта кто-то вызывает `/learn` — занятие уходит живьём в тот же вечер, а утром приходит ещё раз по расписанию. Дедуп `has_recent_lesson_practice_sent()` не спасает: он сам читает `status='sent'` из той же очереди, которую живая выдача не заполняет.

**Источник (2026-08-10):** два живых аккаунта получили День 1 дважды с разницей в 3 минуты (один участник + сопровождающий сотрудник на отдельном тестовом аккаунте, оба помогали друг другу настроить бота).

**Фикс:** `mark_lesson_practice_delivered(user_id, day)` (`db/queries/marathon_newcomer.py`) — помечает `learning.marathon_queue` этого дня `sent` сразу после успешной живой доставки (идемпотентно, `WHERE status='pending'`), тем же способом, что и `mark_queue_sent()` у планировщика. Правило: любой НОВЫЙ on-demand путь доставки контента марафона обязан делать то же самое, иначе дубль повторится для него.
