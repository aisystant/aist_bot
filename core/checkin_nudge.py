"""Missed check-in reminders: at most two per missed episode (РП-568 Ф16, DP.SC.116).

Pilot rule (11.11.2026): for one missed check-in send no more than two reminders —
the first the next day, the second three days after the first; then silence until
the participant comes back. The previous code sent «Проверка связи» every day once
three days were missed (≈27 reminders per person per month).

Episode = the run of missed days that starts at the first missed working day M
after the participant's last activity (check-in or resume after a pause). A new
activity closes the episode; the next miss opens a new one with a new M.

Stages use windows rather than exact days, so a skipped scheduler run or a cap
suppression on one day does not lose the reminder:
  stage 1 — on any day in [M+1, M+3];
  stage 2 — on any day in [S+3, S+5], where S is the Moscow date stage 1 was
            actually delivered; no stage 1 delivered → no stage 2.
Each stage is sent at most once: the journal key carries M and the stage number.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

STAGE1_WINDOW = (1, 3)          # days after M
STAGE2_WINDOW = (3, 5)          # days after the delivery date of stage 1

STAGE_TEXTS = {
    1: (
        "🌤 *Небольшая пауза*\n\n"
        "Вчера не получилось чекинуться — ничего страшного. "
        "Сегодня новый день и новый слот. Попробуем снова?\n\n"
        "Если хочешь сбросить марафон — /settings → «Мои данные» → "
        "«Управление данными» → «🔄 Сбросить марафон». "
        "Если нужна помощь — напиши /support."
    ),
    2: (
        "🤝 *Проверка связи*\n\n"
        "Несколько дней без чек-ина. Возможно, марафон идёт в фоне, "
        "а возможно, нужна помощь. Больше напоминать не будем — "
        "вернёшься, когда будет удобно.\n\n"
        "Чтобы начать с чистого листа — /settings → «Мои данные» → "
        "«Управление данными» → «🔄 Сбросить марафон».\n"
        "Напиши /support или просто ответь: что мешает?"
    ),
}


def journal_key(chat_id: int, episode_start: date, stage: int) -> str:
    return f"marathon_nudge:{chat_id}:{episode_start.isoformat()}:{stage}"


def due_stage(
    today: date,
    episode_start: date,
    stage1_sent_on: Optional[date],
    stage2_sent: bool,
) -> Optional[int]:
    """Which stage (1 or 2) is due today for this episode, or None."""
    if stage1_sent_on is None:
        first, last = STAGE1_WINDOW
        if episode_start + timedelta(days=first) <= today <= episode_start + timedelta(days=last):
            return 1
        return None
    if stage2_sent:
        return None
    first, last = STAGE2_WINDOW
    if stage1_sent_on + timedelta(days=first) <= today <= stage1_sent_on + timedelta(days=last):
        return 2
    return None
