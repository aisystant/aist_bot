"""
WP-498 Ф5.1 — Mentor Context-Sufficiency Gate, шаг 1 (DP.M.386).

RAG-поиск релевантного PD.METHOD.* в PACK-personal ДО генерации ответа
Наставника (MIM.R.001 Режим 2, роль-компонент Преподаватель-предметник).
Детерминированный шаг вне генерирующей модели — DP.M.386 «Алгоритм», п.1:
"Найден PD.METHOD.* выше порога релевантности → в контекст передаётся
method_id + текст метода. Не найден → в контекст передаётся явный флаг
no_grounding_source."

Реюз, не новый RAG-backend: поиск идёт через тот же gateway_mcp.knowledge_search
(source_type='pack'), который уже используют pre-search для Навигатора/Диагноста
(context_pipeline.collect_pre_search) и search_knowledge tool. Порог релевантности
переиспользует единственный существующий в кодовой базе прецедент —
RetrievalConfig.min_relevance_score = 0.3 (engines/shared/retrieval.py) —
вместо изобретения нового числа "из воздуха".

Grounding здесь ýже, чем общий knowledge_search: годится ТОЛЬКО документ с
идентификатором PD.METHOD.* (метод личного развития, PACK-personal) — см.
DP.SC.197 §Инварианты. Прочие Pack-документы (даже релевантные) не считаются
grounding-источником для предметника — это осознанное сужение, не баг.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from clients.gateway_mcp import gateway_mcp

logger = logging.getLogger(__name__)

# DP.M.386 Алгоритм п.1: детерминированный порог релевантности (cosine similarity,
# 0..1). Совпадает с RetrievalConfig.min_relevance_score (retrieval.py) — точное
# число оставлено на реализацию Ф5 (DP.M.386 §Границы), выбран существующий
# прецедент вместо произвольного нового значения.
GROUNDING_RELEVANCE_THRESHOLD = 0.3

# Grounding-источник обязан быть конкретным методом PACK-personal, а не любым
# Pack-документом (DP.SC.197 §Инварианты: "ссылаться на конкретный PD.METHOD.*").
_METHOD_ID_PATTERN = re.compile(r"PD\.METHOD\.\d+", re.IGNORECASE)

# С запасом на фильтрацию: не все результаты source_type='pack' будут PD.METHOD.*
# (Pack поиск охватывает digital-platform/mim/personal вперемешку).
_SEARCH_LIMIT = 8

_MAX_METHOD_TEXT_CHARS = 2000


@dataclass
class GroundingResult:
    """Результат шага 1 context-sufficiency gate (DP.M.386)."""

    grounded: bool
    method_id: Optional[str] = None
    text: Optional[str] = None
    source: Optional[str] = None
    score: float = 0.0


def _extract_method_id(item: Dict[str, Any]) -> Optional[str]:
    """Достаёт PD.METHOD.NNN из полей результата поиска.

    Проверяет source/filename/github_url первыми (структурные поля), затем
    начало текста (заголовок документа часто содержит id методом markdown).
    """
    candidates = [
        item.get("source", ""),
        item.get("filename", ""),
        item.get("github_url", ""),
        (item.get("text", "") or item.get("content", ""))[:200],
    ]
    for candidate in candidates:
        if not candidate:
            continue
        match = _METHOD_ID_PATTERN.search(str(candidate))
        if match:
            return match.group(0).upper()
    return None


async def mentor_grounding_search(
    question: str,
    telegram_user_id: Optional[int] = None,
) -> GroundingResult:
    """DP.M.386 шаг 1: RAG-поиск PD.METHOD.* по PACK-personal.

    Детерминированный (не самооценка модели): найден метод с cosine score >=
    GROUNDING_RELEVANCE_THRESHOLD → grounded=True (method_id + текст). Иначе
    grounded=False — вызывающий код обязан передать явный флаг
    no_grounding_source в диспетчер-промпт (format_grounding_section ниже).

    Не бросает исключения наружу — при ошибке поиска возвращает grounded=False
    (честная деградация, DP.SC.197 §Режим отказа: технический сбой → явный
    отказ, не притворная персонализация).
    """
    if not question or not question.strip():
        return GroundingResult(grounded=False)

    try:
        results = await gateway_mcp.knowledge_search(
            query=question,
            limit=_SEARCH_LIMIT,
            source_type="pack",
            telegram_user_id=telegram_user_id,
        )
    except Exception as e:
        logger.warning(f"Mentor grounding search error: {e}")
        return GroundingResult(grounded=False)

    if not results:
        return GroundingResult(grounded=False)

    best: Optional[GroundingResult] = None
    for item in results:
        if not isinstance(item, dict):
            continue
        method_id = _extract_method_id(item)
        if not method_id:
            continue  # Pack-документ, но не PD.METHOD.* — не годится как grounding
        try:
            score = float(item.get("score", 0) or 0)
        except (TypeError, ValueError):
            score = 0.0
        if best is None or score > best.score:
            text = (item.get("text", "") or item.get("content", "") or "")[:_MAX_METHOD_TEXT_CHARS]
            best = GroundingResult(
                grounded=score >= GROUNDING_RELEVANCE_THRESHOLD,
                method_id=method_id,
                text=text,
                source=item.get("source", method_id),
                score=score,
            )

    if best is None:
        return GroundingResult(grounded=False)

    if not best.grounded:
        logger.info(
            f"Mentor grounding: found {best.method_id} but score={best.score:.3f} "
            f"< threshold={GROUNDING_RELEVANCE_THRESHOLD} → no_grounding_source"
        )
    else:
        logger.info(
            f"Mentor grounding: {best.method_id} score={best.score:.3f} → grounded"
        )

    return best


def format_grounding_section(result: GroundingResult, lang: str = "ru") -> str:
    """DP.M.386 шаг 2: результат шага 1 → блок диспетчер-промпта.

    Добавляется в system prompt ДО генерации (не проверяется post-hoc после
    ответа) — DP.M.386 «Почему один шаг, а не два независимых прохода».
    """
    if result.grounded and result.method_id:
        return (
            "\n\nGROUNDING-ГЕЙТ (Преподаватель-предметник, DP.M.386, шаг 1 — RAG "
            "вне модели):\n"
            f"Найден релевантный метод {result.method_id} "
            f"(source={result.source}, score={result.score:.2f}):\n"
            f"{result.text}\n\n"
            "Если в ответе активируется роль-компонент Преподаватель-предметник — "
            f"ОБЯЗАН сослаться на {result.method_id} явно (например: «по методу "
            f"{result.method_id}…»). Используй ИМЕННО этот текст, не общие знания "
            "модели о личном развитии."
        )
    return (
        "\n\nGROUNDING-ГЕЙТ (Преподаватель-предметник, DP.M.386, шаг 1 — RAG вне "
        "модели):\nРелевантный метод PD.METHOD.* НЕ найден (no_grounding_source).\n"
        "Если в ответе активируется роль-компонент Преподаватель-предметник — "
        "ЗАПРЕЩЕНО отвечать из общих знаний модели о личном развитии. ОБЯЗАН "
        "явно сказать, что точного метода на это не нашёл, и либо дать самый "
        "общий принцип с явной пометкой «без привязки к конкретному методу», "
        "либо честно отказаться от предметного ответа. Диагност/Навигатор/"
        "Преподаватель-лидер этим гейтом НЕ ограничены (DP.SC.197 §Инварианты)."
    )
