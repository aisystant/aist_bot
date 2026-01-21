"""
Динамический контекст для улучшения Knowledge Retrieval.

Компоненты:
- UserProgressContext: контекст прогресса пользователя (день марафона, пройденные темы)
- ConversationMemory: память о предыдущих вопросах в сессии
- TopicMetadataContext: контекст из метаданных темы (related_concepts, pain_point)
- DynamicContextBuilder: объединяет все контексты
"""

import json
from datetime import datetime, date
from typing import Optional, List, Dict, Set
from dataclasses import dataclass, field

from config import get_logger

logger = get_logger(__name__)


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class UserProgress:
    """Прогресс пользователя в обучении"""
    current_day: int = 1
    completed_topics: List[str] = field(default_factory=list)
    complexity_level: int = 1
    mode: str = "marathon"
    marathon_status: str = "not_started"
    active_days_streak: int = 0

    # Вычисляемые поля
    topics_completed_count: int = 0
    is_behind_schedule: bool = False

    def __post_init__(self):
        self.topics_completed_count = len(self.completed_topics)


@dataclass
class ConversationItem:
    """Элемент истории диалога"""
    question: str
    answer: str
    context_topic: str
    timestamp: datetime
    mcp_sources: List[str] = field(default_factory=list)


@dataclass
class TopicMetadata:
    """Метаданные текущей темы"""
    topic_id: str
    title: str
    main_concept: str
    related_concepts: List[str] = field(default_factory=list)
    pain_point: str = ""
    key_insight: str = ""
    day: int = 0
    topic_type: str = "theory"  # theory или practice


@dataclass
class DynamicContext:
    """Полный динамический контекст для retrieval и генерации"""
    user_progress: Optional[UserProgress] = None
    conversation_history: List[ConversationItem] = field(default_factory=list)
    topic_metadata: Optional[TopicMetadata] = None

    # Дополнительные сигналы для retrieval
    boost_concepts: List[str] = field(default_factory=list)  # Концепции для приоритета
    exclude_topics: Set[str] = field(default_factory=set)  # Уже покрытые темы

    def get_search_boost_terms(self) -> List[str]:
        """Возвращает термины для приоритетного поиска"""
        terms = list(self.boost_concepts)

        if self.topic_metadata:
            terms.extend(self.topic_metadata.related_concepts)
            if self.topic_metadata.main_concept:
                terms.insert(0, self.topic_metadata.main_concept)

        return terms[:10]  # Максимум 10 термов

    def get_context_summary(self) -> str:
        """Возвращает краткую сводку контекста для промпта"""
        parts = []

        if self.user_progress:
            progress = self.user_progress
            parts.append(f"День {progress.current_day} марафона")
            parts.append(f"Сложность: {progress.complexity_level}/3")
            if progress.topics_completed_count > 0:
                parts.append(f"Пройдено тем: {progress.topics_completed_count}")
            if progress.active_days_streak > 1:
                parts.append(f"Серия активных дней: {progress.active_days_streak}")

        if self.topic_metadata:
            meta = self.topic_metadata
            if meta.title:
                parts.append(f"Текущая тема: {meta.title}")

        return " | ".join(parts) if parts else ""


# =============================================================================
# USER PROGRESS CONTEXT
# =============================================================================

class UserProgressContext:
    """Извлекает и форматирует контекст прогресса пользователя"""

    @staticmethod
    def extract(intern: dict) -> UserProgress:
        """Извлекает прогресс из профиля пользователя

        Args:
            intern: словарь профиля пользователя

        Returns:
            UserProgress с данными прогресса
        """
        # Парсим completed_topics (может быть строкой JSON или списком)
        completed_topics = intern.get('completed_topics', [])
        if isinstance(completed_topics, str):
            try:
                completed_topics = json.loads(completed_topics)
            except json.JSONDecodeError:
                completed_topics = []

        # Вычисляем текущий день марафона
        current_day = 1
        marathon_start = intern.get('marathon_start_date')
        if marathon_start:
            if isinstance(marathon_start, str):
                try:
                    marathon_start = datetime.strptime(marathon_start, "%Y-%m-%d").date()
                except ValueError:
                    marathon_start = None

            if marathon_start:
                days_passed = (date.today() - marathon_start).days
                current_day = min(max(days_passed + 1, 1), 14)  # 1-14

        # Проверяем отставание от графика
        expected_topics = current_day * 2  # 2 темы в день
        is_behind = len(completed_topics) < expected_topics - 2  # Отстаём на день+

        progress = UserProgress(
            current_day=current_day,
            completed_topics=completed_topics,
            complexity_level=intern.get('complexity_level', 1),
            mode=intern.get('mode', 'marathon'),
            marathon_status=intern.get('marathon_status', 'not_started'),
            active_days_streak=intern.get('active_days_streak', 0),
            is_behind_schedule=is_behind
        )

        logger.debug(f"UserProgressContext: день {progress.current_day}, "
                    f"тем пройдено: {progress.topics_completed_count}")

        return progress

    @staticmethod
    def get_completed_concepts(progress: UserProgress,
                               knowledge_structure: dict) -> List[str]:
        """Извлекает концепции из пройденных тем

        Args:
            progress: прогресс пользователя
            knowledge_structure: структура знаний (YAML)

        Returns:
            Список изученных концепций
        """
        concepts = []
        topics = knowledge_structure.get('topics', [])

        for topic_id in progress.completed_topics:
            for topic in topics:
                if topic.get('id') == topic_id:
                    if topic.get('main_concept'):
                        concepts.append(topic['main_concept'])
                    concepts.extend(topic.get('related_concepts', []))
                    break

        return list(set(concepts))  # Уникальные


