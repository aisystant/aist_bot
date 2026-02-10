# CLAUDE.md — AIST Track Bot (new-architecture)

> **Общие инструкции:** см. `/Users/tserentserenov/Github/CLAUDE.md`
>
> Этот файл содержит только специфику данного репозитория.

---

## 1. Тип репозитория

**Downstream-instrument** — Telegram-бот марафона личного развития.

**НЕ является source-of-truth** — определения в Pack'ах.

**Ветка:** `new-architecture` (State Machine в разработке)

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
│   ├── fallback.py           # Catch-all: SM routing или legacy
│   └── legacy/
│       ├── learning.py       # LearningStates + send_topic (USE_STATE_MACHINE=false)
│       └── fallback_handler.py # Legacy обработка вне FSM
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
| FSM стейты (UpdateStates, LearningStates) | `handlers.settings`, `handlers.legacy.learning` | ~~bot~~ |
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

---

## 9. Частые ошибки

| Неправильно | Правильно |
|-------------|-----------|
| "контент" (в Ленте) | Дайджест |
| "тема" | Урок / Задание / Дайджест |
| "сессия" | Дайджест / День |
| "рефлексия" | Фиксация |
| "пользователь" | Ученик / Читатель |

---

## 10. Ловушки i18n и UI

### 10.1. Markdown-краш при отсутствии ключа

`t()` при отсутствии ключа возвращает строку ключа (напр. `"help.about_marathon"`).
Если в ней `_` — Telegram интерпретирует как курсив → `TelegramBadRequest: can't parse entities`.

**Правило:** при добавлении нового `t()` вызова — убедись, что ключ существует в schema.yaml + es.yaml + fr.yaml.

### 10.2. Inline keyboard при смене стейта

При переходе между стейтами с разными типами клавиатур (inline → reply) **обязательно** удалять/редактировать сообщение со старой inline-клавиатурой. Иначе пользователь кликает по устаревшим кнопкам → `Unhandled callback`.
