# Миграция на модульную архитектуру

## Созданные модули

### config/
```python
from config import (
    BOT_TOKEN, ANTHROPIC_API_KEY, DATABASE_URL,
    MCP_URL, KNOWLEDGE_MCP_URL, validate_env,
    get_logger, MOSCOW_TZ,
    Mode, MarathonStatus, FeedStatus, FeedWeekStatus,
    COMPLEXITY_LEVELS, BLOOM_LEVELS,  # BLOOM_LEVELS = alias
    STUDY_DURATIONS, DIFFICULTY_LEVELS,
    FEED_TOPICS_TO_SUGGEST,
)
```

### db/
```python
from db import init_db, db_pool, acquire, create_tables
from db.queries.users import get_intern, update_intern
from db.queries.answers import save_answer, get_weekly_work_products
from db.queries.activity import record_active_day, get_activity_stats
from db.queries.feed import create_feed_week, get_current_feed_week
from db.queries.qa import save_qa, get_qa_history
```

### clients/
```python
from clients import claude, mcp_guides, mcp_knowledge

# Генерация контента
content = await claude.generate_content(topic, intern, mcp_guides, mcp_knowledge)
question = await claude.generate_question(topic, intern)

# Поиск через MCP
results = await mcp_guides.semantic_search("системное мышление")
knowledge = await mcp_knowledge.search("агентность")
```

### core/
```python
from core import detect_intent, IntentType, get_question_keywords
from core.helpers import get_personalization_prompt, load_topic_metadata

# Определение интента
intent = detect_intent(user_message, context={'awaiting_answer': True})
if intent.type == IntentType.QUESTION:
    # Пользователь задал вопрос
    pass
elif intent.type == IntentType.ANSWER:
    # Пользователь ответил на задание
    pass
```

### engines/shared/
```python
from engines.shared import handle_question

# Обработка вопроса пользователя
answer, sources = await handle_question(
    question="Что такое системное мышление?",
    intern=intern_data,
    context_topic="День 1: Три состояния"
)
```

## Шаги миграции bot.py

### 1. Заменить импорты

```python
# БЫЛО:
import os
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# ...много констант...

# СТАЛО:
from config import (
    BOT_TOKEN, ANTHROPIC_API_KEY, DATABASE_URL,
    get_logger, MOSCOW_TZ, validate_env,
    DIFFICULTY_LEVELS, STUDY_DURATIONS, BLOOM_LEVELS,
    COMPLEXITY_LEVELS, MARATHON_DAYS, MAX_TOPICS_PER_DAY,
)
validate_env()
logger = get_logger(__name__)
```

### 2. Заменить работу с БД

```python
# БЫЛО:
db_pool = await asyncpg.create_pool(DATABASE_URL)
async with db_pool.acquire() as conn:
    # SQL запросы...

# СТАЛО:
from db import init_db, acquire
await init_db()

async with acquire() as conn:
    intern = await get_intern(chat_id)
    await update_intern(chat_id, {'name': 'Иван'})
```

### 3. Заменить Claude/MCP клиенты

```python
# БЫЛО:
class ClaudeClient:
    # ...250 строк кода...

# СТАЛО:
from clients import claude, mcp_guides, mcp_knowledge

content = await claude.generate_content(topic, intern, mcp_guides, mcp_knowledge)
```

### 4. Добавить распознавание вопросов

```python
from core import detect_intent, IntentType
from engines.shared import handle_question

@router.message(F.text)
async def handle_message(message: Message, state: FSMContext):
    current_state = await state.get_state()
    intern = await get_intern(message.chat.id)

    # Определяем контекст
    context = {
        'awaiting_answer': current_state == LearningStates.waiting_for_answer,
        'awaiting_work_product': current_state == LearningStates.waiting_for_work_product,
        'mode': intern.get('mode', 'marathon'),
    }

    intent = detect_intent(message.text, context)

    if intent.type == IntentType.QUESTION:
        # Обрабатываем вопрос через MCP
        answer, sources = await handle_question(
            question=message.text,
            intern=intern,
            context_topic=intern.get('current_context', {}).get('topic')
        )
        await message.answer(answer)
    elif intent.type == IntentType.ANSWER:
        # Обрабатываем ответ как раньше
        pass
    # ...
```

## Новые таблицы БД

После запуска с новыми модулями автоматически создадутся:

- `feed_weeks` — недельные планы Ленты
- `feed_sessions` — сессии Ленты
- `activity_log` — лог активности (систематичность)
- `qa_history` — история Q&A

И добавятся поля в `interns`:
- `mode` — текущий режим (marathon/feed/both)
- `complexity_level` — уровень сложности (1-3)
- `active_days_*` — статистика активности
