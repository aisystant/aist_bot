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

## Реализуемые обещания (SC)

| SC | Обещание | Сервисы бота |
|----|----------|--------------|
| [SC.003](../../PACK-digital-platform/pack/digital-platform/08-service-clauses/DP.SC.003-learning-and-development.md) | Обучение и развитие | S12 Q&A, S13 DZ-Check, S14 Content Pre-Gen, S15 Feed Delivery, S16 Marathon Step, S37 Bloom Eval |
| [SC.005](../../PACK-digital-platform/pack/digital-platform/08-service-clauses/DP.SC.005-content-publishing.md) | Публикация контента | S25 Daily Scan, S26 Scheduled Publish, S27 Manual Publish, S28 Comment Check |
| [SC.007](../../PACK-digital-platform/pack/digital-platform/08-service-clauses/DP.SC.007-triage-and-techdebt.md) | Триаж и техдолг | S29 Auto-Triage, S30 Triage Session |
| SC-19 (WP-74) | Конвейер обратной связи | S29+ (расширение), Наблюдатель, FAQ-автор, Баг-трекер |
| [SC.008](../../PACK-digital-platform/pack/digital-platform/08-service-clauses/DP.SC.008-self-healing.md) | Самовосстановление | S31 L1 Unstick, S32 L2 Auto-Fix, S33 L3 Restart, S34 L4 Escalate |
| [SC.012](../../PACK-digital-platform/pack/digital-platform/08-service-clauses/DP.SC.012-onboarding.md) | Онбординг | S12 (первый вопрос), Onboarding flow |

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

### 4.1. Ежедневная разработка (pilot)

**Правило Pilot-First:** Все изменения по умолчанию → ветка `pilot`. На `new-architecture` (прод) — ТОЛЬКО по прямой команде пользователя.

**Вход:** Задача из РП (WP-5, WP-7)
**Действие:** код → коммит → push в `pilot` → Railway auto-deploy (aist_pilot_bot)
**Выход:** Изменение на пилоте, в Close-отчёте: «залито на pilot, на new-architecture не заливалось»

### 4.2. Merge на прод (pilot → new-architecture)

**Триггер:** Команда пользователя «мержи на прод» / «заливай на new-architecture».
**Проверка Стратегом:** В Day Plan Стратег проверяет наличие незалитых коммитов (шаг 3c в day-plan.md).

**Действие:**
1. `git checkout new-architecture && git pull origin new-architecture`
2. `git log new-architecture..pilot --oneline` → показать список коммитов пользователю
3. Получить подтверждение
4. `git merge pilot` → разрешить конфликты (если есть)
5. `git push origin new-architecture`
6. Проверить Railway деплой прода (aist_me_bot)
7. Smoke test на проде

**Выход:** Прод обновлён, pilot и new-architecture синхронизированы

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

## 6. Конвейер обратной связи (Feedback Loop → Content Improvement)

> Тип: внутренний процесс (многоуровневый)
> Владелец: R7 Триажёр техдолга (DP.ROLE.001)
> Обещание: SC-19 (WP-74) — каждая жалоба учтена, классифицирована и доведена до изменения или осознанного отказа с объяснением
> АрхГейт: 58/70 (WP-178)
> Source-of-truth: WP-178 (DS-my-strategy/inbox/WP-178-feedback-loop-content-improvement.md)

### 6.1. Обзор конвейера

```
Точка контакта → Единый inbox → Классификация → Маршрутизация → Действие → Верификация
     ↓               ↓              ↓               ↓              ↓            ↓
  Бот 👎         feedback_       R7 Триажёр      K→FAQ/Pack     Агент или   Наблюдатель:
  Клуб 💬        unified       (Haiku, авто)    C→WP-7 (баг)   человек     кластер ↓?
  Ручной ввод                                   U/F→WP-5       вносит       Затык →
                                                L→инфра         изменение    эскалация
```

### 6.2. Уровни обработки

| Уровень | Кто | Grade | Когда | Что делает |
|---------|-----|-------|-------|-----------|
| **Auto-triage** | Bot process (`core/feedback_triage.py`) | 1 | При каждом helpful=false / ✏️ comment | LLM classify (Haiku) → `feedback_unified` DB + TG alert |
| **Наблюдатель** | Cron (`feedback-watchdog.sh`) | 2 | Ежедневно | Мониторинг кластеров, трендов, SLA → TG alert при затыке |
| **Review** | Claude Code (сессия WP-7) | 3 | При открытии сессии техдолга | Читает предклассифицированный backlog → решения |
| **FAQ-автор** | Cron (Ф1+) | 2 | Ежедневно (ночной) | Генерирует дополнения FAQ из кластеров → PR |
| **Баг-трекер** | Cron (Ф1+) | 2 | При category=C | Формирует issue → WP-7 backlog |

### 6.3. Роли и исполнители

