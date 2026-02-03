# План миграции на State Machine

> Цель: перевести бота на модульную архитектуру State Machine.
> Ветка: `new-architecture`

---

## Правильный порядок миграции

```
Этап 0: Интеграция Claude API в State Machine  ← ТЕКУЩИЙ
    ↓
Этап 1: Тестирование State Machine (USE_STATE_MACHINE=true)
    ↓
Этап 2: Очистка bot.py (удаление дублирующего кода)
    ↓
Этап 3: Полная миграция handlers → states
```

---

## Этап 0: Интеграция Claude API в State Machine (ПРИОРИТЕТ)

**Проблема:** Стейты Марафона содержат TODO-заглушки, генерация контента не работает.

**Задача:** Интегрировать `clients/claude.py` в стейты.

### 0.1 Файлы для интеграции

| Файл | TODO | Метод Claude |
|------|------|--------------|
| `states/workshops/marathon/lesson.py:96` | `# TODO: Интеграция с claude.generate_content()` | `claude.generate_content(topic, intern, mcp_client, knowledge_client)` |
| `states/workshops/marathon/question.py:82` | `# TODO: Интеграция с claude.generate_question()` | `claude.generate_question(topic, intern, bloom_level)` |
| `states/workshops/marathon/task.py:79` | `# TODO: Интеграция с claude.generate_practice_intro()` | `claude.generate_practice_intro(topic, intern)` |

### 0.2 Что нужно сделать

1. **Импортировать клиенты:**
   ```python
   from clients import claude, mcp_guides, mcp_knowledge
   ```

2. **Получить данные темы:**
   ```python
   from core.knowledge import get_topic_by_index
   topic = get_topic_by_index(topic_index)
   ```

3. **Вызвать генерацию:**
   ```python
   content = await claude.generate_content(topic, intern, mcp_guides, mcp_knowledge)
   ```

4. **Обработать результат и отправить пользователю**

### 0.3 Критерии готовности

- [ ] `lesson.py` генерирует теорию через Claude API
- [ ] `question.py` генерирует вопрос через Claude API
- [ ] `task.py` генерирует введение к практике через Claude API
- [ ] Бот работает с `USE_STATE_MACHINE=true`
- [ ] Smoke test: `/learn` показывает сгенерированный контент

---

## Этап 1: Тестирование State Machine

**Задача:** Убедиться, что State Machine полностью работоспособен.

### 1.1 Тесты

| Сценарий | Команда | Ожидаемый результат |
|----------|---------|---------------------|
| Урок | `/learn` | Сгенерированная теория + вопрос |
| Ответ на вопрос | Текст ответа | Обратная связь + задание |
| Практика | Текст РП | Подтверждение + следующий день |
| Лента | `/feed` | Выбор тем / дайджест |

### 1.2 Критерии готовности

- [ ] Все сценарии Марафона работают
- [ ] Все сценарии Ленты работают
- [ ] Нет регрессий по сравнению с bot.py

---

## Этап 2: Очистка bot.py

**Задача:** Удалить дублирующий код после успешной миграции.

### 2.1 Что удалить из bot.py

| Секция | Строки | Причина удаления |
|--------|--------|------------------|
| ClaudeClient | 698-1105 | Дублирует `clients/claude.py` |
| MCPClient | 1109-1268 | Дублирует `clients/mcp.py` |
| БД (init_db, PostgresStorage) | 265-595 | Дублирует `db/` |
| Хелперы (get_intern, etc.) | 481-695 | Дублирует `db/queries/` |

### 2.2 Критерии готовности

- [ ] bot.py < 2000 строк
- [ ] Нет дублирования классов
- [ ] Бот работает без регрессий

---

## Этап 3: Полная миграция handlers → states

**Задача:** Перенести оставшиеся handlers в модульную структуру.

### 3.1 Handlers для миграции

```
handlers/
├── onboarding.py      → states/common/start.py
├── learning.py        → states/workshops/marathon/
├── profile.py         → states/common/profile.py
├── progress.py        → states/common/progress.py
└── fallback.py        → states/common/error.py
```

### 3.2 Критерии готовности

- [ ] bot.py содержит только main() и инициализацию
- [ ] Все handlers вынесены в states/
- [ ] State Machine — единственная точка обработки сообщений

---

## Текущий статус

| Этап | Статус | Дата |
|------|--------|------|
| Этап 0: Интеграция Claude | **В РАБОТЕ** | 2026-01-29 |
| Этап 1: Тестирование | Ожидает | - |
| Этап 2: Очистка bot.py | Ожидает | - |
| Этап 3: Миграция handlers | Ожидает | - |

---

## Сравнение: Лента vs Марафон

| Компонент | Лента | Марафон |
|-----------|-------|---------|
| Архитектура | `engines/feed/` | `states/workshops/marathon/` |
| Интеграция Claude | ✅ Работает | ❌ TODO-заглушки |
| Статус | Production-ready | Требует интеграции |

**Почему Лента работает:** `engines/feed/planner.py` напрямую вызывает Claude API.

**Почему Марафон не работает:** Стейты созданы как каркас, интеграция не завершена.

---

## Связанные документы

- `docs/plans-and-reports/2026-01-29-progress.md` — отчёт о прогрессе
- `docs/processes/process-02-content-generation.md` — процесс генерации контента
- `states/README.md` — документация State Machine
