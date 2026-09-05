from datetime import datetime, timezone

from scripts.wp117_backfill_nudge_receipts import parse_historical_receipt


NOW = datetime(2026, 8, 6, tzinfo=timezone.utc)


def test_parses_each_once_per_recipient_family():
    for key in (
        "nudge_sessions_10",
        "nudge_active_days_30",
        "nudge_stage_reached_4",
    ):
        receipt = parse_historical_receipt(
            f"notification-nudge:123:2026-07-31:{key}", NOW
        )
        assert receipt is not None
        assert receipt.recipient_chat_id == 123
        assert receipt.nudge_key == key
        assert receipt.delivered_at == NOW


def test_does_not_backfill_recurring_recognition():
    assert parse_historical_receipt(
        "notification-nudge:123:2026-07-31:nudge_agency_high", NOW
    ) is None


def test_rejects_malformed_external_id():
    assert parse_historical_receipt(
        "notification-nudge:not-a-chat:2026-07-31:nudge_sessions_10", NOW
    ) is None
