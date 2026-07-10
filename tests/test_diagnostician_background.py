"""Unit tests for engines.diagnostician.background (WP-318 Ф9)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from engines.diagnostician.background import (
    CP_STALE_DAYS,
    cp_status,
    suggest_for_attestator,
    suggest_for_navigator,
    suggest_for_tailor,
)


def _assessment(age_days: int):
    assessed_at = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    return {"assessed_at": assessed_at, "valid_until": None}


@pytest.mark.asyncio
async def test_cp_status_fresh():
    with patch(
        "engines.diagnostician.background.get_latest_cp_assessment",
        new_callable=AsyncMock,
    ) as m:
        m.return_value = _assessment(CP_STALE_DAYS - 1)
        status = await cp_status("uuid")
        assert status["state"] == "fresh"
        assert status["age_days"] == CP_STALE_DAYS - 1


@pytest.mark.asyncio
async def test_cp_status_stale():
    with patch(
        "engines.diagnostician.background.get_latest_cp_assessment",
        new_callable=AsyncMock,
    ) as m:
        m.return_value = _assessment(CP_STALE_DAYS + 1)
        status = await cp_status("uuid")
        assert status["state"] == "stale"
        assert status["age_days"] == CP_STALE_DAYS + 1


@pytest.mark.asyncio
async def test_cp_status_missing():
    with patch(
        "engines.diagnostician.background.get_latest_cp_assessment",
        new_callable=AsyncMock,
    ) as m:
        m.return_value = None
        status = await cp_status("uuid")
        assert status["state"] == "missing"


@pytest.mark.asyncio
async def test_suggest_for_navigator_onboarding_no_cp():
    with patch(
        "engines.diagnostician.background.get_latest_cp_assessment",
        new_callable=AsyncMock,
    ) as m:
        m.return_value = None
        hint = await suggest_for_navigator("uuid", "с чего начать?")
        assert hint is not None
        assert "/diagnose" in hint


@pytest.mark.asyncio
async def test_suggest_for_navigator_no_keyword():
    with patch(
        "engines.diagnostician.background.get_latest_cp_assessment",
        new_callable=AsyncMock,
    ) as m:
        m.return_value = None
        hint = await suggest_for_navigator("uuid", "как дела?")
        assert hint is None


@pytest.mark.asyncio
async def test_suggest_for_navigator_fresh():
    with patch(
        "engines.diagnostician.background.get_latest_cp_assessment",
        new_callable=AsyncMock,
    ) as m:
        m.return_value = _assessment(10)
        hint = await suggest_for_navigator("uuid", "с чего начать?")
        assert hint is None


@pytest.mark.asyncio
async def test_suggest_for_tailor_stale():
    with patch(
        "engines.diagnostician.background.get_latest_cp_assessment",
        new_callable=AsyncMock,
    ) as m:
        m.return_value = _assessment(CP_STALE_DAYS + 5)
        hint = await suggest_for_tailor("uuid")
        assert hint is not None
        assert "/diagnose" in hint


@pytest.mark.asyncio
async def test_suggest_for_tailor_fresh():
    with patch(
        "engines.diagnostician.background.get_latest_cp_assessment",
        new_callable=AsyncMock,
    ) as m:
        m.return_value = _assessment(10)
        hint = await suggest_for_tailor("uuid")
        assert hint is None


@pytest.mark.asyncio
async def test_suggest_for_attestator_stale():
    with patch(
        "engines.diagnostician.background.get_latest_cp_assessment",
        new_callable=AsyncMock,
    ) as m:
        m.return_value = _assessment(CP_STALE_DAYS + 5)
        hint = await suggest_for_attestator("uuid", new_bh_stage=2)
        assert hint is not None
        assert "/diagnose" in hint


@pytest.mark.asyncio
async def test_suggest_for_attestator_fresh():
    with patch(
        "engines.diagnostician.background.get_latest_cp_assessment",
        new_callable=AsyncMock,
    ) as m:
        m.return_value = _assessment(10)
        hint = await suggest_for_attestator("uuid", new_bh_stage=2)
        assert hint is None
