# P-02 Генерация контента

> Процесс генерации материалов урока и вопросов через Claude и MCP.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Тип | Процесс |
| Модель | Claude Sonnet 4 |
| MCP серверы | Guides, Knowledge |

---

## 1. Генерация материала урока

### Функция generate_content()

**Входные параметры:**
- `topic` — структура темы из `knowledge_structure.yaml`
- `intern` — профиль пользователя
- `marathon_day` — день марафона (для ротации)
- `mcp_client` — клиент MCP-Guides
- `knowledge_client` — клиент MCP-Knowledge

### Процесс генерации

```
1. Определение объёма
   study_duration → word_count
   ├─ 5 мин  → 500 слов
   ├─ 15 мин → 1500 слов
   └─ 25 мин → 2500 слов

2. Загрузка метаданных темы
   topics/{topic_id}.yaml
   ├─ guides_mcp: ["запрос1", "запрос2"]
   └─ knowledge_mcp: ["запрос1", "запрос2"]

3. Поиск в MCP-Guides
   ├─ semantic_search(query, lang="ru", limit=2)
   ├─ До 3 запросов
   └─ До 5 фрагментов (1500 символов каждый)

4. Поиск в MCP-Knowledge
   ├─ search(query, sort_by="created_at:desc")
   ├─ До 3 запросов
   └─ До 5 фрагментов с датами

5. Формирование промпта
   ├─ System: профиль + правила + ONTOLOGY
   └─ User: тема + pain_point + MCP контекст

6. Вызов Claude API
   └─ Персонализированный материал
```

### Персонализация

| Поле профиля | Использование |
|--------------|---------------|
| `study_duration` | Объём текста (слова) |
| `name` | Обращение в тексте |
| `occupation` | Примеры 1-го уровня |
| `interests` | Примеры из хобби |
| `motivation` | Мотивационный блок |
| `goals` | "Как это поможет достичь..." |
| `language` | Язык генерации |

### System prompt

```
Ты — персональный наставник по системному мышлению.

ПРОФИЛЬ СТАЖЕРА:
- Имя: {name}
- Занятие: {occupation}
- Интересы: {interests}
- Что важно: {motivation}
- Что изменить: {goals}
- Время: {duration} мин (~{words} слов)

ИНСТРУКЦИИ:
1. Показать, как тема поможет достичь goals
2. Опираться на motivation
3. Правильный объём слов
4. Примеры: работа → хобби → далёкая сфера

СТРОГО ЗАПРЕЩЕНО:
- Добавлять вопросы
- Заголовки "Вопрос:"
- Заканчивать вопросом

{ONTOLOGY_RULES}
```

### User prompt

```
Тема: {title}
Основное понятие: {main_concept}
Связанные понятия: {related_concepts}

Боль читателя: {pain_point}
Ключевой инсайт: {key_insight}

{content_prompt из метаданных}

КОНТЕКСТ ИЗ AISYSTANT:
{MCP_CONTEXT}

Начни с признания боли, раскрой тему, подведи к инсайту.
```

---

## 2. Генерация вопроса урока

### Функция generate_question()

**Входные параметры:**
- `topic` — структура темы
- `intern` — профиль
- `marathon_day` — день (для ротации контекстов)
- `bloom_level` — уровень сложности (1/2/3)

### Уровни сложности

| Уровень | Тип | Примеры вопросов |
|---------|-----|-----------------|
| 1 | Различение | "В чём разница между X и Y?" |
| 2 | Понимание | "Почему X важен для Y?" |
| 3 | Применение | "Приведите пример X из практики" |

### Ротация контекстов по дню

| День | Контекст |
|------|----------|
| 1 | Профессия (occupation) |
| 2 | Интересы (interests) |
| 3 | Повседневная жизнь |
| 4 | Отношения с людьми |
| 5 | Личное развитие |
| 6 | Принятие решений |
| 7+ | Цикл повторяется |

### Шаблоны вопросов

Загружаются из метаданных темы:
```yaml
time_levels:
  5:
    bloom_1:
      question_templates:
        - "В чём разница между X и Y?"
  15:
    bloom_2:
      question_templates:
        - "Как вы понимаете X?"
  25:
    bloom_3:
      question_templates:
        - "Приведите пример..."
```

### Ограничения промпта

- Только вопрос (1-3 предложения)
- Без введения
- Без заголовков "Вопрос:"
- Без примеров
- Ничего после вопроса

---

## 3. Генерация практического задания

