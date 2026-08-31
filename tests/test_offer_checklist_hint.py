"""
WP-522 (пир-сессия 2026-08-31-07-wp522-checklist-caller-code-section-m):
`checklist_next_step_hint()` — первый боевой вызывающий код read-model
чек-листа участника внутри бота. Fail-open по конструкции: любая ошибка
(нет привязки, checklist-mcp недоступен/не настроен, сеть) -> None,
`_maybe_offer_onboarder` показывает оффер БЕЗ хинта — то же поведение,
что было до этой функции.
"""

import pytest
from unittest.mock import AsyncMock, patch

from core.onboarder import offer


@pytest.mark.asyncio
async def test_hint_none_when_not_linked():
    with patch("helpers.dual_write.resolve_ory_id_from_chat", new_callable=AsyncMock,
               return_value=None):
        assert await offer.checklist_next_step_hint(12345) is None


@pytest.mark.asyncio
async def test_hint_none_when_checklist_service_unavailable():
    """checklist_mcp.get_checklist_status() уже возвращает None на любой отказ
    (сеть/не настроен/HTTP-ошибка) — hint обязан деградировать так же тихо."""
    with patch("helpers.dual_write.resolve_ory_id_from_chat", new_callable=AsyncMock,
               return_value="acct-1"), \
         patch("clients.checklist_mcp.checklist_mcp.get_checklist_status", new_callable=AsyncMock,
               return_value=None):
        assert await offer.checklist_next_step_hint(12345) is None


@pytest.mark.asyncio
async def test_hint_none_on_unexpected_exception():
    """Fail-open даже на исключение, не только на None — оффер не должен падать
    из-за read-model чек-листа."""
    with patch("helpers.dual_write.resolve_ory_id_from_chat", new_callable=AsyncMock,
               side_effect=RuntimeError("network down")):
        assert await offer.checklist_next_step_hint(12345) is None


@pytest.mark.asyncio
async def test_hint_formats_stage_and_progress():
    status = {"summary": {"in_progress_stage": "Г", "stage_progress": "2/3"}}
    with patch("helpers.dual_write.resolve_ory_id_from_chat", new_callable=AsyncMock,
               return_value="acct-1"), \
         patch("clients.checklist_mcp.checklist_mcp.get_checklist_status", new_callable=AsyncMock,
               return_value=status):
        hint = await offer.checklist_next_step_hint(12345)
    assert hint == "Ты сейчас на этапе «Оснащение» (2/3)."


@pytest.mark.asyncio
async def test_hint_none_when_summary_incomplete():
    """confirmed_stage=None (все этапы пройдены) means in_progress_stage тоже
    None — нет смысла показывать пустой хинт."""
    status = {"summary": {"in_progress_stage": None, "stage_progress": None}}
    with patch("helpers.dual_write.resolve_ory_id_from_chat", new_callable=AsyncMock,
               return_value="acct-1"), \
         patch("clients.checklist_mcp.checklist_mcp.get_checklist_status", new_callable=AsyncMock,
               return_value=status):
        assert await offer.checklist_next_step_hint(12345) is None


def test_offer_payload_without_hint_unchanged():
    payload = offer.offer_payload()
    assert "Хочешь освоиться" in payload["text"]
    assert payload["button_text"] == "🎓 Освоиться"


def test_offer_payload_prepends_hint():
    payload = offer.offer_payload("Ты сейчас на этапе «Оснащение» (2/3).")
    assert payload["text"].startswith("Ты сейчас на этапе «Оснащение» (2/3).")
    assert "Хочешь освоиться" in payload["text"]
