"""
Улучшенный Knowledge Retrieval для MCP.

Ранжирование: MCP embedding score (cosine similarity от bge-m3).
Клиент НЕ переранжирует результаты — это ответственность MCP-сервера.

Компоненты:
- QueryExpander: расширение запросов + fuzzy correction опечаток
- SemanticDeduplicator: умная дедупликация
- FallbackStrategy: стратегия при пустых результатах
- EnhancedRetrieval: основной класс, объединяющий всё
"""

import re
import hashlib
import asyncio
from difflib import get_close_matches
from typing import Optional, List, Tuple, Dict, Set
from dataclasses import dataclass, field

from config import get_logger
from clients.gateway_mcp import gateway_mcp

logger = get_logger(__name__)


# =============================================================================
# СЛОВАРЬ ТЕРМИНОВ И СИНОНИМОВ
# =============================================================================

# Связанные термины для расширения запросов
# Ключ — основной термин, значения — связанные понятия
TERM_RELATIONS: Dict[str, List[str]] = {
    # Состояния
    "хаос": ["расфокус", "разбросанность", "незавершённое", "флюгер", "внимание"],
    "тупик": ["застревание", "стеклянный потолок", "день сурка", "стагнация"],
    "поворот": ["изменения", "страх перемен", "новое начало", "трансформация"],
    "развитие": ["рост", "прогресс", "движение", "улучшение"],

    # Ключевые концепции
    "собранность": ["фокус", "концентрация", "внимание", "целостность", "агентность"],
    "агентность": ["автономия", "действие", "инициатива", "контроль", "влияние"],
    "мастерство": ["навык", "умение", "компетенция", "экспертиза", "практика"],
    "ясность": ["понимание", "чёткость", "определённость", "видение"],

    # Практики
    "слот саморазвития": ["время для себя", "практика", "регулярность", "ритм"],
    "трекер": ["отслеживание", "учёт", "метрики", "прогресс", "дневник"],
    "рабочий продукт": ["результат", "артефакт", "выход", "deliverable"],
    "мем": ["единица мышления", "концепция", "идея", "паттерн"],

    # Проблемы
    "прокрастинация": ["откладывание", "избегание", "завтра", "потом"],
    "выгорание": ["истощение", "усталость", "burnout", "перегрузка"],
    "быстрые удовольствия": ["соцсети", "сериалы", "игры", "дофамин", "отвлечения"],

    # Системное мышление
    "системное мышление": ["системный подход", "холизм", "взаимосвязи", "эмерджентность"],
    "мантра": ["мантра системного мышления", "принципы мышления", "чек-лист"],
    "экзокортекс": ["внешний мозг", "заметки", "база знаний", "second brain"],
    "итерации": ["циклы", "повторения", "спринты", "улучшения"],
    "инкременты": ["приращения", "шаги", "малые изменения"],

    # Роли
    "созидатель": ["творец", "создатель", "деятель", "maker"],
    "практикующий ученик": ["осознанный ученик", "системный ученик"],
}

# Синонимы (двусторонние связи)
SYNONYMS: Dict[str, str] = {
    # Термины с вариантами написания
    "саморазвитие": "самосовершенствование",
    "самосовершенствование": "саморазвитие",
    "целеполагание": "постановка целей",
    "постановка целей": "целеполагание",
    "тайм-менеджмент": "управление временем",
    "управление временем": "тайм-менеджмент",
    "осознанность": "mindfulness",
    "mindfulness": "осознанность",
    "медитация": "практика осознанности",
    "фокус": "концентрация",
    "концентрация": "фокус",
}


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class RetrievalResult:
    """Результат поиска с метаданными"""
    text: str
    source: str
    source_type: str  # "pack", "guides", or "ds"
    relevance_score: float = 0.0
    date: Optional[str] = None
    original_item: dict = field(default_factory=dict)

    @property
    def text_hash(self) -> str:
        """Хеш для сравнения похожести"""
        # Нормализуем текст для сравнения
        normalized = re.sub(r'\s+', ' ', self.text.lower().strip())
        return hashlib.md5(normalized[:500].encode()).hexdigest()

    @property
    def key_phrases(self) -> Set[str]:
        """Извлекаем ключевые фразы для семантического сравнения"""
        text_lower = self.text.lower()
        # Извлекаем слова длиннее 4 символов
        words = set(re.findall(r'\b[а-яёa-z]{4,}\b', text_lower))
        return words


