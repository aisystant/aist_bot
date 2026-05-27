from datetime import datetime, timezone, date

CHANNEL_WEIGHT_TG = 0.8
MIN_CHARS = 20


async def track_typing(chat_id: int, text: str, message_id: int) -> None:
    """Fire-and-forget: emit user_typing_tracked event for a TG message."""
    from helpers.dual_write import post_event, resolve_ory_id_from_chat

    if not text or len(text) < MIN_CHARS:
        return

    char_count = len(text)
    weighted = round(char_count * CHANNEL_WEIGHT_TG, 2)
    today = date.today().isoformat()

    account_id = await resolve_ory_id_from_chat(chat_id)
    if not account_id:
        return

    await post_event(
        source="aist-bot",
        external_id=f"typing-tg-{chat_id}-{message_id}",
        event_type="user_typing_tracked",
        schema_version="v1",
        occurred_at=datetime.now(timezone.utc),
        account_id=account_id,
        payload={
            "interface": "tg-bot",
            "date": today,
            "char_count": char_count,
            "weighted_chars": weighted,
        },
    )
