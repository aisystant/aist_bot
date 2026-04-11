# P-08 Self-Knowledge

> Трёхуровневая модель самознания бота: как он отвечает на вопросы про себя и свои сценарии. Быстрый путь (FAQ, ~100ms) → полный путь (Sonnet + tools, ~3-8s). Бот — surface view над Pack'ом, не хранит знания о себе в коде.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Тип | Процесс (pipeline сборки system prompt) |
| Источник | WP-7 W12 (anti-hallucination), WP-209 Ф2b (IWE instructions), коммит `a9473d2` (FAQ порог + pre-search auth) |
| Файлы | `core/self_knowledge.py` (484), `engines/shared/context_pipeline.py` (339) |
| Source-of-truth | `PACK-digital-platform/.../DP.AISYS.014-aist-bot.md` |
| Архитектурное правило | Бот НЕ знает о себе в коде. Идентичность, сценарии, FAQ — в Pack. Бот читает проекцию. |

---

## 1. Архитектурная граница

**Бот — surface view над Pack.** Identity, сценарии, FAQ, troubleshooting, integrations, программы обучения — всё приходит из Pack-паспорта `DP.AISYS.014-aist-bot.md` через Синхронизатор.

```
PACK-digital-platform/              Бот
  DP.AISYS.014-aist-bot.md          ┌─────────────────────────┐
    ├── Идентичность                │ core/self_knowledge.py  │
    ├── Сценарии (таблица)          │   _parse_pack()         │
    ├── FAQ (таблица)      ──Sync── │     ↓                   │
    ├── Troubleshooting             │   _scenarios, _faq,     │
    ├── Integrations                │   _troubleshooting,     │
    └── Platform / Programs         │   _identity, _platform  │
                                    └─────────────────────────┘
         ↓                                    ↓
DS-synchronizer/pack-project.sh         get_self_knowledge(lang)
         ↓                                    ↓
config/self_knowledge_projection.yaml   match_faq(question, lang)
```

**Что владеет бот:**
- ✅ Код pipeline (парсинг, кеширование, матчинг keywords, сборка system prompt)
- ✅ `config/self_knowledge_projection.yaml` — auto-generated, read-only

**Что НЕ владеет бот:**
- ❌ Текст идентичности, описания сценариев, FAQ-ответы — это Pack
- ❌ Список сценариев с флагом ✅ — это Pack (парсер берёт только ✅)
- ❌ Self-Knowledge НЕ включает код, данные профиля пользователя или результаты MCP-поиска (это другие слои pipeline)

---

## 2. Четыре уровня скорости ответа

| Уровень | Латентность | Что делает | Функция |
|---------|-------------|-----------|---------|
| **L0 Structured** | ~0ms | YAML-данные марафона из RAM (точные числа: сколько тем, дней) | `structured_lookup()` |
| **L1 FAQ** | ~100ms | Keyword match в FAQ/Troubleshooting из Pack-проекции | `match_faq()` |
| **L2 MCP-lite** | ~1-3s | (TODO, не реализован) Pack через MCP + быстрая модель | — |
| **L3 Full** | ~3-8s | Sonnet + tools (`search_knowledge`, `search_guides`, `get_bot_info`) | `consultation.py` L3 pipeline |

**Порядок в consultation (`states/common/consultation.py:580-620`):**

1. **Early role detection** — если вопрос для R27 Навигатор / R28 Диагност → пропустить L1/L0 FAQ, сразу L3
2. **L0 Structured** — точные данные марафона (сколько тем, день, длительность) → `structured_context`
3. **L1 FAQ** — `match_faq(question, lang)` → готовый ответ из Pack
4. **L3 Full** — `bot_context = get_self_knowledge(lang)` + `context_pipeline.assemble_context(tier=...)` → Sonnet с tools

**Bypass L1:** `deep_search` (префикс «?»), `is_refinement` (кнопка «Подробнее»), `_skip_faq` (role detected), `structured_hit` (L0 попал).

---

## 3. Загрузка self-knowledge: приоритеты

`_parse_pack()` (lazy, один раз на процесс):

```
L0 Projection                 config/self_knowledge_projection.yaml
(приоритет)                   ↓ есть → parse → return
                              ↓ нет
Local Pack                    ~/IWE/PACK-digital-platform/.../DP.AISYS.014-aist-bot.md
(dev fallback)                ↓ есть → parse → return
                              ↓ нет
GitHub raw URL                raw.githubusercontent.com/.../DP.AISYS.014-aist-bot.md
(prod fallback)               ↓ есть → parse → return
                              ↓ нет
Пустой кеш                    _scenarios=[], _faq=[] — WARNING в лог
```