@dataclass
class RetrievalConfig:
    """Конфигурация retrieval pipeline"""
    # Лимиты поиска
    guides_limit: int = 5
    knowledge_limit: int = 5
    max_results: int = 7

    # Фильтрация по релевантности
    min_relevance_score: float = 0.3

    # Размеры текста
    max_chunk_size: int = 5000

    # Дедупликация
    similarity_threshold: float = 0.6  # Jaccard similarity для дедупликации

    # Fallback
    enable_fallback: bool = True
    fallback_broader_query: bool = True


# =============================================================================
# QUERY EXPANDER
# =============================================================================

class QueryExpander:
    """Расширяет поисковые запросы связанными терминами"""

    def __init__(self, term_relations: Dict[str, List[str]] = None,
                 synonyms: Dict[str, str] = None):
        self.term_relations = term_relations or TERM_RELATIONS
        self.synonyms = synonyms or SYNONYMS

    def fuzzy_correct(self, text: str, cutoff: float = 0.7) -> str:
        """Исправляет опечатки, сверяя слова с известными терминами.

        Использует difflib.get_close_matches (SequenceMatcher ratio).
        Порог 0.7 достаточно консервативен: «манры»→«мантра» (0.73) ✓,
        «манеры»→«мантра» (0.67) ✗.
        """
        known_terms = list(self.term_relations.keys())
        words = text.split()
        corrected = []
        changed = False

        for word in words:
            word_lower = word.lower()
            # Пропускаем короткие слова и уже известные термины
            if len(word_lower) < 4 or word_lower in self.term_relations:
                corrected.append(word)
                continue

            matches = get_close_matches(word_lower, known_terms, n=1, cutoff=cutoff)
            if matches:
                logger.info(f"QueryExpander: опечатка '{word}' → '{matches[0]}'")
                corrected.append(matches[0])
                changed = True
            else:
                corrected.append(word)

        result = ' '.join(corrected)
        if changed:
            logger.info(f"QueryExpander: corrected → '{result[:80]}'")
        return result

    def expand(self, query: str, max_expansions: int = 3) -> List[str]:
        """Расширяет запрос связанными терминами

        Args:
            query: исходный запрос
            max_expansions: максимум дополнительных запросов

        Returns:
            Список запросов (оригинал + расширенные)
        """
        # Сначала исправляем опечатки
        query = self.fuzzy_correct(query)
        queries = [query]
        query_lower = query.lower()

        expansions_added = 0

        # 1. Ищем прямые связи с терминами
        for term, related in self.term_relations.items():
            if term in query_lower:
                # Добавляем запросы с связанными терминами
                for related_term in related[:2]:  # Максимум 2 связанных на термин
                    if expansions_added >= max_expansions:
                        break
                    expanded = f"{query} {related_term}"
                    if expanded not in queries:
                        queries.append(expanded)
                        expansions_added += 1
                        logger.debug(f"QueryExpander: '{term}' → добавлен '{related_term}'")

        # 2. Ищем синонимы
        for original, synonym in self.synonyms.items():
            if original in query_lower and expansions_added < max_expansions:
                # Заменяем термин на синоним
                expanded = query_lower.replace(original, synonym)
                if expanded != query_lower and expanded not in [q.lower() for q in queries]:
                    queries.append(expanded)
                    expansions_added += 1
                    logger.debug(f"QueryExpander: синоним '{original}' → '{synonym}'")

        logger.info(f"QueryExpander: {len(queries)} запросов из '{query[:50]}...'")
        return queries

    def extract_key_concepts(self, query: str) -> List[str]:
        """Извлекает ключевые концепции из запроса"""
        query_lower = query.lower()
        concepts = []

        for term in self.term_relations.keys():
            if term in query_lower:
                concepts.append(term)

        return concepts


# =============================================================================
# SEMANTIC DEDUPLICATOR
# =============================================================================

