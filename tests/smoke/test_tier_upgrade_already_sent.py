"""Upgrade nudges treat «already in the delivery journal» as sent (WP-568 F16 1b).

The delivery layer refuses a journaled key before the queue (reason
"already-sent"). The upgrade nudge must report it as sent: otherwise the
scheduler falls through to a second, generic nudge in the same tick.
"""
import pytest

import handlers.tier_upgrade as tu


@pytest.mark.asyncio
@pytest.mark.parametrize("reason, expected", [
    ("already-sent", True),    # sent earlier: no fallback nudge this tick
    ("cap-exceeded", False),   # really not sent: the scheduler may try another
])
async def test_onboarding_gap_reports_journaled_message_as_sent(monkeypatch, reason, expected):
    async def _enqueue(chat_id, klass, content, **kw):
        return {"notification_id": None, "status": "suppressed", "reason": reason}
    monkeypatch.setattr(tu, "enqueue", _enqueue)

    assert await tu.nudge_onboarding_gap_t2(None, 555, 3) is expected
