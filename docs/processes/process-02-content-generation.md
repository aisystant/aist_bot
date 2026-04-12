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
   calc_words(study_duration, bloom_level) → word_count
   ├─ 5 мин  → 300–510 слов  (bloom 1–3)
   ├─ 15 мин → 900–1530 слов (bloom 1–3)
   └─ 25 мин → 1500–2550 слов (bloom 1–3)
   Детали формулы: §7 (Content Budget Model)

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

## 7. Content Budget Model (DP.D.027)

Модель из Pack (`DP.D.027`) задаёт, сколько слов и какого стиля должен быть сгенерированный контент. **Три независимые оси** — нельзя смешивать:

| Ось | Что определяет | Как реализована |
|-----|----------------|-----------------|
| **Ось 1 — Длина** | Целевое число слов | `calc_words(duration, bloom)` = `duration × WPM_BASE × BLOOM_MULTIPLIER[bloom]` |
| **Ось 2 — Глубина** | Стиль изложения (доступный / профессиональный / экспертный) | `BLOOM_INSTRUCTION[bloom]` — отдельная инструкция в system prompt |
| **Ось 3 — Персонализация** | Профиль пользователя, assessment state, DT tier context | `_build_user_profile()` + TIER_PIPELINE ([P-08 § 6](process-08-self-knowledge.md)) |

### 7.1. Константы (`config/settings.py:230-248`)

```python
WPM_BASE = 60  # слов/мин — базовая скорость чтения учебного текста

BLOOM_MULTIPLIER = {1: 1.0, 2: 1.3, 3: 1.7}

BLOOM_INSTRUCTION = {
    1: "Объясни доступно, без терминов. Примеры из повседневной жизни.",
    2: "Используй профессиональную терминологию. Показывай связи между понятиями.",
    3: "Экспертный уровень. Критический анализ, неочевидные аспекты, ссылки на источники.",
}

def calc_words(duration_minutes: int, bloom_level: int = 1) -> int:
    bl = max(1, min(bloom_level, 3))
    return int(duration_minutes * WPM_BASE * BLOOM_MULTIPLIER.get(bl, 1.0))
```

### 7.2. Расчёт для типовых случаев

| duration | bloom=1 | bloom=2 | bloom=3 |
|----------|---------|---------|---------|
| 5 мин | 300 | 390 | 510 |
| 15 мин | 900 | 1170 | 1530 |
| 25 мин | 1500 | 1950 | 2550 |

**Важно:** источник правды — `calc_words()`. Таблица § 1.1 обновлена и отражает диапазоны по bloom-уровням.

### 7.3. Правило раздельности осей

**Bloom НЕ ДОЛЖЕН смешиваться между осями:**

- ❌ `"Уровень bloom=3 → пиши длиннее и профессиональнее"` — смешение Осей 1 и 2 в одной инструкции
- ✅ Ось 1: `words = calc_words(duration, bloom)` → передаётся в system prompt как `~{words} слов`
- ✅ Ось 2: `BLOOM_INSTRUCTION[bloom]` → отдельная строка в system prompt, управляющая стилем

Так расчёт остаётся воспроизводимым: при дебаге можно изолированно проверить длину (Ось 1) или стиль (Ось 2), не распутывая комбинированную инструкцию.

### 7.4. Auto-upgrade bloom

`BLOOM_AUTO_UPGRADE_AFTER` (в settings.py) — через сколько успешных тем автоматически поднимается bloom-уровень пользователя. Реализация в `states/workshops/marathon/question.py` (assessment flow). Правило: bloom растёт не от желания user, а от демонстрации освоенности.

### 7.5. Связь с `generate*()` методами

- `generate_content(topic, intern, marathon_day, ...)` — `words = calc_words(intern['study_duration'], intern['bloom_level'])`, подставляется в system prompt
- `generate_question(topic, intern, marathon_day, bloom_level)` — bloom передаётся напрямую для выбора `question_templates` из topic YAML
- `generate_practice_intro(topic, intern)` — bloom не используется (практика не имеет уровней), только language для перевода

**Adaptive max_tokens** (§9 CLAUDE.md): `min(words × 1.5, 4096)` в `generate_content`. Не hardcode 4000 — масштабируется под Ось 1.

---

## 8. Ключевые файлы

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
| 2026-04-11 | Добавлена §7: Content Budget Model DP.D.027 (3 оси, `calc_words`, `BLOOM_MULTIPLIER`, `BLOOM_INSTRUCTION`) |
| 2026-02-02 | Refactoring: ClaudeClient удалён из bot.py, используется импорт из clients/claude.py |
| 2026-01-29 | Добавлена секция 3: генерация практики с переводом |
| 2026-01-22 | Создание документа |
