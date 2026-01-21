"""
Общие компоненты для всех режимов.

Содержит:
- question_handler.py: обработка вопросов пользователя
  (работает в любом режиме, использует MCP + контекст)
- retrieval.py: улучшенный Knowledge Retrieval с:
  - Query Expansion (расширение запросов связанными терминами)
  - Relevance Scoring (оценка релевантности)
  - Semantic Deduplication (умная дедупликация)
  - Fallback Strategy (стратегия при пустых результатах)
"""

from .question_handler import (
    handle_question,
    search_mcp_context,  # deprecated, use enhanced_search
    generate_answer,
    answer_with_context,
)

from .retrieval import (
    EnhancedRetrieval,
    QueryExpander,
    RelevanceScorer,
    SemanticDeduplicator,
    FallbackStrategy,
    RetrievalResult,
    RetrievalConfig,
    enhanced_search,
    get_retrieval,
    TERM_RELATIONS,
    SYNONYMS,
)

__all__ = [
    # Question Handler
    'handle_question',
    'search_mcp_context',
    'generate_answer',
    'answer_with_context',
    # Enhanced Retrieval
    'EnhancedRetrieval',
    'QueryExpander',
    'RelevanceScorer',
    'SemanticDeduplicator',
    'FallbackStrategy',
    'RetrievalResult',
    'RetrievalConfig',
    'enhanced_search',
    'get_retrieval',
    'TERM_RELATIONS',
    'SYNONYMS',
]