### Функция generate_practice_intro()

**Входные параметры:**
- `topic` — структура темы из `knowledge_structure.yaml`
- `intern` — профиль пользователя (включая `language`)

**Выходные данные (dict):**
- `intro` — введение к заданию (2-4 предложения)
- `task` — переведённое задание
- `work_product` — переведённый рабочий продукт
- `examples` — переведённые примеры РП

### Процесс генерации

```
1. Получить исходные данные на русском
   topic.get('task')
   topic.get('work_product')
   topic.get('wp_examples')

2. Определить язык пользователя
   intern.get('language') → ru/en/es/fr

3. Сформировать промпт с требованием перевода
   "Переведи и адаптируй всё на целевой язык"

4. Вызвать Claude API
   → Структурированный ответ в формате:
   INTRO: ...
   TASK: ...
   WORK_PRODUCT: ...
   EXAMPLES: ...

5. Распарсить ответ в dict

6. Fallback: если парсинг не удался, вернуть оригинал на русском
```

### Формат промпта

**System:**
```
Ты — персональный наставник по системному мышлению.
{персонализация}

{lang_instruction} ← "Write EVERYTHING in English" и т.д.

Выдай ответ СТРОГО в формате:
INTRO: ...
TASK: ...
WORK_PRODUCT: ...
EXAMPLES: ...
```

**User:**
```
Тема: {title}
Понятие: {main_concept}

ИСХОДНЫЕ ДАННЫЕ (переведи на целевой язык):
Задание: {task_ru}
Рабочий продукт: {work_product_ru}
Примеры РП:
{examples_ru}
```

### Использование в task.py

```python
practice_data = await claude.generate_practice_intro(topic, intern)

task_text = practice_data.get('task', '') or topic.get('task')
work_product = practice_data.get('work_product', '') or topic.get('work_product')
```

### Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `clients/claude.py` | `generate_practice_intro()` |
| `states/workshops/marathon/task.py` | Использует переведённые данные |

---

## 4. MCP поиск контекста

### MCP-Guides

```python
await mcp_client.semantic_search(
    query="запрос",
    lang="ru",
    limit=2
)
```

**Результат:** Фрагменты из руководств Aisystant.

### MCP-Knowledge

```python
await knowledge_client.semantic_search(
    query="запрос",
    lang="ru",
    limit=2,
    sort_by="created_at:desc"  # Свежие первыми
)
```

**Результат:** Посты с датами `[2026-01-22] текст`.

---

## 5. ONTOLOGY_RULES

Правила терминологии в промпте:

| Правило | Верно | Неверно |
|---------|-------|---------|
| СИСТЕМА | Объект с элементами | Метод, процесс |
| РАБОЧИЙ ПРОДУКТ | Существительное, артефакт | Глагол, действие |
| ЦЕЛЬ | Результат | Средство (ИИ, CRM) |
| ФУНКЦИЯ | Что делает | Чем является |
| РОЛЬ | Функциональная позиция | Человек |
| ПРОБЛЕМА | Корневая причина | Симптом |
| СОСТОЯНИЕ | Статическое | Процесс |

---

## 6. Диаграмма

```
Марафон (день N)
    ↓
generate_content()
    ├─ Профиль (study_duration, name, occupation, motivation, goals)
    ├─ Метаданные темы (search_keys, content_prompt)
    ├─ MCP-Guides → контекст руководств
    ├─ MCP-Knowledge → свежие посты
    └─ Claude → персональный материал
    ↓
generate_question()
    ├─ Профиль (complexity_level, occupation, interests)
    ├─ Ротация контекстов (по дню)
    ├─ Метаданные (question_templates)
    └─ Claude → 1-3 предложения
    ↓
Ученику: Материал + Вопрос
```

---

## 7. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `clients/claude.py` | Claude API клиент: generate_content, generate_question, generate_practice_intro |
| `clients/mcp.py` | MCP клиенты (Guides, Knowledge) |
| `states/workshops/marathon/lesson.py` | State Machine: генерация урока |
| `states/workshops/marathon/question.py` | State Machine: генерация вопроса |
| `states/workshops/marathon/task.py` | State Machine: генерация практики с переводом |
| `core/knowledge.py` | Работа с темами из knowledge_structure.yaml |
| `config/settings.py` | ONTOLOGY_RULES |
| `topics/*.yaml` | Метаданные тем |

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-01-29 | Добавлена секция 3: генерация практики с переводом |
| 2026-01-22 | Создание документа |
