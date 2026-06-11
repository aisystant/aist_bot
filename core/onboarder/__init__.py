"""
Онбордер (DP.ROLE.067) — каркас модуля входа (WP-406 Ф5, «Фундамент»).

# see DP.SC.170, DP.ROLE.067

Онбордер доводит нового человека до состояния «Первокурсник» — закрывает две
характеристики готовности:
  Х2 — понимание сообщества (что за сообщество, роли, нормы, куда обращаться).
  Х3 — выбор траектории (ступень 1-5 + узкое место + первый курс).

Архитектура (консенсус peer-сессии 2026-06-11-07-wp406-onboarder-f5-impl):
  - Онбордер = тонкий ОРКЕСТРАТОР поверх существующих машин, не третья FSM.
  - Х3 делегируется готовому Диагносту R28 (handlers/diagnose.py): ступень +
    узкое место уже считаются, `recommended_stream` уже в проде
    (db/queries/cp_assessment.py). Онбордер транслирует поток в человеко-
    читаемый оффер курса (см. core/onboarder/x3.py).
  - Х2 — структурированный мини-тест из 4 вопросов в режиме teach-confirm
    (дай ориентацию → подтверди), БЕЗ fail-state: инвариант обещания
    «Онбордер не бросает» (DP.SC.170). См. core/onboarder/x2.py.
  - Хранилище: отметки завершения `x2_completed_at` / `x3_completed_at` в
    development.user_state (миграция 030, queryable для проактивных напоминаний
    и Ф6); черновики/last_gap — в current_context['onboarding']. См. storage.py.
  - Вход — explicit-триггер, НЕ перехват свободного текста (консенсус peer-сессии
    2026-06-11-16). Кнопка «Освоиться» (handlers/onboarding.py) и проактивный
    нудж (scheduler, follow-up) вызывают handle(); handle сам выбирает первый
    открытый разрыв (Х2 → Х3). Перехват произвольных сообщений в Dispatcher
    отвергнут как рискованный (риск проглотить консультацию/заметку); explicit-
    модель консистентна с уже подключённым Х3.

Достижимость: кнопка «Освоиться» показывается там, куда новый человек реально
попадает после /start (Экран A/B быстрого пути) и в приветствии возвращающегося,
под гейтом has_open_gap. Это закрывает дефект, найденный в peer-сессии: кнопка Х3
в старом FSM-экране confirming_profile для нового пользователя недостижима.
"""

import logging

from core.onboarder import offer, storage, x2, x3

logger = logging.getLogger(__name__)

__all__ = [
    "offer",
    "storage",
    "x2",
    "x3",
    "get_status",
    "has_open_gap",
    "handle",
]


async def get_status(chat_id: int) -> dict:
    """Готовность Первокурсника по двум характеристикам Онбордера.

    Returns:
        {"x2_done": bool, "x3_done": bool,
         "x2_completed_at": datetime | None, "x3_completed_at": datetime | None}
    """
    return await storage.get_status(chat_id)


async def has_open_gap(chat_id: int) -> bool:
    """Есть ли незакрытая характеристика Онбордера (Х2 или Х3).

    Только для пользователей, прошедших базовую регистрацию
    (onboarding_completed). Это третий уровень защиты Dispatcher —
    «зачем звать Онбордера». Без открытого разрыва Онбордер молчит.
    """
    status = await storage.get_status(chat_id)
    return not (status["x2_done"] and status["x3_done"])


async def handle(intern: dict, message) -> None:
    """Единая точка входа Онбордера: довести первый открытый разрыв (Х2 → Х3).

    Вызывается из inline-кнопки «Освоиться» (handlers/onboarding.py) и из
    проактивного нуджа (scheduler, follow-up). Выбирает первый незакрытый
    разрыв и запускает соответствующий срез:
      - Х2 (понимание сообщества) — раньше Х3: ориентация готовит осознанный
        выбор курса.
      - Х3 (выбор траектории) — после Х2 либо если Х2 уже закрыт.

    Идемпотентен: если оба разрыва закрыты — ничего не шлёт. В живом роутинге
    кнопка под гейтом has_open_gap, поэтому при обоих закрытых handle не зовётся;
    проверка здесь — защита от прямого вызова.
    """
    chat_id = intern.get("chat_id") or message.chat.id
    status = await storage.get_status(chat_id)
    if not status["x2_done"]:
        await x2.run_step(intern, message)
    elif not status["x3_done"]:
        await x3.run_x3(intern, message)
    else:
        logger.debug("[onboarder] handle called with no open gap for chat_id=%s", chat_id)
