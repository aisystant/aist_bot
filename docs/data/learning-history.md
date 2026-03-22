# Learning History (WP-149)

> **Подход C:** Event-sourced + Materialized Table (CQRS).
> **АрхГейт:** 8.4/10 (CONDITIONAL PASS).

## Архитектура

```
Слой 1: development.user_events (append-only, source of truth)
    → log_learning_completed() / log_event('learning_completed', ...)
         ↓ trigger (development.materialize_learning)
Слой 2: development.learning_history (материализованная таблица)
    → типизированные колонки + CHECK constraints
         ↓ dt_sync.py (ежедневный cron)
Слой 3: digital_twins → 2_10_learning_history (JSONB в ЦД)
```

**Принцип:** trigger никогда не роняет транзакцию. Ошибки → RAISE WARNING. Events всегда сохраняются. Replay восстанавливает learning_history.

## Event Payload Contract

```python
# event_type = 'learning_completed'
# _schema_version: обязателен. При изменении — инкремент.

{
    '_schema_version': 1,       # int, обязателен

    # Что прошёл
    'program_id': str,          # 'PD-PROGRAM-V2' — обязателен
    'module_id': str | None,    # 'F1'..'F4' (фаза программы)
    'direction': int,           # 1-6 (направление из SOP.001 §Матрица)
    'topic_id': str,            # 'ZP.1', 'FPF.A.1' — обязателен
    'bloom_level': int,         # 1-5 (глубина Bloom)
    'cell_id': str | None,      # 'ZP.1@2' (если из готовой ячейки)

    # Результат
    'score': float | None,      # 0.0–1.0
    'passed': bool,             # can-do подтверждены?
    'errors': list[str],        # коды ошибок (пустой = нет ошибок)
    'duration_min': int | None, # длительность в минутах

    # Контекст сборки (от Портного, для аудита)
    'tailor_context': {         # dict | None
        'phase': int,           # 1-4
        'student_stage': int,   # 0-4
        'it_level': int,        # 0-3
        'state': str,           # chaos|stuck|turn|development
        'energy': int,          # 1-5
    },
}
```

## Направления (direction 1–6)

| # | Направление | EN |
|---|---|---|
| 1 | Картины мира и мировоззрение | worldviews |
| 2 | Навыки и мастерство | skills |
| 3 | Ограничения и узкие горлышки | limitations |
| 4 | Экзокортекс и ИИ | exocortex |
| 5 | Окружение, культура и рабочие системы | environment |
| 6 | Организм и личные ресурсы | organism |

## Таблица development.learning_history

| Колонка | Тип | Constraint | Описание |
|---|---|---|---|
| id | SERIAL PK | | |
| user_id | BIGINT NOT NULL | | chat_id |
| user_uuid | UUID | | Ory UUID (для dt_sync) |
| program_id | TEXT NOT NULL | | ID программы |
| module_id | TEXT | | Фаза программы |
| direction | SMALLINT NOT NULL | CHECK 1–6 | Направление |
| topic_id | TEXT NOT NULL | | Тема/принцип |
| bloom_level | SMALLINT NOT NULL | CHECK 1–5 | Глубина Bloom |
| cell_id | TEXT | | Ячейка из curriculum |
| score | REAL | CHECK 0.0–1.0 | Оценка |
| passed | BOOLEAN NOT NULL | DEFAULT FALSE | Пройдено |
| errors | TEXT[] | DEFAULT '{}' | Коды ошибок |
| duration_min | SMALLINT | | Длительность |
| tailor_context | JSONB | | Контекст сборки |
| source | TEXT NOT NULL | CHECK enum | Продьюсер |
| event_id | BIGINT NOT NULL | UNIQUE | Ссылка на user_events |
| schema_version | SMALLINT NOT NULL | DEFAULT 1 | Версия payload |
| created_at | TIMESTAMP NOT NULL | | UTC |

## Индексы

| Индекс | Колонки | Partial | Для чего |
|---|---|---|---|
| idx_lh_user | user_id | — | Основной фильтр |
| idx_lh_user_topic | user_id, topic_id | — | Поиск по теме |
| idx_lh_user_dir | user_id, direction | — | Портной Шаг 3 |
| idx_lh_user_dir_passed | user_id, direction | WHERE passed | Портной gap-анализ |
| idx_lh_user_passed | user_id | WHERE passed | bottleneck-first |
| idx_lh_created | created_at | — | Range queries |

## dt_sync: группа 2_10_learning_history

```json
{
  "topics_completed": 12,
  "topics_passed": 10,
  "last_topic": "ZP.1",
  "last_completed_at": "2026-03-22T14:30:00",
  "depths_by_direction": {
    "1": {"max_bloom": 2, "attempts": 5, "passed": 4, "last_passed_at": "..."},
    "2": {"max_bloom": 1, "attempts": 2, "passed": 2, "last_passed_at": "..."}
  },
  "history": [
    {
      "topic_id": "ZP.1", "bloom_level": 2, "score": 0.85,
      "passed": true, "direction": 1, "errors": [], "date": "..."
    }
  ]
}
```

## Использование

```python
from db.queries.events import log_learning_completed

# Типизированный helper (рекомендуется)
await log_learning_completed(
    user_id=chat_id,
    topic_id='ZP.1',
    direction=1,
    bloom_level=2,
    score=0.85,
    passed=True,
    errors=[],
    duration_min=12,
    tailor_context={
        'phase': 1, 'student_stage': 0,
        'it_level': 1, 'state': 'development', 'energy': 4,
    },
)
```

## Replay (восстановление)

```sql
TRUNCATE development.learning_history;
-- Затем: python-скрипт или SQL (см. миграцию 007)
```

## Миграция

```bash
python -m db.migrations.007_create_learning_history
```

---

*Создан: 2026-03-22 | WP-149 Ф0→Ф1*