**Source-of-truth:** `config/self_knowledge_projection.yaml` — read-only, генерируется Синхронизатором (`DS-synchronizer/scripts/pack-project.sh`). Prod-путь: projection в commit.

**Markers в projection:** `_meta.synced_at` логируется при загрузке → видно, когда в последний раз синхронизировались.

---

## 4. Парсинг Pack-таблиц

**Сценарии** (`_parse_scenarios_from_rows`):
- 6 колонок: `№ | Название | Команда | Статус | Описание RU | Описание EN`
- **Фильтр:** только строки с `✅` в статусе попадают в L1 (draft/planned — пропускаются)
- Иконки доклеиваются из `core/registry.py` по `command` → не дублируются в Pack

**FAQ/Troubleshooting** (`_parse_faq_from_rows`):
- 6 колонок: `№ | Вопрос RU | Вопрос EN | Keywords | Ответ RU | Ответ EN`
- Keywords — comma-separated, используются для `match_faq()` scoring

**Идентичность** (`_parse_identity`) — regex по `**Имя:**`, `**Назначение (ru):**`, `**Как задать вопрос (ru):**`.

---

## 5. FAQ matching (L1, коммит `a9473d2`)

Функция: `match_faq(question, lang) → Optional[str]`.

**Алгоритм:**
1. Для каждого FAQ item: `matched = count(kw in question for kw in keywords)`
2. `ratio = matched / len(keywords)`
3. Среди всех с `matched > 0` выбрать item с максимальным `ratio` (tie-break: больше абсолютных совпадений)
4. **Порог: `ratio >= 0.25`** (доля ≥25% keywords)

**Почему доля, а не абсолютное число** (WP-7 W12):
- Раньше: `min_score=1` → любое совпадение keyword → FAQ-ответ
- Проблема: вопрос «что такое IWE?» матчился FAQ #14 «iwe» (1/8 keywords = 12%) → бот выдавал нерелевантный короткий ответ
- После фикса: 1/8 = 12% < 25% → FAQ не матчит → L3 с полным контекстом

**Граница для keywords:**
- Короткие (≤3 символа) → word-boundary regex `(?<!\w){kw}(?!\w)`. Без этого «рп» матчилось внутри «интерпретатор»
- Длинные (>3) → substring `kw in text`

**⚠️ Баг в code comments (не функциональный):** `states/common/consultation.py:581` помечен как «L1: Structured Lookup», а `:586` — «L0: FAQ-матч». Лейблы перепутаны: structured lookup — это L0 (точные данные из RAM), FAQ-матч — L1 (keyword search). Логика работает правильно, только комментарии вводят в заблуждение. Фикс — отдельным коммитом при следующем касании файла.

**Конверсия `\n`:** Pack хранит литеральные `\n` → функция заменяет на реальные переносы перед возвратом.

---

## 6. L3 Context Pipeline (`engines/shared/context_pipeline.py`)

Сборка system prompt для Sonnet на L3 — параллельные collectors через `asyncio.gather`. Управляется тиром пользователя (UITier 1-4).

### 6.1. Collectors (8 штук)

| Collector | Что собирает | Placeholder | Зависимость |
|-----------|-------------|-------------|-------------|
| `collect_user_profile` | Профиль из bot DB (имя, занятие, цели) | `user_profile` | `_build_user_profile` из question_handler |
| `collect_bot_context` | Self-knowledge бота (L1 полный текст) | `bot_section` | Заранее собран в consultation: `get_self_knowledge(lang)` |
| `collect_pre_search` | knowledge-mcp до Claude (search-first) | `knowledge_section` | `gateway_mcp.knowledge_search` (требует `telegram_user_id`) |
| `collect_user_progress` | Прогресс: тир, серия, марафон, лента | `progress_section` | `intern` dict + `TIER_DISPLAY` |
| `collect_standard_claude` | Методология CLAUDE.md | `standard_section` | `get_standard_claude_md()` |
| `collect_iwe_instructions` | IWE platform instructions через Gateway | `iwe_section` | `gateway_mcp.get_instructions()` (public tool) |
| `collect_personal_claude` | Personal CLAUDE.md из GitHub пользователя | `personal_section` | Заранее загружен в caller |