# =============================================================================
# CONVERSATION MEMORY
# =============================================================================

class ConversationMemory:
    """Управляет памятью о предыдущих вопросах"""

    def __init__(self, max_items: int = 5):
        self.max_items = max_items
        self._cache: Dict[int, List[ConversationItem]] = {}  # chat_id -> history

    async def load_history(self, chat_id: int,
                          qa_history_loader=None) -> List[ConversationItem]:
        """Загружает историю из БД или кэша

        Args:
            chat_id: ID чата
            qa_history_loader: функция для загрузки из БД (get_qa_history)

        Returns:
            Список последних вопросов
        """
        # Проверяем кэш
        if chat_id in self._cache:
            return self._cache[chat_id]

        # Загружаем из БД
        if qa_history_loader:
            try:
                raw_history = await qa_history_loader(chat_id, limit=self.max_items)
                history = [
                    ConversationItem(
                        question=item['question'],
                        answer=item['answer'][:500],  # Сокращаем ответы
                        context_topic=item.get('context_topic', ''),
                        timestamp=item.get('created_at', datetime.now()),
                        mcp_sources=item.get('mcp_sources', [])
                    )
                    for item in raw_history
                ]
                # Разворачиваем — нужен хронологический порядок
                history.reverse()
                self._cache[chat_id] = history
                return history
            except Exception as e:
                logger.error(f"ConversationMemory: ошибка загрузки истории: {e}")

        return []

    def add_item(self, chat_id: int, item: ConversationItem):
        """Добавляет элемент в историю"""
        if chat_id not in self._cache:
            self._cache[chat_id] = []

        self._cache[chat_id].append(item)

        # Ограничиваем размер
        if len(self._cache[chat_id]) > self.max_items:
            self._cache[chat_id] = self._cache[chat_id][-self.max_items:]

    def get_recent_topics(self, chat_id: int) -> Set[str]:
        """Возвращает темы недавних вопросов"""
        history = self._cache.get(chat_id, [])
        return {item.context_topic for item in history if item.context_topic}

    def get_conversation_context(self, chat_id: int) -> str:
        """Форматирует историю для промпта"""
        history = self._cache.get(chat_id, [])
        if not history:
            return ""

        # Берём последние 3 вопроса
        recent = history[-3:]

        lines = ["НЕДАВНИЕ ВОПРОСЫ ПОЛЬЗОВАТЕЛЯ:"]
        for item in recent:
            topic_info = f" (тема: {item.context_topic})" if item.context_topic else ""
            lines.append(f"- {item.question[:100]}...{topic_info}")

        return "\n".join(lines)

    def clear(self, chat_id: int = None):
        """Очищает кэш"""
        if chat_id:
            self._cache.pop(chat_id, None)
        else:
            self._cache.clear()


# =============================================================================
# TOPIC METADATA CONTEXT
# =============================================================================

class TopicMetadataContext:
    """Извлекает контекст из метаданных темы"""

    @staticmethod
    def extract(topic_id: str, knowledge_structure: dict) -> Optional[TopicMetadata]:
        """Извлекает метаданные темы

        Args:
            topic_id: ID темы
            knowledge_structure: структура знаний (YAML)

        Returns:
            TopicMetadata или None
        """
        topics = knowledge_structure.get('topics', [])

        for topic in topics:
            if topic.get('id') == topic_id:
                return TopicMetadata(
                    topic_id=topic_id,
                    title=topic.get('title', ''),
                    main_concept=topic.get('main_concept', ''),
                    related_concepts=topic.get('related_concepts', []),
                    pain_point=topic.get('pain_point', ''),
                    key_insight=topic.get('key_insight', ''),
                    day=topic.get('day', 0),
                    topic_type=topic.get('type', 'theory')
                )

        return None

    @staticmethod
    def get_search_boost_from_metadata(metadata: TopicMetadata) -> List[str]:
        """Возвращает термины для усиления поиска из метаданных"""
        terms = []

        if metadata.main_concept:
            terms.append(metadata.main_concept)

        terms.extend(metadata.related_concepts[:5])

        # Извлекаем ключевые слова из pain_point и key_insight
        if metadata.pain_point:
            # Берём существительные/глаголы длиннее 5 символов
            import re
            words = re.findall(r'\b[а-яёa-z]{5,}\b', metadata.pain_point.lower())
            terms.extend(words[:3])

        return terms