class SemanticDeduplicator:
    """Умная дедупликация на основе семантического сходства"""

    def __init__(self, similarity_threshold: float = 0.6):
        self.similarity_threshold = similarity_threshold

    def jaccard_similarity(self, set1: Set[str], set2: Set[str]) -> float:
        """Вычисляет коэффициент Жаккара между двумя множествами"""
        if not set1 or not set2:
            return 0.0
        intersection = len(set1 & set2)
        union = len(set1 | set2)
        return intersection / union if union > 0 else 0.0

    def are_similar(self, result1: RetrievalResult, result2: RetrievalResult) -> bool:
        """Проверяет, являются ли два результата семантически похожими"""
        # 1. Проверка по хешу (идентичный текст)
        if result1.text_hash == result2.text_hash:
            return True

        # 2. Проверка по ключевым фразам (семантическое сходство)
        phrases1 = result1.key_phrases
        phrases2 = result2.key_phrases

        similarity = self.jaccard_similarity(phrases1, phrases2)

        return similarity >= self.similarity_threshold

    def deduplicate(self, results: List[RetrievalResult]) -> List[RetrievalResult]:
        """Удаляет семантически похожие результаты

        Оставляет результат с наивысшим relevance_score из группы похожих.
        """
        if not results:
            return []

        # Сортируем по score чтобы оставлять лучшие
        sorted_results = sorted(results, key=lambda r: r.relevance_score, reverse=True)

        unique_results = []

        for result in sorted_results:
            # Проверяем, похож ли на уже добавленные
            is_duplicate = False
            for existing in unique_results:
                if self.are_similar(result, existing):
                    is_duplicate = True
                    logger.debug(f"Deduplicator: пропущен дубль "
                               f"(similarity >= {self.similarity_threshold})")
                    break

            if not is_duplicate:
                unique_results.append(result)

        removed = len(results) - len(unique_results)
        if removed > 0:
            logger.info(f"SemanticDeduplicator: удалено {removed} дубликатов")

        return unique_results


# =============================================================================
# FALLBACK STRATEGY
# =============================================================================

class FallbackStrategy:
    """Стратегия при пустых или недостаточных результатах"""

    def __init__(self, expander: QueryExpander = None):
        self.expander = expander or QueryExpander()

    def generate_fallback_queries(self, original_query: str,
                                  tried_queries: List[str]) -> List[str]:
        """Генерирует fallback запросы

        Args:
            original_query: исходный запрос
            tried_queries: уже опробованные запросы

        Returns:
            Новые запросы для попытки
        """
        fallback_queries = []
        tried_set = set(q.lower() for q in tried_queries)

        # 1. Извлекаем ключевые концепции и ищем по ним отдельно
        concepts = self.expander.extract_key_concepts(original_query)
        for concept in concepts:
            if concept.lower() not in tried_set:
                fallback_queries.append(concept)

        # 2. Берём связанные термины напрямую
        for concept in concepts:
            related = self.expander.term_relations.get(concept, [])
            for term in related[:2]:
                if term.lower() not in tried_set:
                    fallback_queries.append(term)

        # 3. Упрощаем запрос — берём только существительные (длинные слова)
        words = re.findall(r'\b[а-яёa-z]{5,}\b', original_query.lower())
        simple_query = ' '.join(words[:3])
        if simple_query and simple_query not in tried_set:
            fallback_queries.append(simple_query)

        # 4. Каждое длинное слово отдельно (для опечаток и неточных запросов)
        for word in words[:2]:
            if word not in tried_set and len(word) >= 5:
                fallback_queries.append(word)

        logger.info(f"FallbackStrategy: сгенерировано {len(fallback_queries)} запросов")
        return fallback_queries[:4]  # Максимум 4 fallback запроса


# =============================================================================
# ENHANCED RETRIEVAL — ГЛАВНЫЙ КЛАСС
# =============================================================================

