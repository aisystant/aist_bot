"""
WP-498 Ф11 — Наставник называет понятие (писатель события `concept_named`).

Контракт: РП-522 «Чек-лист участника экосистемы», абзац «Писатель «Наставник
называет понятие»» (утверждён пилотом 30.08, S0-правка 04.09, уточнение
пилота 05.09: код понятия участнику не показывать вовсе). Наставник (MIM.R.001,
Режим 2) по запросу участника («объясни через понятия IWE», «какой гейт мы
применили») называет применённое системное понятие по-русски и объясняет,
зачем оно; код понятия живёт только в служебном маркере и событии. Когда названное
понятие совпадает с элементом раздела М чек-листа (сегодня М10/М11/М14), бот
шлёт событие `concept_named.v1` в event-gateway; read-model чек-листа (РП-522)
закрывает по нему факт `practiced`.

Три детерминированных проверки до отправки события (пир-сессия
2026-09-05-06 с Kimi, ход 2-3 — маркер сам по себе ненадёжен, модель может
поставить его «на всякий случай»):
  1. под-интент запроса = `concept_naming` (см. states/common/consultation.py);
  2. код из маркера `CONCEPT_NAMED: <код>` есть в CONCEPT_FACT_MAP;
  3. в тексте ответа участнику (после снятия маркера) встречается одна из явных
     форм русского названия понятия (`answer_markers`, подстрока в нижнем
     регистре — ручной список, не морфология).

Словарь понятий = граф понятий платформы (WP-498 Ф10, 04.09): коды ниже —
живые узлы графа (`knowledge_concept_search_by_name`), проверены поиском
GRAPH_SNAPSHOT_DATE. В коде живёт ТОЛЬКО машинная привязка код → русское
название → М-факт → формы для проверки ответа; определения понятий — в Pack
(PACK-agent-rules), сюда не копируются (OwnerIntegrity). Обновление среза —
по запросу при изменении графа, автосинхронизации нет (контракт РП-522).

Транспорт: helpers/dual_write.post_event (fire-and-forget, HMAC). Отказ
шины (400 unknown_event_type / schema_invalid, сеть) — post_event сам пишет
warning с event_type и телом ответа; здесь перед отправкой пишется info,
чтобы в логах пара «отправлено → отклонено» читалась вместе.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from helpers.dual_write import post_event, resolve_ory_id_from_chat

logger = logging.getLogger(__name__)

# Дата среза графа понятий, по которому проверены коды CONCEPT_FACT_MAP.
GRAPH_SNAPSHOT_DATE = "2026-09-05"

EVENT_TYPE = "concept_named"
EVENT_SCHEMA_VERSION = "v1"
EVENT_SOURCE = "aist-bot"
EVENT_CHANNEL = "bot"

# Режим сессии участника (канон РП-522 §Два режима сессии): обычная рабочая
# сессия / сессия саморазвития (/lesson … /lesson-close, одна на день).
SESSION_TYPE_WORK = "work_session"
SESSION_TYPE_SELF_DEVELOPMENT = "self_development"
SESSION_TYPES = (SESSION_TYPE_WORK, SESSION_TYPE_SELF_DEVELOPMENT)
# В боте сегодня нет потока, который ставил бы self_development, — ключ
# session_ctx["session_type"] зарезервирован под него; дефолт — рабочая сессия.
SESSION_TYPE_CTX_KEY = "session_type"
DEFAULT_SESSION_TYPE = SESSION_TYPE_WORK

CONCEPT_MARKER_PREFIX = "CONCEPT_NAMED:"
# Маркер — отдельная строка в конце ответа модели; участник его не видит
# (снимается extract_concept_marker до отправки). Регэксп терпим к типичному
# «самодеятельному» оформлению модели (холодное ревью 05.09, Critical):
# markdown-обрамление (*, **, _, `), буллет/цитата в начале строки, пробел
# перед двоеточием, точка в конце, CRLF-окончание строки. Иначе строка не
# снималась бы и утекала
# участнику дословно. Класс кода тот же, что в схеме concept_named.v1.json.
_MARKER_LINE_RE = re.compile(
    r"^[ \t]*[*_`>\-]*[ \t]*CONCEPT_NAMED[ \t]*:[ \t]*"
    r"([A-Z]+(?:\.[A-Z]+)*\.\d{3})"
    r"[ \t]*[.*_`]*[ \t]*\r?$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ConceptEntry:
    """Машинная привязка понятия IWE к М-факту чек-листа."""

    name_ru: str
    fact_id: str
    # Человекочитаемое название документа-источника для цитаты в ответе
    # (S0: код и путь к файлу участнику не показываются — решение пилота 05.09).
    source_title: str
    # Явные формы названия в нижнем регистре для проверки 3 (подстрока).
    answer_markers: Tuple[str, ...]


CONCEPT_FACT_MAP: Dict[str, ConceptEntry] = {
    "AR.003": ConceptEntry(
        name_ru="АрхГейт",
        fact_id="М10",
        source_title="правило «АрхГейт» свода правил агентов IWE",
        answer_markers=("архгейт", "archgate"),
    ),
    "AR.D.003": ConceptEntry(
        name_ru="Стоп-краны",
        fact_id="М11",
        source_title="различение «Стоп-краны как практика ≠ отдельный гейт»",
        answer_markers=("стоп-кран", "стоп кран", "pre-action gate"),
    ),
    "AR.D.004": ConceptEntry(
        name_ru="Экзоскелетный режим",
        fact_id="М14",
        source_title="различение «Экзоскелет ≠ Автопилот»",
        answer_markers=("экзоскелет",),
    ),
}


def format_concept_naming_section(lang: str = "ru") -> str:
    """Блок диспетчер-промпта для под-интента «назвать понятие».

    Добавляется в system prompt ДО генерации (тот же приём, что
    mentor_grounding.format_grounding_section): список допустимых понятий с
    источниками + правила формы ответа (S0) + инструкция про служебный маркер.
    Только русский: интент определён каноном для русскоязычного участника,
    lang оставлен ради единообразия сигнатуры с grounding-гейтом.
    """
    lines = [
        "",
        "",
        "ИНТЕНТ «НАЗВАТЬ ПОНЯТИЕ» (Преподаватель-предметник, чек-лист участника, раздел «Мастерство IWE»):",
        "Участник просит назвать системное понятие / гейт / метод IWE, который был применён "
        "в его недавней работе (смотри историю диалога), и объяснить, зачем оно.",
        "СЛОВАРЬ ПОНЯТИЙ, которые засчитываются как факт мастерства (срез графа понятий "
        f"платформы от {GRAPH_SNAPSHOT_DATE}):",
    ]
    for code, entry in CONCEPT_FACT_MAP.items():
        lines.append(f"- {entry.name_ru} ({code}) — источник: {entry.source_title}.")
    lines.extend([
        "ПРАВИЛА ОТВЕТА:",
        "1. Понятие называй по-русски: «АрхГейт», «Стоп-краны». Служебный код понятия (AR.003 и "
        "т.п.) участнику в тексте НЕ показывай — он нужен только в служебной строке из п.4.",
        "2. Источник называй по-русски, как называется документ (например «правило «АрхГейт» "
        "свода правил агентов»). Коды документов, пути к файлам, имена папок и репозиториев "
        "участнику НЕ показывай.",
        "3. Объясни в 2-4 предложениях, что именно в работе участника было этим понятием и "
        "зачем оно (какую ошибку предотвращает / что даёт).",
        "4. Если применённое понятие есть в словаре выше — последней строкой ответа добавь ровно "
        f"одну служебную строку вида «{CONCEPT_MARKER_PREFIX} <код>» (пример: "
        f"«{CONCEPT_MARKER_PREFIX} AR.D.003») — без markdown-оформления, без точки, отдельной строкой. "
        "Участник эту строку не увидит.",
        "5. Если применённое понятие IWE есть, но его нет в словаре — назови его честно, без "
        "служебной строки. Если из истории не видно, что какое-то понятие применялось, — скажи "
        "прямо, что не видишь применённого понятия, и спроси, о какой работе речь. "
        "Служебную строку «на всякий случай» ставить ЗАПРЕЩЕНО: она засчитывает факт мастерства.",
        "6. Блок GROUNDING-ГЕЙТ (поиск метода практикума) к этому интенту не прилагается: "
        "источник здесь — словарь понятий выше, запрет отвечать «из общих знаний» действует "
        "в форме «понятия вне словаря — без служебной строки».",
    ])
    return "\n".join(lines)


def extract_concept_marker(answer: str) -> Tuple[str, Optional[str]]:
    """Снимает служебные строки маркера из ответа и возвращает (чистый ответ, код).

    Несколько маркеров — неоднозначно: все снимаются, код не возвращается
    (модель не должна называть два понятия одним ответом-фактом).
    """
    matches = _MARKER_LINE_RE.findall(answer)
    clean = _MARKER_LINE_RE.sub("", answer).rstrip()
    if not matches:
        return clean, None
    if len(matches) > 1:
        logger.warning("Mentor concept_named: %d markers in one answer, ignored: %s", len(matches), matches)
        return clean, None
    return clean, matches[0]


def validate_named_concept(concept_id: Optional[str], clean_answer: str) -> Optional[ConceptEntry]:
    """Проверки 2 и 3: код в словаре И русское название понятия есть в ответе."""
    if not concept_id:
        return None
    entry = CONCEPT_FACT_MAP.get(concept_id)
    if entry is None:
        # Контракт РП-522: понятие вне словаря — кандидат для Экстрактора, не М-факт.
        logger.info("Mentor concept_named: %s not in CONCEPT_FACT_MAP — candidate for Extractor, no event", concept_id)
        return None
    answer_lower = clean_answer.lower()
    if not any(marker in answer_lower for marker in entry.answer_markers):
        logger.warning(
            "Mentor concept_named: marker %s but answer does not name «%s» — ignored",
            concept_id, entry.name_ru,
        )
        return None
    return entry


def resolve_session_type(session_ctx: dict) -> str:
    """Режим сессии из контекста консультации; неизвестное значение → дефолт с warning."""
    value = session_ctx.get(SESSION_TYPE_CTX_KEY, DEFAULT_SESSION_TYPE)
    if value not in SESSION_TYPES:
        logger.warning("Mentor concept_named: unknown session_type %r → %s", value, DEFAULT_SESSION_TYPE)
        return DEFAULT_SESSION_TYPE
    return value


def build_external_id(account_id: str, concept_id: str, occurred_at: datetime) -> str:
    """Идемпотентность по дню: тот же участник + понятие + день → один external_id."""
    return f"concept-named-{account_id}-{concept_id}-{occurred_at.strftime('%Y%m%d')}"


async def emit_concept_named(chat_id: int, entry_code: str, entry: ConceptEntry, session_type: str) -> bool:
    """Отправляет concept_named.v1 в event-gateway. True — событие поставлено в отправку.

    Без Ory-аккаунта (T0, не привязан) факт не к кому привязать — пропуск с логом,
    не ошибка. Сама отправка — fire-and-forget через post_event (ловит всё сам).
    """
    account_id = await resolve_ory_id_from_chat(chat_id)
    if not account_id:
        logger.info("Mentor concept_named: %s named but no account for chat — event skipped", entry_code)
        return False
    occurred_at = datetime.now(timezone.utc)
    logger.info(
        "Mentor concept_named: emitting %s (%s, fact %s, %s)",
        entry_code, entry.name_ru, entry.fact_id, session_type,
    )
    asyncio.create_task(post_event(
        source=EVENT_SOURCE,
        external_id=build_external_id(account_id, entry_code, occurred_at),
        event_type=EVENT_TYPE,
        schema_version=EVENT_SCHEMA_VERSION,
        occurred_at=occurred_at,
        account_id=account_id,
        payload={"concept_id": entry_code, "session_type": session_type, "channel": EVENT_CHANNEL},
    ))
    return True
