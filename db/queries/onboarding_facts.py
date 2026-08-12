"""
Onboarding facts read-model — WP-406 Ф33 §4.

Read-контракт для 4 фактов онбординга, которые сегодня знает только бот
(concept-checklist-data-ownership-2026-08-10.md §4, АрхГейт 10.08; детали
реализации — пир-сессия sessions/2026-08/11/2026-08-11-25-wp406-onboarding-facts-read).
Единственный потребитель — gateway-mcp через GET /internal/onboarding-facts
(oauth_server.py), формат каждого факта: {status, source, occurred_at}.

Статусная вокабула v2 (решение пилота 12.08.2026; вопрос вынесен сессиями
2026-08-11-25/-30 — «натяжка pending_evaluation» заменена явным статусом):
  confirmed — факт наблюдён в каноническом столе
  absent    — авторитетное «факта нет у владельца» (строки нет / отметки нет);
              в v1 кодировалось pending_evaluation, что конфликтовало с настоящим
              «сдано и ждёт оценщика» из конвейера диагностики (§2 концепции)
  unknown   — техническая недоступность источника ИЛИ пробел
              наблюдаемости (meeting_held: стола нет вообще, С2)

Retention-оговорка (Codex, ход 3): «нет строк в guide_render_queue => absent»
корректно, пока очередь не чистится — на 11.08.2026 ни activity-hub (владелец
схемы), ни потребитель render-pilot-guides.py строк не удаляют. Если появится
cleanup done-строк — семантика деградирует в unknown, менять здесь и бампать
contract_version.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from db.connection import get_pool, get_learning_pool, get_persona_pool

logger = logging.getLogger(__name__)

CONTRACT_VERSION = "onboarding-facts-v2"

_SRC_TELEGRAM = "persona.ory_identity"
_SRC_GUIDE = "learning.guide_render_queue"
_SRC_TRAJECTORY = "development.user_state.x3_completed_at"


def _fact(status: str, source: Optional[str], occurred_at: Optional[datetime]) -> Dict[str, Any]:
    return {
        "status": status,
        "source": source,
        "occurred_at": occurred_at.isoformat() if occurred_at else None,
    }


async def _telegram_fact(account_id: str) -> Tuple[Dict[str, Any], Optional[int]]:
    """telegram_linked + telegram_id для downstream-фактов.

    occurred_at = created_at строки ory_identity — приближение (строка могла
    появиться раньше привязки telegram_id через UPDATE), честнее чем ничего;
    зафиксировано в report.md сессии как известное упрощение v1.
    """
    try:
        pool = await get_persona_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT telegram_id, created_at FROM public.ory_identity"
                " WHERE account_id = $1::uuid",
                account_id,
            )
        if row and row["telegram_id"]:
            return _fact("confirmed", _SRC_TELEGRAM, row["created_at"]), int(row["telegram_id"])
        return _fact("absent", _SRC_TELEGRAM, None), None
    except Exception as exc:
        logger.warning(
            "[onboarding_facts] telegram_linked read failed for %s…: %s",
            str(account_id)[:8], exc,
        )
        return _fact("unknown", _SRC_TELEGRAM, None), None


async def _guide_fact(account_id: str) -> Dict[str, Any]:
    """guide_issued: «выдан» = успешное завершение render_pilot() = status='done'
    в очереди (допущение С1 концепции; enqueued ≠ issued — ход 5 сессии-26 10.08).
    Несколько done-строк => детерминизм: max(completed_at) (Codex, ход 3).

    Принятое ограничение v1 (review-01 Medium): dead_letter-строки (retry-бюджет
    исчерпан, авторендер не случится) тоже дают absent — «не выдан»
    для читателя верно, но «скоро будет» из этого статуса выводить нельзя.
    Различение failed-состояния = бамп contract_version, не тихая правка."""
    try:
        pool = await get_learning_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT MAX(completed_at) FILTER (WHERE status = 'done') AS done_at,"
                "       COUNT(*) AS total"
                " FROM learning.guide_render_queue WHERE account_id = $1::uuid",
                account_id,
            )
        if row and row["done_at"]:
            return _fact("confirmed", _SRC_GUIDE, row["done_at"])
        return _fact("absent", _SRC_GUIDE, None)
    except Exception as exc:
        logger.warning(
            "[onboarding_facts] guide_issued read failed for %s…: %s",
            str(account_id)[:8], exc,
        )
        return _fact("unknown", _SRC_GUIDE, None)


async def _trajectory_fact(telegram_id: Optional[int],
                           mapping_failed: bool) -> Dict[str, Any]:
    """trajectory_confirmed: допущение С3 — X3 (выбор траектории) закрыт."""
    if mapping_failed:
        # Ory->Telegram не резолвится по технической причине — читать user_state не по чему.
        return _fact("unknown", _SRC_TRAJECTORY, None)
    if telegram_id is None:
        # Привязки нет => X3 (бот-флоу) заведомо не проходился.
        return _fact("absent", _SRC_TRAJECTORY, None)
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            completed_at = await conn.fetchval(
                "SELECT x3_completed_at FROM development.user_state WHERE chat_id = $1",
                telegram_id,
            )
        if completed_at:
            return _fact("confirmed", _SRC_TRAJECTORY, completed_at)
        return _fact("absent", _SRC_TRAJECTORY, None)
    except Exception as exc:
        logger.warning(
            "[onboarding_facts] trajectory_confirmed read failed for chat %s: %s",
            telegram_id, exc,
        )
        return _fact("unknown", _SRC_TRAJECTORY, None)


async def collect_onboarding_facts(account_id: str) -> Dict[str, Dict[str, Any]]:
    """Собрать 4 факта контракта. Fail-open по фактам, не по ответу: ошибка
    одного источника даёт unknown этому факту, остальные отдаются (Т3, ход 0)."""
    telegram, telegram_id = await _telegram_fact(account_id)
    mapping_failed = telegram["status"] == "unknown"
    return {
        "telegram_linked": telegram,
        "guide_issued": await _guide_fact(account_id),
        # С2: стола нет ни у одного сервиса — пробел наблюдаемости, не «не сделано».
        "meeting_held": _fact("unknown", None, None),
        "trajectory_confirmed": await _trajectory_fact(telegram_id, mapping_failed),
    }