class EnhancedRetrieval:
    """Улучшенный Knowledge Retrieval с полным pipeline"""

    def __init__(self, config: RetrievalConfig = None):
        self.config = config or RetrievalConfig()
        self.expander = QueryExpander()
        self.deduplicator = SemanticDeduplicator(self.config.similarity_threshold)
        self.fallback = FallbackStrategy(self.expander)

    async def search(self, query: str,
                     keywords: List[str] = None,
                     context_topic: Optional[str] = None,
                     dynamic_context: "DynamicContext" = None) -> Tuple[str, List[str]]:
        """Выполняет улучшенный поиск по MCP

        Args:
            query: поисковый запрос
            keywords: ключевые слова (если уже извлечены)
            context_topic: контекст текущей темы
            dynamic_context: динамический контекст (прогресс, история, метаданные)

        Returns:
            Tuple[context, sources] - контекст для LLM и список источников
        """
        logger.info(f"EnhancedRetrieval: запрос len={len(query)}")

        # 1. Формируем базовый запрос с контекстом
        base_query = f"{context_topic} {query}" if context_topic else query

        # 2. Добавляем boost_concepts из динамического контекста
        boost_terms = []
        if dynamic_context:
            boost_terms = dynamic_context.get_search_boost_terms()
            if boost_terms:
                # Добавляем ключевые концепции к запросу
                boost_query = ' '.join(boost_terms[:3])
                base_query = f"{base_query} {boost_query}"
                logger.info(f"EnhancedRetrieval: boost terms: {boost_terms[:3]}")

        # 3. Расширяем запросы (включая fuzzy correction опечаток)
        expanded_queries = self.expander.expand(base_query, max_expansions=2)

        # 4. Выполняем поиск по всем запросам параллельно
        all_results: List[RetrievalResult] = []
        tried_queries = []

        search_tasks = [self._search_both_sources(q) for q in expanded_queries]
        search_results = await asyncio.gather(*search_tasks)

        for results in search_results:
            all_results.extend(results)
        tried_queries.extend(expanded_queries)

        # 5. Fallback при пустых результатах
        if len(all_results) < 2 and self.config.enable_fallback:
            logger.info("EnhancedRetrieval: мало результатов, пробуем fallback")
            fallback_queries = self.fallback.generate_fallback_queries(
                base_query, tried_queries
            )[:2]
            if fallback_queries:
                fallback_results = await asyncio.gather(
                    *[self._search_both_sources(q) for q in fallback_queries]
                )
                for results in fallback_results:
                    all_results.extend(results)

        # 6. Ранжирование по MCP embedding score (уже установлен в _parse_result)
        all_results.sort(key=lambda r: r.relevance_score, reverse=True)

        if all_results:
            logger.info(f"EnhancedRetrieval: {len(all_results)} результатов, "
                       f"top MCP score={all_results[0].relevance_score:.3f}")

        # 7. Фильтрация по MCP score
        filtered = [r for r in all_results
                   if r.relevance_score >= self.config.min_relevance_score]

        if len(filtered) < len(all_results):
            logger.info(f"EnhancedRetrieval: отфильтровано {len(all_results) - len(filtered)} "
                       f"результатов (MCP score < {self.config.min_relevance_score})")

        # 8. Дедупликация
        unique_results = self.deduplicator.deduplicate(filtered)

        # 9. Ограничиваем количество
        final_results = unique_results[:self.config.max_results]

        # 10. Формируем контекст и источники
        context, sources = self._format_results(final_results)

        logger.info(f"EnhancedRetrieval: итого {len(final_results)} результатов, "
                   f"{len(context)} символов контекста")

        return context, sources

    async def _search_both_sources(self, query: str) -> List[RetrievalResult]:
        """Ищет в unified Knowledge MCP (все source_type)"""
        results = []
        total_limit = self.config.guides_limit + self.config.knowledge_limit

        try:
            search_results = await gateway_mcp.knowledge_search(
                query, limit=total_limit
            )
        except Exception as e:
            logger.error(f"MCP-Knowledge error: {e}")
            search_results = []

        for item in (search_results or []):
            # source_type приходит из unified MCP (pack/guides/ds)
            item_source_type = item.get('source_type', 'pack') if isinstance(item, dict) else 'pack'
            result = self._parse_result(item, item_source_type)
            if result:
                results.append(result)

        return results

    def _parse_result(self, item: dict, source_type: str) -> Optional[RetrievalResult]:
        """Парсит результат из MCP в RetrievalResult"""
        if isinstance(item, str):
            text = item
            source = "Материалы Aisystant"  # Дефолтное имя вместо Unknown
            date = None
        elif isinstance(item, dict):
            # Логируем ключи для отладки (только первый раз)
            if not hasattr(self, '_logged_keys'):
                self._logged_keys = set()
            item_keys = frozenset(item.keys())
            if item_keys not in self._logged_keys:
                logger.debug(f"MCP {source_type} item keys: {list(item.keys())}")
                self._logged_keys.add(item_keys)

            # Извлекаем текст
            text = item.get('text', item.get('content', item.get('snippet', '')))

            # Извлекаем источник — пробуем разные ключи
            source = (
                item.get('source') or
                item.get('guide') or
                item.get('guide_title') or
                item.get('guide_slug') or
                item.get('section') or
                item.get('section_title') or
                item.get('title') or
                item.get('name') or
                item.get('url') or
                # Если ничего не найдено — берём первые слова текста
                (text[:50].split('.')[0] if text else None) or
                "Материалы Aisystant"
            )

            date = item.get('created_at', item.get('date', item.get('published_at')))
        else:
            return None

        if not text or len(text.strip()) < 50:
            return None

        # Обрезаем до максимального размера
        text = text[:self.config.max_chunk_size]

        # MCP возвращает cosine similarity score (0..1)
        mcp_score = 0.0
        if isinstance(item, dict):
            raw_score = item.get('score', 0)
            try:
                mcp_score = float(raw_score)
            except (TypeError, ValueError):
                mcp_score = 0.0

        return RetrievalResult(
            text=text,
            source=source,
            source_type=source_type,
            relevance_score=mcp_score,
            date=date,
            original_item=item if isinstance(item, dict) else {}
        )

    def _format_results(self, results: List[RetrievalResult]) -> Tuple[str, List[str]]:
        """Форматирует результаты в контекст для LLM"""
        if not results:
            return "", []

        context_parts = []
        sources = []

        # Группируем: guides отдельно, pack/ds — вместе как "знания"
        guides_results = [r for r in results if r.source_type == "guides"]
        knowledge_results = [r for r in results if r.source_type in ("pack", "ds")]

        # Pack/DS имеют приоритет (source-of-truth)
        if knowledge_results:
            context_parts.append("📚 АКТУАЛЬНЫЕ МАТЕРИАЛЫ:")
            for r in knowledge_results:
                date_prefix = f"[{r.date}] " if r.date else ""
                github_url = r.original_item.get("github_url", "") if r.original_item else ""
                url_line = f"\n🔗 {github_url}" if github_url else ""
                context_parts.append(f"{date_prefix}{r.text}{url_line}")
                if r.source and f"База знаний: {r.source}" not in sources:
                    sources.append(f"База знаний: {r.source}")

        if guides_results:
            if knowledge_results:
                context_parts.append("\n---\n")
            context_parts.append("📖 ИЗ РУКОВОДСТВ:")
            for r in guides_results:
                github_url = r.original_item.get("github_url", "") if r.original_item else ""
                url_line = f"\n🔗 {github_url}" if github_url else ""
                context_parts.append(f"{r.text}{url_line}")
                if r.source and f"Руководство: {r.source}" not in sources:
                    sources.append(f"Руководство: {r.source}")

        context = "\n\n".join(context_parts)

        return context, sources


# =============================================================================
# УДОБНАЯ ФУНКЦИЯ ДЛЯ ИСПОЛЬЗОВАНИЯ
# =============================================================================

# Глобальный instance
_retrieval_instance: Optional[EnhancedRetrieval] = None


def get_retrieval() -> EnhancedRetrieval:
    """Возвращает singleton instance EnhancedRetrieval"""
    global _retrieval_instance
    if _retrieval_instance is None:
        _retrieval_instance = EnhancedRetrieval()
    return _retrieval_instance


async def enhanced_search(query: str,
                         keywords: List[str] = None,
                         context_topic: Optional[str] = None,
                         dynamic_context: "DynamicContext" = None) -> Tuple[str, List[str]]:
    """Удобная функция для поиска

    Args:
        query: поисковый запрос
        keywords: ключевые слова
        context_topic: контекст темы
        dynamic_context: динамический контекст (опционально)

    Returns:
        Tuple[context, sources]
    """
    retrieval = get_retrieval()
    return await retrieval.search(query, keywords, context_topic, dynamic_context)


# Type hint import (в конце файла для избежания циклических импортов)
try:
    from .context import DynamicContext
except ImportError:
    DynamicContext = None
