"""
Ручная живая проверка регрессии X2 message loop (WP-7, 2026-07-10/11).

Не pytest-suite (существующие tests/test-telegram/test_01_onboarding.py
писаны под старый, уже удалённый флоу выбора языка/имени — WP-79 их снял).
Разовый скрипт для проверки конкретного бага: двойной тап по кнопке
«Освоиться» больше не должен дублировать intro+topic.

Запуск:
    source .venv/bin/activate
    python3 tests/test-telegram/live_check_x2_loop.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from client import BotTestClient  # noqa: E402


def _load_env_test():
    env_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env.test")
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key, value)


async def main():
    _load_env_test()
    session_path = os.path.join(os.path.dirname(__file__), "e2e_test_session")
    client = BotTestClient(session_name=session_path)
    await client.start()

    try:
        offer_msg = await _find_onboarder_message(client)
        if offer_msg is None:
            print("SKIP: кнопка «Освоиться» не найдена в последних 50 сообщениях — "
                  "аккаунт уже прошёл X2/X3 или история пуста")
            return

        before_count = await _count_topic_messages(client)

        # Двойной тап подряд — та же ситуация, что у пилота (stale-кнопка).
        await offer_msg.click(data=b"onboarder_start")
        await offer_msg.click(data=b"onboarder_start")
        await asyncio.sleep(4)

        after_count = await _count_topic_messages(client)
        new_messages = after_count - before_count

        print(f"Новых сообщений с текстом темы X2 после двойного тапа: {new_messages}")
        if new_messages <= 1:
            print("PASS: дубля нет")
        else:
            print(f"FAIL: тема отправлена {new_messages} раз(а) — регрессия не устранена")
    finally:
        await client.stop()


async def _find_onboarder_message(client: BotTestClient):
    """Ищет сообщение с кнопкой callback_data == b'onboarder_start' среди последних."""
    from telethon.tl.types import ReplyInlineMarkup, KeyboardButtonCallback

    messages = await client.client.get_messages(client.bot_entity, limit=50)
    for msg in messages:
        if not msg.reply_markup or not isinstance(msg.reply_markup, ReplyInlineMarkup):
            continue
        for row in msg.reply_markup.rows:
            for btn in row.buttons:
                if isinstance(btn, KeyboardButtonCallback) and btn.data == b"onboarder_start":
                    return msg
    return None


async def _count_topic_messages(client: BotTestClient) -> int:
    messages = await client.client.get_messages(client.bot_entity, limit=50)
    return sum(1 for m in messages if m.text and "Что это за место" in m.text)


if __name__ == "__main__":
    asyncio.run(main())
