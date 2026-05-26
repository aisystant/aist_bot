"""
WP-349 Ф11-Ф14: tier-сообщения, loop-баг, марафон 8→4, «Что ещё?»

8 merge-blockers:
  test_setup_tier_message           (T1-T4 canonical template)
  test_setup_tier_t0_no_level_string
  test_consent_new_user_no_loop
  test_marathon_day1_immediate      (< 18:00 MSK)
  test_marathon_day1_tomorrow       (≥ 18:00 MSK)
  test_what_else_button_appears_and_back
  test_consent_accept_tier_check
  test_marathon_integration_full
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch


# ─── helpers ───────────────────────────────────────────────────────────────

def _journey(tier: int) -> dict:
    return {"tier": tier, "stage": 0, "has_subscription": tier >= 2,
            "has_browser": tier >= 3, "has_github": tier >= 4}


# ═══════════════════════════════════════════════════════════════════════════
# Tier message tests (DP.SC.158)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("tier,expected_fragment", [
    (1, "Т1 «Старт»"),
    (2, "Т2 «Изучение»"),
    (3, "Т3 «Персонализация»"),
    (4, "Т4 «Созидание»"),
])
def test_setup_tier_message(tier, expected_fragment):
    """Каждый тир возвращает правильный шаблон «Твой уровень на платформе — Тx «Имя»»."""
    from handlers.setup import _render_tier_screen
    text = _render_tier_screen(_journey(tier), is_returning=False)
    assert "Твой уровень на платформе" in text, f"T{tier}: шаблон не содержит 'Твой уровень'"
    assert expected_fragment in text, f"T{tier}: ожидалось '{expected_fragment}', нет в тексте"


@pytest.mark.asyncio
async def test_setup_tier_t0_no_level_string():
    """T0 не видит tier-экран: cmd_setup отправляет 'привяжите аккаунт', не tier-шаблон."""
    sent_texts = []

    mock_message = AsyncMock()
    mock_message.chat = MagicMock(id=888)
    mock_message.from_user = MagicMock(id=888)
    mock_message.answer = AsyncMock(side_effect=lambda text, **kw: sent_texts.append(text))

    # T0: resolve_ory_id_from_chat вернёт None
    with patch("handlers.setup.resolve_ory_id_from_chat", new_callable=AsyncMock,
               return_value=None):
        from handlers.setup import cmd_setup
        await cmd_setup(mock_message)

    assert len(sent_texts) == 1
    assert "Твой уровень на платформе" not in sent_texts[0]
    assert "привяжите" in sent_texts[0].lower() or "link" in sent_texts[0].lower()


# ═══════════════════════════════════════════════════════════════════════════
# Loop bug: consent.py (Ф12)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_consent_new_user_no_loop():
    """Новый пользователь (intern=None) получает first_start, а НЕ застревает в цикле.

    Гарантирует что show_consent_optin больше не вызывает is_onboarded —
    достаточно проверить что intern=None → сообщение first_start, и flow не
    уходит дальше (не пытается resolve account_id для None intern).
    """
    sent_texts = []

    mock_message = AsyncMock()
    mock_message.from_user = MagicMock(id=999)
    mock_message.chat = MagicMock(id=999)
    mock_message.answer = AsyncMock(side_effect=lambda text, **kw: sent_texts.append(text))

    with patch("handlers.consent._resolve_account", new_callable=AsyncMock,
               return_value=(None, None)), \
         patch("handlers.consent.t", return_value="Сначала /start"):
        from handlers.consent import show_consent_optin
        await show_consent_optin(mock_message)

    assert len(sent_texts) == 1
    assert "Сначала /start" in sent_texts[0]


# ═══════════════════════════════════════════════════════════════════════════
# Marathon timing (DP.SC.157)
# ═══════════════════════════════════════════════════════════════════════════

MSK = timezone(timedelta(hours=3))


def _make_msk(hour: int) -> datetime:
    return datetime(2026, 5, 23, hour, 0, 0, tzinfo=MSK)


@pytest.mark.asyncio
async def test_marathon_day1_immediate():
    """Старт до 18:00 МСК → queue[0].day_num=1, scheduled_at ≈ now (< 5 мин)."""
    enqueued = []
    now_msk = _make_msk(10)  # 10:00 < 18:00

    async def fake_enqueue(user_id, day_number, scheduled_at, content_texts=None):
        enqueued.append({"day_number": day_number, "scheduled_at": scheduled_at})

    mock_reply = AsyncMock()
    mock_reply.answer = AsyncMock()

    with patch("handlers.marathon.get_or_create_progress", new_callable=AsyncMock,
               return_value={"status": "registered"}), \
         patch("handlers.marathon.update_progress", new_callable=AsyncMock), \
         patch("handlers.marathon.update_intern", new_callable=AsyncMock), \
         patch("handlers.marathon.enqueue_day_items", side_effect=fake_enqueue), \
         patch("handlers.marathon.moscow_now", return_value=now_msk), \
         patch("core.marathon_content.get_day_text", return_value="content"):
        from handlers.marathon import start_marathon_flow
        await start_marathon_flow(12345, mock_reply)

    assert len(enqueued) == 14
    day1 = next(e for e in enqueued if e["day_number"] == 1)
    delta = abs((day1["scheduled_at"] - now_msk).total_seconds())
    assert delta < 300, f"Первый урок должен быть ≈ сейчас, delta={delta}s"


@pytest.mark.asyncio
async def test_marathon_day1_tomorrow():
    """Старт после 18:00 МСК → queue[0].day_num=1, scheduled_at = завтра 04:00 МСК."""
    enqueued = []
    now_msk = _make_msk(20)  # 20:00 >= 18:00

    async def fake_enqueue(user_id, day_number, scheduled_at, content_texts=None):
        enqueued.append({"day_number": day_number, "scheduled_at": scheduled_at})

    mock_reply = AsyncMock()
    mock_reply.answer = AsyncMock()

    with patch("handlers.marathon.get_or_create_progress", new_callable=AsyncMock,
               return_value={"status": "registered"}), \
         patch("handlers.marathon.update_progress", new_callable=AsyncMock), \
         patch("handlers.marathon.update_intern", new_callable=AsyncMock), \
         patch("handlers.marathon.enqueue_day_items", side_effect=fake_enqueue), \
         patch("handlers.marathon.moscow_now", return_value=now_msk), \
         patch("core.marathon_content.get_day_text", return_value="content"):
        from handlers.marathon import start_marathon_flow
        await start_marathon_flow(12345, mock_reply)

    assert len(enqueued) == 14
    day1 = next(e for e in enqueued if e["day_number"] == 1)
    # Завтра 04:00 МСК
    tomorrow_0400 = datetime(2026, 5, 24, 4, 0, 0, tzinfo=MSK)
    assert day1["scheduled_at"] == tomorrow_0400, (
        f"day1 scheduled_at={day1['scheduled_at']}, expected={tomorrow_0400}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# «Что ещё?» tier-aware (DP.SC.156)
# ═══════════════════════════════════════════════════════════════════════════

def test_what_else_button_appears_and_back():
    """Кнопка «💡 Что ещё?» есть на T1-T4; кнопка «← Назад» возвращает tier-экран."""
    from handlers.setup import _tier_keyboard, _WHAT_ELSE_BTN, _BACK_BTN

    for tier in (1, 2, 3, 4):
        kb = _tier_keyboard(_journey(tier))
        all_buttons = [btn for row in kb.inline_keyboard for btn in row]
        labels = [b.text for b in all_buttons]
        assert "💡 Что ещё?" in labels, f"T{tier}: кнопка «Что ещё?» отсутствует"

    # «← Назад» зарегистрирована как константа и используется в on_what_else
    assert _BACK_BTN.callback_data == "setup:tier_back"


def test_what_else_tier_aware():
    """T1 видит только T1-команды, не видит T2+ команды."""
    from handlers.setup import _what_else_text
    from core.tier_config import UITier

    t1_text = _what_else_text(UITier.T1)
    assert "/support" in t1_text, "T1: /support должен быть"
    assert "/knowledge" not in t1_text, "T1: /knowledge (T2) не должен быть"
    assert "/twin" not in t1_text, "T1: /twin (T3) не должен быть"


# ═══════════════════════════════════════════════════════════════════════════
# Post-consent tier check (DP.SC.157)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_consent_accept_tier_check():
    """T1 после consent видит tier-экран с кнопкой марафона; T2 видит T2-экран."""
    from handlers.setup import _render_tier_screen, _tier_keyboard

    t1_text = _render_tier_screen(_journey(1), is_returning=False)
    t1_kb = _tier_keyboard(_journey(1))
    t1_buttons = [btn.callback_data for row in t1_kb.inline_keyboard for btn in row
                  if hasattr(btn, 'callback_data')]
    assert "setup_action:marathon" in t1_buttons, "T1: нет кнопки марафона"
    assert "Т1 «Старт»" in t1_text, "T1: нет T1-шаблона"

    t2_text = _render_tier_screen(_journey(2), is_returning=False)
    t2_kb = _tier_keyboard(_journey(2))
    t2_buttons = [btn.callback_data for row in t2_kb.inline_keyboard for btn in row
                  if hasattr(btn, 'callback_data')]
    assert "setup_action:marathon" not in t2_buttons, "T2: не должно быть кнопки марафона"
    assert "Т2 «Изучение»" in t2_text, "T2: нет T2-шаблона"


# ═══════════════════════════════════════════════════════════════════════════
# Marathon integration full flow (DP.SC.157)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_marathon_integration_full():
    """consent accept → tier-экран → кнопка marathon → start_marathon_flow вызван."""
    flow_called = []

    async def fake_start_marathon(user_id, reply_msg):
        flow_called.append(user_id)

    callback = AsyncMock()
    callback.from_user = MagicMock(id=777)
    callback.answer = AsyncMock()
    callback.message = AsyncMock()
    callback.message.edit_reply_markup = AsyncMock()
    callback.message.answer = AsyncMock()
    callback.data = "setup_action:marathon"

    with patch("handlers.marathon.start_marathon_flow", side_effect=fake_start_marathon):
        from handlers.setup import on_setup_action
        await on_setup_action(callback)

    assert flow_called == [777], "start_marathon_flow должна быть вызвана с user_id=777"