| Роль | Кто | Что делает |
|------|-----|-----------|
| **R7 Триажёр (Grade 1)** | Bot process (Haiku) | helpful=false → classify (K/C/U/L/P/F) + severity + cluster + confidence + suggested_action → `feedback_unified` DB → TG alert if high/critical |
| **R30 Наблюдатель** | Cron (bash) | Ежедневно: кластеры >7 дней без уменьшения → TG alert тех. оператору. SLA compliance. Тренды |
| **R31 Эскалатор** | Event (Наблюдатель) | При затыке/зацикливании/SLA-нарушении → TG alert с контекстом |
| **R28 FAQ-автор** (Ф1+) | Cron (Sonnet) | Кластер category=K, ≥3 тикетов → генерирует FAQ дополнение → PR |
| **R29 Баг-трекер** (Ф1+) | Cron (Haiku) | category=C → структурированный issue → WP-7 backlog |
| **Поставщик отчёта** | Синхронизатор (bash) | `unsatisfied-report.sh` → отчёт (дельта, кластеры, urgent, lifecycle, SLA) |
| **Поставщик intake** | Синхронизатор/Стратег/Пользователь | code-scan → captures.md, заметки → fleeting-notes.md, `.баг` → fleeting-notes.md |
| **Review (R7 Grade 3)** | Claude Code (сессия WP-7) | feedback_unified DB + intake → решения |
| **Исполнитель (R6)** | Claude Code / Разработчик | Код, фиксы по одобренному scope |
| **Тех. оператор** | Человек | Мониторит дашборд, разбирает эскалации, переклассифицирует при низкой confidence |

### 6.4. Lifecycle тикета

```
new → classified → routed → in_progress → resolved → confirmed
                     ↓                        ↑          ↓
                  wontfix                   reopen    (closed)
                  deferred ──(дата)──→ routed
```

| Статус | Кто ставит | Когда |
|--------|-----------|-------|
| **new** | Bot (callbacks.py) | Пользователь нажал 👎 или ✏️ |
| **classified** | R7 Триажёр (Haiku) | Автоклассификация завершена (<1 мин) |
| **routed** | R7 Триажёр / Тех. оператор | confidence ≥ 0.7 → авто; < 0.7 → ручная маршрутизация |
| **in_progress** | Агент (R28/R29) / Исполнитель | PR создан, issue открыт, работа начата |
| **resolved** | Исполнитель / Тех. оператор | Изменение внесено (merge, fix, FAQ дополнен) |
| **confirmed** | R30 Наблюдатель | Через 7 дней: кластер уменьшился, повторных жалоб нет |
| **wontfix** | Тех. оператор | Осознанное решение не исправлять. **Обязательно:** resolution_note с объяснением |
| **deferred** | Тех. оператор | Отложено с датой пересмотра. R30 напомнит |

### 6.5. Категории (расширение L/C/U/K)

| Код | Категория | Маршрут | Пример |
|-----|-----------|---------|--------|
| **K** | Knowledge (знание) | FAQ/Pack → R28 FAQ-автор | «Бот не знает про X» |
| **C** | Correctness (баг) | WP-7 → R29 Баг-трекер | «Бот ответил неправильно» |
| **U** | Usability (UX) | WP-5 backlog | «Непонятно как сделать X» |
| **L** | Latency (скорость) | Инфраструктура | «Долго отвечает» |
| **P** | Process (процесс) | PROCESSES.md | «Процесс X не работает» |
| **F** | Feature (фича) | WP-5 backlog | «Хочу чтобы бот умел X» |

### 6.6. SLA и эскалация

| Условие | SLA | Эскалация |
|---------|-----|-----------|
| Любой тикет → classified | < 1 мин (авто) или < 24h (ручная) | — |
| severity = 1 (критический) | Действие < 48h | Немедленный TG-алерт |
| Тикет в routed > 14 дней | — | Автоэскалация → тех. оператор |
| Кластер не уменьшается > 7 дней | — | R30 → R31: «⚠️ Затык» |
| Тикет resolved → reopen > 2 раз | — | R30 → R31: «🔄 Зацикливание, переклассификация?» |

### 6.7. Процесс сессии триажа (Review, Grade 3)

**Вход:** Открытие сессии техдолга (WP-7)

**Действие:**
1. Прочитать `unsatisfied-questions.md` — структурированный отчёт (замечания, urgent, кластеры, lifecycle, SLA)
2. Прочитать 2 файловых intake: fleeting-notes.md, captures.md
3. Прочитать текущий `WP-7-bot-tech-debt.md` backlog
4. Review предклассифицированных тикетов (`feedback_unified` — category/severity/cluster/suggested_action)
5. Оценить бюджет каждого (0.5h / 1h / 2h+)
6. Предложить: что берём, что отложить, что отклонить
7. Одобренные → WP-7 backlog, отработанные → `status='resolved'` + `resolution_note` в feedback_unified

**Выход:** Обновлённый WP-7 backlog + scope текущей сессии

**Триггер:** «Открываем сессию техдолга» / «WP-7» / «Давай разберём замечания»

### 6.8. Метрики (дашборд в unsatisfied-report)

