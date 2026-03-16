# CLAUDE.md — AIST_me_bot (new-architecture)

> **Общие инструкции:** см. `/Users/tserentserenov/IWE/CLAUDE.md`
>
> Этот файл содержит только специфику данного репозитория.

---

## 1. Тип репозитория

**DS/instrument** — Telegram-бот марафона личного развития.

**НЕ является source-of-truth** — определения в Pack'ах.

**Ветки:** `pilot` (разработка) → `new-architecture` (прод). Правило Pilot-First — см. MEMORY.md.

**Pre-push hook:** `.githooks/pre-push` блокирует push в `new-architecture` без `FORCE_PROD=1`. После clone: `git config core.hooksPath .githooks`.

---

## 2. Терминология

**ВСЕГДА используй термины из [ontology.md](ontology.md).**

### 2.1. Краткая справка

| Термин | Что это |
|--------|---------|
| **Ученик** | Пользователь Марафона |
| **Читатель** | Пользователь Ленты |
| **Марафон** | 14-дневная программа |
| **Лента** | Гибкое обучение по дайджестам |
| **Урок** | Теория в Марафоне |
| **Задание** | Практика в Марафоне |
| **Дайджест** | Ежедневный материал в Ленте |
| **Фиксация** | Личный вывод Читателя |

### 2.2. Соответствие кода и терминов

| Термин | В коде сейчас | Целевое имя |
|--------|---------------|-------------|
| Урок | `theory` | `lesson` |
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

## 4. Три уровня документации

| Категория | Описывает | Папка |
|-----------|-----------|-------|
| **Сценарий** | Взаимодействие с ботом | `docs/scenarios/` |
| **Процесс** | Внутренняя логика | `docs/processes/` |
| **Данные** | Структура БД | `docs/data/` |

---

## 5. Правила разработки

### 5.1. При изменении кода — СПРОСИ

**Любое изменение кода требует:**
1. Определить категорию (Сценарий/Процесс/Данные)
2. Спросить: "Это изменение затронет [категория]. Подтвердите?"
3. Дождаться подтверждения
4. Код + документация в одном коммите

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
- **Adaptive max_tokens** в `generate_content`: `min(words × 1.5, 4096)`. Не hardcode 4000.
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
| "тема" | Урок / Задание / Дайджест |
| "сессия" | Дайджест / День |
| "рефлексия" | Фиксация |
| "пользователь" | Ученик / Читатель |

### Anti-hallucination в промптах консультанта

1. **Граница знаний = правило #1** (question_handler.py). Инструкция «НЕ додумывай» должна быть первой, не третьей — иначе конкурирует с другими правилами.
2. **Depth instruction → «используй ВСЕ из контекста»**, а НЕ «объясни механизмы, приведи примеры» — второе провоцирует генерацию из параметрической памяти.
3. **Structured data > MCP** для точных ответов: `previous_days_connection` из topic YAML → `format_structured_context()` → модель не выдумывает связи.

---

## 10. Ловушки i18n и UI

### 10.1. Markdown-краш при отсутствии ключа

`t()` при отсутствии ключа возвращает строку ключа (напр. `"help.about_marathon"`).
Если в ней `_` — Telegram интерпретирует как курсив → `TelegramBadRequest: can't parse entities`.

**Правило:** при добавлении нового `t()` вызова — убедись, что ключ существует в schema.yaml + es.yaml + fr.yaml.

### 10.2. Markdown fallback для Claude-контента

При отправке LLM-генерированного текста с `parse_mode="Markdown"` — **всегда** оборачивай в `try/except` с fallback без форматирования. Claude может генерировать незакрытые сущности (`*`, `_`, `[`), которые ломают Telegram API (`TelegramBadRequest: can't parse entities`).

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

### 10.10. Scheduler = read-only для user state

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
| **Sonnet** | Уроки, практика, консультации (L3 + tool_use), /twin insights | Креативный/сложный вывод, нужен reasoning, следование FORBIDDEN-правилам |

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

`MAX_USERS_PER_SLOT = 50` (db/queries/users.py). При выборе времени обучения (onboarding + settings):
1. `get_slot_load(time)` → считает пользователей в окне ±5 мин (11 слотов)
2. Если count ≥ 50 → `kb_slot_suggestions()` показывает до 3 🟢 свободных слотов + 🟡 «оставить как есть»
3. Это **мягкое** ограничение — пользователь может настоять на перегруженном слоте

**Зачем:** рассредоточение нагрузки на scheduler pre-generation. Без staggering — все 50 users = 50 concurrent Claude API вызовов в одну минуту.

### 10.17. config/__init__.py — barrel file sync

При добавлении новой константы в `config/settings.py` — **ОБЯЗАТЕЛЬНО** добавить её в оба места в `config/__init__.py`: блок `from .settings import (...)` И список `__all__`. Без этого — `ImportError` crash loop на деплое (IDE не ловит, потому что `from config.settings import X` работает, а `from config import X` — нет).

### 10.18. Scheduler Log Noise — подавлен

apscheduler INFO-логи (`Running job`, `executed successfully`) подавлены до WARNING в `bot.py`. FSM `get_state`/`set_state` переведены на DEBUG. Для отладки scheduler — временно вернуть INFO.

### 10.19. Look-Ahead Pre-Gen

После доставки урока/практики `_pregen_next_topic_bg()` генерирует следующую тему в фоне (`asyncio.create_task`, fire-and-forget). Покрывает случай: пользователь пришёл до scheduled delivery.

### 10.20. Haiku On-The-Fly Fallback

При cache miss lesson.py и task.py используют `model=CLAUDE_MODEL_HAIKU` (3-5s вместо 15-19s Sonnet). Pre-gen scheduler и look-ahead всегда Sonnet (default). Worst case latency <5s.

