"""
Structured Lookup — слой L1 между FAQ (L0) и MCP (L3).

Pattern matching по загруженным YAML-данным (core/topics.py:TOPICS).
Инжектирует структурированный контекст в bot_context для LLM.

Не заменяет MCP — дополняет его точными данными из RAM.

Паттерны (приоритет):
1. День N → get_topics_for_day(N)
2. Неделя N → topics для 7 дней
3. Все темы / программа → все 28 topics
4. Заголовок темы → substring match в title/title_en/title_es
5. Концепт → main_concept + related_concepts
"""

import re
from dataclasses import dataclass, field
from typing import Optional, List

from core.topics import TOPICS, get_topics_for_day, get_topic_title, load_topic_metadata


@dataclass
class StructuredHit:
    """Результат structured lookup."""
    pattern: str           # "day", "week", "all_topics", "title", "concept"
    topics: List[dict]     # найденные темы
    label: str = ""        # описание для LLM ("День 5", "Неделя 2", ...)
    query_param: str = ""  # параметр из запроса (число дня, слово концепта)


# --- Pattern matchers ---

_DAY_RU = re.compile(r'\b(?:день|дня|дню|днём|дне)\s*(\d{1,2})\b', re.IGNORECASE)
_DAY_EN = re.compile(r'\bday\s*(\d{1,2})\b', re.IGNORECASE)
_DAY_ES = re.compile(r'\bd[ií]a\s*(\d{1,2})\b', re.IGNORECASE)

_WEEK_RU = re.compile(r'\b(?:недел[яиеюь]|неделю)\s*(\d)\b', re.IGNORECASE)
_WEEK_EN = re.compile(r'\bweek\s*(\d)\b', re.IGNORECASE)
_WEEK_ES = re.compile(r'\bsemana\s*(\d)\b', re.IGNORECASE)

# "вторая неделя", "первая неделя"
_WEEK_ORDINAL_RU = re.compile(
    r'\b(перв|втор|1-?|2-?)\w*\s+недел', re.IGNORECASE
)

_ALL_TOPICS_RU = ["все темы", "программа марафона", "список тем", "какие темы",
                   "что входит", "содержание марафона", "о чём марафон",
                   "расписание марафона", "план марафона"]
_ALL_TOPICS_EN = ["all topics", "marathon program", "topic list", "what topics",
                   "marathon content", "marathon schedule", "marathon plan"]
_ALL_TOPICS_ES = ["todos los temas", "programa del maratón", "lista de temas"]


def _match_day(question: str) -> Optional[StructuredHit]:
    """Паттерн 1: День N."""
    for pattern in (_DAY_RU, _DAY_EN, _DAY_ES):
        m = pattern.search(question)
        if m:
            day = int(m.group(1))
            if 1 <= day <= 14:
                topics = get_topics_for_day(day)
                if topics:
                    return StructuredHit(
                        pattern="day",
                        topics=topics,
                        label=f"День {day}",
                        query_param=str(day),
                    )
    return None


def _match_week(question: str) -> Optional[StructuredHit]:
    """Паттерн 2: Неделя N."""
    week_num = None

    for pattern in (_WEEK_RU, _WEEK_EN, _WEEK_ES):
        m = pattern.search(question)
        if m:
            week_num = int(m.group(1))
            break

    if week_num is None:
        m = _WEEK_ORDINAL_RU.search(question)
        if m:
            ordinal = m.group(1).lower()
            if ordinal.startswith(("перв", "1")):
                week_num = 1
            elif ordinal.startswith(("втор", "2")):
                week_num = 2

    if week_num and week_num in (1, 2):
        start_day = 1 if week_num == 1 else 8
        end_day = 7 if week_num == 1 else 14
        topics = [t for t in TOPICS if start_day <= t['day'] <= end_day]
        if topics:
            return StructuredHit(
                pattern="week",
                topics=topics,
                label=f"Неделя {week_num} (дни {start_day}–{end_day})",
                query_param=str(week_num),
            )
    return None


def _match_all_topics(question: str) -> Optional[StructuredHit]:
    """Паттерн 3: все темы / программа."""
    q = question.lower()
    all_keywords = _ALL_TOPICS_RU + _ALL_TOPICS_EN + _ALL_TOPICS_ES
    if any(kw in q for kw in all_keywords):
        return StructuredHit(
            pattern="all_topics",
            topics=list(TOPICS),
            label="Все темы марафона",
        )
    return None


def _match_title(question: str) -> Optional[StructuredHit]:
    """Паттерн 4: совпадение с заголовком темы."""
    q = question.lower()
    matched = []
    for topic in TOPICS:
        titles = [
            topic.get('title', '').lower(),
            topic.get('title_en', '').lower(),
            topic.get('title_es', '').lower(),
        ]
        for title in titles:
            if title and len(title) > 3 and title in q:
                matched.append(topic)
                break
            # Обратный поиск: слова из вопроса в заголовке
            if title and len(q) > 5:
                # Ищем существенные слова (>4 символов) из вопроса в заголовке
                q_words = [w for w in q.split() if len(w) > 4]
                if q_words and sum(1 for w in q_words if w in title) >= 2:
                    matched.append(topic)
                    break

    if matched:
        return StructuredHit(
            pattern="title",
            topics=matched[:3],
            label="Совпадение по названию темы",
        )
    return None