| Метрика | Формула | Цель |
|---------|---------|------|
| **MTTR** | avg(resolved_at - created_at) | ↓ снижать |
| **Cluster trend** | cluster_size(t) vs cluster_size(t-7d) | ↓ снижать |
| **Reopen rate** | reopened / resolved | < 10% |
| **SLA compliance** | % тикетов в рамках SLA | > 95% |
| **Auto-resolve rate** (Ф1+) | resolved_by_agent / total_resolved | ↑ повышать |
| **Confidence accuracy** | agent_category == operator_category / total | ↑ повышать |

### 6.9. Фазы развития

| Фаза | Что | Статус |
|------|-----|--------|
| **Текущее** | Auto-triage (Haiku) + unsatisfied-report + Review (WP-7 сессия) | ✅ работает |
| **Ф0.5** (W14) | Миграция → feedback_unified + lifecycle + suggested_action + Наблюдатель v0 | pending |
| **Ф1** (W15) | R28 FAQ-автор + R29 Баг-трекер + self-learning промпт + метрики | pending |
| **Ф2** (Q2) | Мультиканальный сбор (клуб, ручной ввод) + уведомление пользователя | pending |
| **Ф3** (Q3) | Self-learning agents + Help-desk MVP | pending |

> Детали фаз: WP-178 context file (DS-my-strategy/inbox/WP-178-feedback-loop-content-improvement.md)

---

## 7. Kids Curriculum Sync (обновление детского контента)

> Тип: внутренний процесс (ручной, ~10 мин)
> Владелец: Разработчик
> Триггер: Денис Асфандияров обновил карточки в `kids-learning-pack`

**Архитектура:**
```
kids-learning-pack (Денис, Pack)  →  DS-principles-curriculum (DS)  →  aist_bot (DS-instrument)
```
Pack = source-of-truth детской методики. DS-principles-curriculum — промежуточный слой.
Бот получает данные через `kids_cells.json` (физическая копия, не живая ссылка).

**Вход:** Денис добавил или изменил карточки эпизодов в `github.com/asf-denis-system/kids-learning-pack`

**Действие:**
1. `cd ~/IWE/DS-principles-curriculum`
2. `python3 scripts/extract_kids_cells.py`
   - Скрипт читает Z0-Z7 карточки (preschool + school) через GitHub API
   - Извлекает: сценарий, can_do, transfer_test, criteria, common_errors
   - Запускает валидацию: ✅ OK → записывает, ❌ FAILED → сообщает о проблеме без записи
3. Если валидация прошла:
   - `cp data/curriculum/kids_cells.json ~/IWE/DS-IT-systems/aist_bot_newarchitecture/data/curriculum/kids_cells.json`
   - Коммит в `pilot`: `git add data/curriculum/kids_cells.json && git commit -m "update kids_cells from Denis pack"`
   - Push → Railway auto-deploy пилота
4. Проверить что `load_kids_cells()` вернула 8 принципов (логи Railway)

**Выход:** Обновлённый `kids_cells.json` на пилоте, бот использует свежие карточки Дениса

**Если валидация FAILED:**
- Смотреть на ❌ строки — они указывают на переименованную секцию в карточке
- Открыть нужный файл в `kids-learning-pack`: `gh api repos/asf-denis-system/kids-learning-pack/contents/03-methods/...`
- Найти новое название секции → обновить regex в `extract_kids_cells.py`
- Перезапустить

**Периодичность:** по факту обновлений от Дениса, не реже раза в месяц

---

## 8. DT Engagement Sync (WP-85 Phase 4)

**Вход:** Ежедневный cron 04:30 MSK (scheduler) ИЛИ `/dt_sync` dev-команда

**Источник:** `development.engagement` SQL View (15 метрик из `user_events`)

**Приёмник:** `digital_twins` таблица (JSONB), та же Neon DB. DT MCP читает при запросе пользователя.

**Поток данных:**

```
development.user_events (append-only, все каналы)
        ↓ SQL View
development.engagement (15 агрегатов на user_uuid)
        ↓ sync_engagement_to_dt()
digital_twins.data JSONB (INSERT ON CONFLICT → deep merge)
        ↓ DT MCP read()
Пользователь видит проекции через /twin или MCP-клиент
```

**4 группы проекций (выравнены с метамоделью `2_collected/`):**

| Группа | Индикаторы |
|--------|------------|
| `2_1_account` | sessions_total, first_event_at, last_event_at, events_total |
| `2_2_courses` | marathon_steps_total, feed_completed_total |
| `2_3_practice` | training_attempts_total, training_passed_total, assessments_total, marathon_tasks_total |
| `2_4_time` | active_days, events_last_7d, events_last_30d, ai_chats_total |

**Файлы:** `db/queries/dt_sync.py`, `core/scheduler.py` (line ~107)

**Фильтрация:** `WHERE user_uuid IS NOT NULL` — только T1+ (с Ory UUID). T0-пользователи копят события по chat_id; при получении Ory UUID следующий sync подхватит автоматически.

**Выход:** Лог `[Scheduler] DT engagement sync: {synced: N, skipped: N, errors: N}`

---

*Последнее обновление: 2026-03-27*