### 10.21. Message Splitting для LLM-контента

`self.send()` с LLM-generated контентом (вопросы, задания, ответы ИИ) → **всегда** `prepare_html_parts()`, **не** `md_to_html()` напрямую. Telegram молча обрезает сообщения >4096 символов без ошибки. Паттерн: `parts = prepare_html_parts(text)` → loop → keyboard на последнем part.

### 10.22. PostgreSQL Views: DROP + CREATE, не REPLACE

`CREATE OR REPLACE VIEW` **запрещён**. PostgreSQL не позволяет менять порядок или имена колонок через REPLACE — бот падает в crash loop при старте. Всегда: `DROP VIEW IF EXISTS` + `CREATE VIEW`. View stateless — данные не теряются.

---

## 11. Error Classification (WP-45, DP.RUNBOOK.001)

**Модуль:** `core/error_classifier.py` — классифицирует `error_logs` по 6 категориям RUNBOOK (fsm, db, claude_api, telegram_api, mcp, scheduler) + severity (L1-L4).

**Порядок паттернов:** специфичные (MCP, Claude, TG) → generic (DB). First match wins. При добавлении нового паттерна — проверяй, не перекрывает ли generic (тест: 13 cases в комментарии к WP-45 коммиту).

**Scheduler:** classify_unprocessed() каждые 5 мин + check_escalation() каждые 15 мин.

**Grafana:** dashboard JSON в `monitoring/grafana-dashboard.json` (PostgreSQL datasource → Neon).

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
- T1: привязан к Aisystant, нет подписки БР и триал истёк (UITier.T1)
- T2: подписка «Бесконечное развитие» на Aisystant ИЛИ 30-дн. триал от /start
- T3: T2 + ЦД подключён
- T4: T3 + GitHub подключён
- TD1: DEVELOPER_CHAT_ID
- TG Stars = донаты (благодарность), НЕ влияют на тир/доступ
- Тир падает до T1 при истечении подписки БР и триала

**Правила:**
- ⚙️ Настройки = universal settings (Language first, Profile link)
- SM НЕ удаляет KB, а ЗАМЕНЯЕТ на контекстную
- TD1 menu = dev-commands (stats, usage, qa, ...) — set в bot.py, НЕ в tier_config
- Menu ☰ per-user через `BotCommandScopeChat`
- Все команды работают на любом тире (видимость ≠ доступность)
- Paywall text НЕ должен обещать функциональность, которой нет у целевой команды (урок: `/start` не показывает тир-инфо)

---

## 12b. DT Engagement Sync (WP-85 Phase 4)

**Файлы:** `db/queries/dt_sync.py`, `core/scheduler.py` (cron 04:30 MSK)

**Принцип:** Бот пишет события в `development.user_events` → SQL View `development.engagement` агрегирует 15 метрик → `sync_engagement_to_dt()` записывает в `digital_twins` JSONB (INSERT ON CONFLICT, deep merge) → DT MCP читает при запросе.

**4 группы в `2_collected/`:** `2_1_account` (сессии), `2_2_courses` (обучение), `2_3_practice` (практика), `2_4_time` (ритм).

**Identity model:** `digital_twins.user_id` = Ory UUID. Sync фильтрует `WHERE user_uuid IS NOT NULL` (T1+). T0 копят события по chat_id — при появлении UUID sync подхватит автоматически.

**Dev-команда:** `/dt_sync` — ручной запуск sync (TD1 only).

---

## 13. Данные: schedule_time и marathon_content

### schedule_time — строго HH:MM (zero-padded)

Scheduler сравнивает `schedule_time = f"{hour:02d}:{minute:02d}"` (exact match). Данные без ведущего нуля (`'7:30'` вместо `'07:30'`) → silent failure (0 результатов, нет ошибки).

**Нормализация на записи:** `zfill(5)` в `update_intern()`, `settings.py`, `profile.py`. Integrity check: `_check_schedule_integrity()` в `core/scheduler.py`, ежедневно 08:00 MSK.

### marathon_status lifecycle — ИСПРАВЛЕНО (2026-02-27)

**Правило:** `marathon_start_date` и `marathon_status` — связанная пара. Любой `update_intern(marathon_start_date=...)` ОБЯЗАН также ставить `marathon_status=MarathonStatus.ACTIVE`. Исправлено в 5 местах: onboarding.py, settings.py, mode_selector.py (2x), legacy/learning.py. `_check_schedule_integrity()` auto-fix расширен: фиксит пользователей с `start_date <= today` даже без прогресса.

### marathon_content.status — семантика

| DB status | Значение | Кто ставит |
|-----------|---------|-----------|
| `pending` | Контент отправлен, пользователь **не открыл** | pre-gen (insert) |
| `delivered` | Пользователь **открыл** урок | `mark_content_delivered()` в lesson.py |

`/delivery` dev-команда показывает эту разницу: 🟢 прочитано / 🟡 отправлено, не открыт.

### Catch-up (нагонять пропущенный урок)

- Если `topic['day'] < marathon_day` → catch-up notification (вместо обычного).
- После прохождения пропущенного → предложить сегодняшний урок (генерация на лету по кнопке).
- Ограничение: **max 1 день** catch-up. `MAX_TOPICS_PER_DAY = 4` (2 yesterday + 2 today).

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

## SOTA: Context Engineering (DP.SOTA.002)

> Бот — surface view над Pack и DDT. Контекст бота = проекция, не копия.

- Каждый state machine state = bounded context с собственным контекстом
- Ответы бота = view over DDT/Pack, не хранение знаний
- При добавлении нового state → определить: что в always-in-context? что on-demand?
- MCP tools = select-стратегия: агент выбирает минимальный контекст для задачи