### 6.2. Tier Pipeline (DP.ARCH.002)

| Тир | Роль | Collectors |
|-----|------|-----------|
| **T1 Expert** | Базовый консультант | profile + bot + pre_search + progress |
| **T2 Mentor** | + методология и IWE | T1 + standard_claude + iwe_instructions |
| **T3 Co-thinker** | + персональный контекст | T2 + personal_claude |
| **T4 Architect** | (пока = T3) | = T3, future: + progress, plans |

**Правило:** добавляя новый collector — обновить `TIER_PIPELINE` dict и добавить ключ в `sections` dict `assemble_context()`.

### 6.3. Search-first (pre-search)

Проблема до фикса: Claude на L3 мог не вызвать `search_knowledge` → ответ «такой возможности нет» несмотря на наличие документов в базе.

**Решение:** `collect_pre_search` вызывает `gateway_mcp.knowledge_search(query=question, limit=5)` ДО Claude. До 5 результатов × 1500 символов → preпiend в system prompt как «ИНФОРМАЦИЯ ИЗ БАЗЫ ЗНАНИЙ (pre-search)». Claude всё равно может вызвать tool для уточнения.

**Коммит `a9473d2`:** передаётся `telegram_user_id=intern.get('chat_id')`. Без этого Gateway возвращал 401 → pre-search всегда пуст.

### 6.4. Error handling

`asyncio.gather(return_exceptions=True)` — падение одного collector не валит pipeline. Ошибка логируется в `Context collector error: ...`, placeholder остаётся пустой строкой → `fill_tier_prompt` отрендерит без секции.

---

## 7. Кеширование и инвалидация

**Module-level кеш** в `self_knowledge.py`:
- `_scenarios, _faq, _troubleshooting, _identity, _integrations, _platform` — заполнены раз в `_parse_pack()`
- `_cache: dict[str, str]` — полный текст по языку (`get_self_knowledge(lang)`)
- `_loaded: bool` — guard против повторного парсинга

**Инвалидация:**
- `invalidate_cache()` — сброс всех global-переменных (для тестов, daily refresh)
- TODO: ежедневный call в scheduler (сейчас кеш живёт до рестарта процесса)

**Риск:** если projection YAML обновлён на диске — бот не увидит до рестарта. Mitigation: Pilot-First flow + Railway автодеплой при коммите (projection обновляется в commit).

---

## 8. Anti-hallucination правила (WP-7 W12)

Все правила из `CLAUDE.md § 10.1-10.14` применяются в L3 пайплайне:

1. **Граница знаний = правило #1** в system prompt (выше всех других правил)
2. **Пустой профиль → явный текст** «не заполнен, НЕ выдумывай» (НЕ пустая строка)
3. **Нулевые метрики ≠ отсутствие** — промпт говорит «данные не подключены», чтобы LLM не угадывал числа
4. **Structured > MCP** для точных данных (день марафона, длительность) → L0 structured_lookup
5. **`get_bot_info` compact** — identity + FAQ, БЕЗ списка сценариев (иначе Claude цитирует команды вместо ответа)

**Symptom, если нарушено:** бот выдумывает команды, которых нет; цитирует цифры вместо «не знаю»; отвечает «нет такой функции» при наличии в knowledge-base.

---

## 9. Точки подключения и потребители

| Caller | Что использует | Где |
|--------|---------------|-----|
| `states/common/consultation.py` | `get_self_knowledge(lang)`, `match_faq(q, lang)` | L1 матч + L3 bot_context |
| `engines/shared/consultation_tools.py` | `get_self_knowledge('ru')` | `get_bot_info` tool (compact output) |
| `engines/shared/context_pipeline.py` | `bot_context` (через kwargs) | `collect_bot_context` collector |

**Не зависит от:** FSM, scheduler, observability. Чистый read-only pipeline, вызывается синхронно в consultation.

---

## 10. Связанные процессы и док-и

- **P-02 Content Generation** — генерация уроков/практик на Sonnet, использует context pipeline аналогично
- **P-03 Intent Detection** — определяет, идёт ли вопрос в consultation или в SM-стейт
- **P-06 Observability** — error_classifier ловит ошибки `collect_*` → категория `claude_api`/`mcp`
- **P-10 Gateway MCP** — clients/gateway_mcp.py (knowledge_search, get_instructions)
- **Синхронизатор** — `DS-synchronizer/scripts/pack-project.sh` (вне бота, генерирует projection)