def _match_concept(question: str) -> Optional[StructuredHit]:
    """Паттерн 5: совпадение с main_concept или related_concepts."""
    q = question.lower()
    scored: list[tuple[int, dict]] = []

    for topic in TOPICS:
        score = 0
        main = topic.get('main_concept', '').lower()
        related = [c.lower() for c in topic.get('related_concepts', [])]

        if main and main in q:
            score += 3
        elif main:
            # Частичное совпадение: слова из main_concept в вопросе
            main_words = [w for w in main.split() if len(w) > 3]
            score += sum(1 for w in main_words if w in q)

        for concept in related:
            if concept in q:
                score += 2
            else:
                concept_words = [w for w in concept.split() if len(w) > 3]
                score += sum(0.5 for w in concept_words if w in q)

        if score >= 2:
            scored.append((score, topic))

    if scored:
        scored.sort(key=lambda x: -x[0])
        return StructuredHit(
            pattern="concept",
            topics=[t for _, t in scored[:3]],
            label="Совпадение по концепции",
        )
    return None


# --- Public API ---

def structured_lookup(question: str, lang: str = 'ru') -> Optional[StructuredHit]:
    """Pattern matching по TOPICS. Возвращает StructuredHit или None.

    Порядок проверки (приоритет):
    1. День N
    2. Неделя N
    3. Все темы
    4. Заголовок
    5. Концепт
    """
    if not TOPICS:
        return None

    for matcher in (_match_day, _match_week, _match_all_topics, _match_title, _match_concept):
        hit = matcher(question)
        if hit:
            return hit

    return None


def format_structured_context(hit: StructuredHit, lang: str = 'ru') -> str:
    """Форматирует StructuredHit в текст для system prompt (bot_context).

    Формат зависит от паттерна:
    - all_topics: компактный список (день + тип + название)
    - day/week: подробный (название + концепция + инсайт + связи)
    - title/concept: до 3 тем с деталями
    """
    if not hit or not hit.topics:
        return ""

    lines = [f"ДАННЫЕ МАРАФОНА ({hit.label}):"]

    if hit.pattern == "all_topics":
        lines.append("")
        current_day = 0
        for topic in hit.topics:
            day = topic.get('day', 0)
            if day != current_day:
                current_day = day
                lines.append(f"День {day}:")
            topic_type = "📖 Урок" if topic.get('type') == 'theory' else "✏️ Задание"
            title = get_topic_title(topic, lang)
            lines.append(f"  {topic_type}: {title}")
        lines.append("")
        lines.append(f"Итого: 14 дней, {len(hit.topics)} тем (уроки + задания)")

    elif hit.pattern in ("day", "week"):
        lines.append("")
        for topic in hit.topics:
            title = get_topic_title(topic, lang)
            topic_type = "📖 Урок" if topic.get('type') == 'theory' else "✏️ Задание"
            main_concept = topic.get('main_concept', '')
            key_insight = topic.get('key_insight', '')
            related = topic.get('related_concepts', [])

            lines.append(f"День {topic.get('day', '?')} — {topic_type}: {title}")
            if main_concept:
                lines.append(f"  Концепция: {main_concept}")
            if key_insight:
                lines.append(f"  Инсайт: {key_insight}")
            if related:
                lines.append(f"  Связанные: {', '.join(related)}")

            # Кросс-ссылки из topic YAML (P3 fix: Context Engineering Select)
            topic_id = topic.get('id', '')
            if topic_id:
                metadata = load_topic_metadata(topic_id)
                if metadata and 'previous_days_connection' in metadata:
                    connections = metadata['previous_days_connection']
                    lines.append("  Связи с предыдущими днями:")
                    for day_key, conn in connections.items():
                        link_text = conn.get('link', '')
                        if link_text:
                            lines.append(f"    • {day_key}: {link_text}")

            lines.append("")

    else:  # title, concept
        lines.append("")
        for topic in hit.topics:
            title = get_topic_title(topic, lang)
            topic_type = "📖 Урок" if topic.get('type') == 'theory' else "✏️ Задание"
            main_concept = topic.get('main_concept', '')
            key_insight = topic.get('key_insight', '')
            related = topic.get('related_concepts', [])
            pain_point = topic.get('pain_point', '')

            lines.append(f"День {topic.get('day', '?')} — {topic_type}: {title}")
            if main_concept:
                lines.append(f"  Концепция: {main_concept}")
            if key_insight:
                lines.append(f"  Инсайт: {key_insight}")
            if pain_point:
                lines.append(f"  Проблема: {pain_point}")
            if related:
                lines.append(f"  Связанные: {', '.join(related)}")

            # Кросс-ссылки из topic YAML (P3 fix: Context Engineering Select)
            topic_id = topic.get('id', '')
            if topic_id:
                metadata = load_topic_metadata(topic_id)
                if metadata and 'previous_days_connection' in metadata:
                    connections = metadata['previous_days_connection']
                    lines.append("  Связи с предыдущими днями:")
                    for day_key, conn in connections.items():
                        link_text = conn.get('link', '')
                        if link_text:
                            lines.append(f"    • {day_key}: {link_text}")

            lines.append("")

    lines.append("Используй эти данные в ответе. Они точные и из программы марафона.")
    return "\n".join(lines)
