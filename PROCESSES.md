---
type: process
status: active
created: 2026-02-10
updated: 2026-02-10
---

# Процессы бота Aist

> Внутренние процессы Telegram-бота марафона.
> Межсистемные сценарии → `ecosystem-development/PROCESSES.md`.
> Детальная документация: `docs/scenarios/`, `docs/processes/`, `docs/data/`.

---

## 1. FSM Routing (обработка сообщения)

> Тип: внутренний процесс
> Владелец: Бот (State Machine)

**Вход:** Сообщение от пользователя (Telegram update)

**Действие:**
1. Middleware: логирование, авторизация
2. Router: engines → handlers → fallback (порядок критичен!)
3. Dispatcher: определение режима (Марафон/Лента) → вызов SM
4. State Machine: текущий стейт → `handle()` → событие → переход
5. Новый стейт: `enter()` → ответ пользователю

**Выход:** Ответ пользователю (сообщение/клавиатура) + новое состояние FSM

---

## 2. Onboarding (регистрация ученика)

> Тип: внутренний процесс
> Владелец: Бот (handlers/onboarding.py)

**Вход:** Команда /start от нового пользователя

**Действие:**
1. Проверка: новый или существующий пользователь
2. Выбор языка
3. Создание профиля в БД
4. Выбор режима (Марафон / Лента)
5. Переход в соответствующий стейт SM

**Выход:** Профиль в БД, пользователь в начальном стейте выбранного режима

---

## 3. Урок Марафона (подача + проверка)

> Тип: внутренний процесс
> Владелец: Бот (states/workshops/marathon/)

**Вход:** Пользователь в стейте `lesson` или `question`

**Действие:**
1. `lesson.enter()` → показать теорию дня
2. Пользователь читает → нажимает «Готов»
3. `question.enter()` → показать вопрос (Bloom's taxonomy)
4. Пользователь отвечает → оценка через Claude API
5. При правильном ответе → переход к заданию или бонусу

**Выход:** Прогресс ученика обновлён, ответ сохранён в БД

---

## 4. Deploy (обновление бота)

> Тип: внутренний процесс
> Владелец: Разработчик

**Вход:** Код в ветке (main или new-architecture)

**Действие:**
1. Тесты (smoke test + E2E в Telegram)
2. Merge в целевую ветку
3. Deploy на сервер (PM2 restart)
4. Проверка healthcheck

**Выход:** Обновлённый бот работает

---

## 5. Keyboard Lifecycle (управление клавиатурой)

> Тип: внутренний процесс
> Владелец: Бот (State Machine + BaseState)

**Вход:** Переход между стейтами (SM `_transition()` / `go_to()`)

**Действие:**

1. SM engine проверяет `keyboard_type` у from_state и to_state
2. Если `reply → non-reply`: записывает `ReplyKeyboardRemove()` в `BaseState._pending_keyboard_cleanup[chat_id]`
3. Первый `send()` нового стейта применяет cleanup:
   - Без `reply_markup` → прикрепляет `ReplyKeyboardRemove` к сообщению
   - С `InlineKeyboardMarkup` → send+edit (отправляет с `ReplyKeyboardRemove`, затем `edit_reply_markup` для InlineKeyboard)
   - С `ReplyKeyboardMarkup` → пропускает cleanup (новая Reply-клавиатура заменяет старую)
4. Стейты с `keyboard_type = "reply"` также чистят клавиатуру вручную в `handle()` (defense-in-depth для нормального пути; SM auto-cleanup — safety net для command-bypass через `go_to()`)

**Выход:** Reply-клавиатура удалена при переходе в non-reply стейт. Inline-клавиатуры — self-cleaning через `edit_text()`.

**Типы:** `"inline"` (default, 13 стейтов), `"reply"` (4 стейта), `"none"` (2 стейта).

**При добавлении нового стейта:**
1. Установить `keyboard_type` в классе
2. Обновить таблицу в `CLAUDE.md § 10.5`
3. Если `reply`: добавить `ReplyKeyboardRemove()` к каждому exit-пути в `handle()`

---

## 6. Issue Triage (триаж замечаний → WP-7)

> Тип: внутренний процесс (двухуровневый)
> Владелец: R7 Триажёр техдолга (DP.AGENT.001)

**Два уровня триажа:**

| Уровень | Кто | Grade | Когда | Что делает |
|---------|-----|-------|-------|-----------|
| **Auto-triage** | Bot process (`core/feedback_triage.py`) | 1 | При каждом helpful=false / ✏️ comment | LLM classify (Haiku) → DB + TG alert |
| **Review** | Claude Code (сессия WP-7) | 3 | При открытии сессии техдолга | Читает предклассифицированный backlog → решения |

**Роли и исполнители:**

| Роль | Кто | Что делает |
|------|-----|-----------|
| **Auto-triage (R7 Grade 1)** | Bot process (Haiku) | helpful=false → classify (L/C/U/K) → feedback_triage DB → TG alert if high |
| **Поставщик отчёта** | Синхронизатор (bash) | unsatisfied-report.sh → unsatisfied-questions.md (weekly report) |
| **Поставщик intake** | Синхронизатор (bash) | code-scan → captures.md |
| **Поставщик intake** | Стратег (Note-Review) | маршрутизация заметок → fleeting-notes.md |
| **Поставщик intake** | Пользователь (TG) | `.баг ...` → fleeting-notes.md |
| **Review (R7 Grade 3)** | Claude Code (сессия WP-7) | feedback_triage DB + 2 файловых intake → решения |
| **Исполнитель (R6)** | Claude Code (сессия WP-7) | пишет код, фиксит баги по одобренному scope |

**Вход:** Открытие сессии техдолга (WP-7)

**Действие:**
1. Прочитать `unsatisfied-questions.md` — структурированный отчёт (замечания, urgent, кластеры)
2. Прочитать 2 файловых intake: fleeting-notes.md, captures.md
3. Прочитать текущий `WP-7-bot-tech-debt.md` backlog
4. Review предклассифицированных замечаний (category/severity/cluster уже в DB)
5. Оценить бюджет каждого (0.5h / 1h / 2h+)
6. Предложить: что берём, что отложить, что отклонить
7. Одобренные → WP-7 backlog, отработанные → `status='resolved'` в feedback_triage

**Выход:** Обновлённый WP-7 backlog + scope текущей сессии

**Триггер:** «Открываем сессию техдолга» / «WP-7» / «Давай разберём замечания»

---

*Последнее обновление: 2026-02-21*
