"""Unit-тесты писателя `onboarding_completed.v1` (WP-522 Ф11 волна 2, WP-406 Ф18).

Покрывает: log_event + post_event оба вызываются при наличии account_id;
post_event пропускается (не log_event) без account_id; конверт события
(source/event_type/schema_version/payload={}); external_id по дню, не по
closed_by (гейт на call site — newly_marked — уже исключает повторный вызов
для одного account_id).
"""

import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from core.onboarder.events import build_external_id, emit_onboarding_completed  # noqa: E402


@pytest.mark.asyncio
async def test_emit_logs_and_posts_with_account():
    with patch("db.queries.events.log_event", new=AsyncMock()) as log_ev, \
         patch("core.onboarder.events.resolve_ory_id_from_chat", new=AsyncMock(return_value="ory-uuid")), \
         patch("core.onboarder.events.post_event", new=AsyncMock()) as post, \
         patch("core.onboarder.events.asyncio.create_task", side_effect=lambda coro: coro.close()):
        await emit_onboarding_completed(42, "direct", "bot", "ru", closed_by="x2")

    log_ev.assert_awaited_once_with(42, "onboarding_completed", {
        "entry_type": "direct", "source": "bot", "lang": "ru", "closed_by": "x2",
    })
    kwargs = post.call_args.kwargs
    assert kwargs["source"] == "aist-bot"
    assert kwargs["event_type"] == "onboarding_completed"
    assert kwargs["schema_version"] == "v1"
    assert kwargs["account_id"] == "ory-uuid"
    assert kwargs["payload"] == {}  # closed_by намеренно не в каноне события — см. docstring
    assert kwargs["external_id"].startswith("onboarding-completed-ory-uuid-")


@pytest.mark.asyncio
async def test_emit_logs_but_skips_gateway_without_account():
    with patch("db.queries.events.log_event", new=AsyncMock()) as log_ev, \
         patch("core.onboarder.events.resolve_ory_id_from_chat", new=AsyncMock(return_value=None)), \
         patch("core.onboarder.events.post_event", new=AsyncMock()) as post:
        await emit_onboarding_completed(42, "direct", "bot", "ru", closed_by="x3")

    log_ev.assert_awaited_once()  # локальный лог не зависит от наличия Ory-аккаунта
    post.assert_not_called()


def test_external_id_stable_per_day():
    day = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)
    evening = datetime(2026, 9, 5, 22, 0, tzinfo=timezone.utc)
    next_day = datetime(2026, 9, 6, 1, 0, tzinfo=timezone.utc)
    assert build_external_id("acc", day) == build_external_id("acc", evening)
    assert build_external_id("acc", day) != build_external_id("acc", next_day)
