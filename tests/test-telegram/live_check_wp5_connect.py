"""
Живая проверка push+VS Code one-click подключения внешних AI-клиентов (WP-5, DP.SC.190).

Не pytest-suite — разовый скрипт по образцу live_check_x2_loop.py.

Проверяет:
  1. /connect показывает меню с пунктом "VS Code".
  2. Клик по "VS Code" отдаёт инструкцию + кнопку-диплинк vscode:mcp/install?...
  3. Диплинк содержит валидный (без токена) JSON-конфиг {"type":"http","url":...}.

НЕ проверяет (не покрывается этим скриптом):
  - Push-нудж при пересечении T3/T4 (требует манипуляции тиром тестового аккаунта в БД).
  - Отзыв ict_-токена при падении тира (требует существующего ict_-подключения).
  - Реальное открытие VS Code по диплинку (за пределами Telegram/бота).

Запуск:
    source .venv/bin/activate
    python3 tests/test-telegram/live_check_wp5_connect.py
"""

import asyncio
import json
import os
import sys
import urllib.parse

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

    ok = True
    try:
        await client.send_command("/connect")
        await asyncio.sleep(0.3)
        menu = await client.wait_for_text("Выбери свой AI-ассистент", timeout=15.0)
        if not menu:
            print("FAIL: бот не ответил меню /connect (chat зашумлён другими рассылками)")
            return
        if not menu.has_button("VS Code"):
            print(f"FAIL: в меню /connect нет кнопки VS Code. Ответ: {menu.text[:200]!r}")
            print(f"      кнопки: {menu.inline_buttons}")
            ok = False
        else:
            print("PASS: кнопка VS Code присутствует в меню /connect")

        clicked = await client.click_button(menu, "VS Code")
        if not clicked:
            print("FAIL: не удалось нажать кнопку VS Code")
            ok = False
        else:
            # on_vscode делает edit_text того же сообщения (не шлёт новое) — перечитываем его по id.
            await asyncio.sleep(2)
            edited = await client.client.get_messages(client.bot_entity, ids=menu.message.id)
            deep_link = None
            if edited and edited.reply_markup:
                for row in edited.reply_markup.rows:
                    for btn in row.buttons:
                        url = getattr(btn, "url", None)
                        if url and url.startswith("vscode:mcp/install?"):
                            deep_link = url
            if not deep_link:
                print(f"FAIL: не нашёл кнопку с диплинком vscode:mcp/install после клика. "
                      f"Текст: {(edited.text if edited else '')[:200]!r}")
                ok = False
            else:
                encoded = deep_link.split("?", 1)[1]
                cfg = json.loads(urllib.parse.unquote(encoded))
                assert cfg.get("type") == "http", cfg
                assert cfg.get("url"), cfg
                assert "headers" not in cfg, "не должно быть токена в диплинке"
                print(f"PASS: диплинк валиден, без токена: {deep_link}")
    finally:
        await client.stop()

    print("\n=== ИТОГ:", "PASS" if ok else "FAIL", "===")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