# =============================================================================
# DYNAMIC CONTEXT BUILDER
# =============================================================================

class DynamicContextBuilder:
    """Строит полный динамический контекст"""

    def __init__(self):
        self.conversation_memory = ConversationMemory(max_items=5)
        self._knowledge_structure: Optional[dict] = None

    def set_knowledge_structure(self, structure: dict):
        """Устанавливает структуру знаний"""
        self._knowledge_structure = structure

    async def build(self,
                   intern: dict,
                   topic_id: Optional[str] = None,
                   qa_history_loader=None) -> DynamicContext:
        """Строит полный динамический контекст

        Args:
            intern: профиль пользователя
            topic_id: ID текущей темы (опционально)
            qa_history_loader: функция загрузки истории Q&A

        Returns:
            DynamicContext со всеми компонентами
        """
        chat_id = intern.get('chat_id')
        context = DynamicContext()

        # 1. Прогресс пользователя
        context.user_progress = UserProgressContext.extract(intern)

        # 2. История диалога
        if chat_id and qa_history_loader:
            context.conversation_history = await self.conversation_memory.load_history(
                chat_id, qa_history_loader
            )

        # 3. Метаданные темы
        if topic_id and self._knowledge_structure:
            context.topic_metadata = TopicMetadataContext.extract(
                topic_id, self._knowledge_structure
            )

        # 4. Формируем boost_concepts
        if context.topic_metadata:
            context.boost_concepts = TopicMetadataContext.get_search_boost_from_metadata(
                context.topic_metadata
            )

        # 5. Исключаем уже покрытые темы из недавних вопросов
        if chat_id:
            context.exclude_topics = self.conversation_memory.get_recent_topics(chat_id)

        logger.info(f"DynamicContextBuilder: построен контекст, "
                   f"boost_concepts={len(context.boost_concepts)}, "
                   f"history={len(context.conversation_history)}")

        return context

    def get_prompt_additions(self, context: DynamicContext) -> Dict[str, str]:
        """Возвращает дополнения для промпта Claude

        Args:
            context: динамический контекст

        Returns:
            Словарь с секциями для промпта
        """
        additions = {}

        # Сводка прогресса
        summary = context.get_context_summary()
        if summary:
            additions['progress_summary'] = f"ПРОГРЕСС ПОЛЬЗОВАТЕЛЯ: {summary}"

        # Контекст темы
        if context.topic_metadata:
            meta = context.topic_metadata
            parts = []

            if meta.pain_point:
                parts.append(f"Боль пользователя: {meta.pain_point}")

            if meta.key_insight:
                parts.append(f"Ключевой инсайт темы: {meta.key_insight}")

            if meta.related_concepts:
                parts.append(f"Связанные концепции: {', '.join(meta.related_concepts[:5])}")

            if parts:
                additions['topic_context'] = "КОНТЕКСТ ТЕМЫ:\n" + "\n".join(parts)

        # История диалога
        if context.conversation_history:
            chat_id = context.user_progress.current_day if context.user_progress else 0
            # Форматируем последние вопросы
            recent = context.conversation_history[-3:]
            lines = ["НЕДАВНИЕ ВОПРОСЫ:"]
            for item in recent:
                q_short = item.question[:80] + "..." if len(item.question) > 80 else item.question
                lines.append(f"- {q_short}")
            additions['conversation_history'] = "\n".join(lines)

        return additions


# =============================================================================
# ГЛОБАЛЬНЫЙ INSTANCE И УДОБНЫЕ ФУНКЦИИ
# =============================================================================

_context_builder: Optional[DynamicContextBuilder] = None


def get_context_builder() -> DynamicContextBuilder:
    """Возвращает singleton instance DynamicContextBuilder"""
    global _context_builder
    if _context_builder is None:
        _context_builder = DynamicContextBuilder()
    return _context_builder


async def build_dynamic_context(intern: dict,
                                topic_id: Optional[str] = None,
                                qa_history_loader=None,
                                knowledge_structure: dict = None) -> DynamicContext:
    """Удобная функция для построения динамического контекста

    Args:
        intern: профиль пользователя
        topic_id: ID текущей темы
        qa_history_loader: функция загрузки истории
        knowledge_structure: структура знаний (если не установлена глобально)

    Returns:
        DynamicContext
    """
    builder = get_context_builder()

    if knowledge_structure:
        builder.set_knowledge_structure(knowledge_structure)

    return await builder.build(intern, topic_id, qa_history_loader)
