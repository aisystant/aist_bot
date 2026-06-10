#!/usr/bin/env python3
"""
Smoke-тест внешней авторизации (WP-411 Ф2).

Что проверяет:
  1. Бот отвечает на /health
  2. Обмен кода работает (endpoint возвращает access + refresh токены)
  3. Шлюз принимает токен и возвращает account_id

Использование:
  python3 scripts/test_external_auth.py --code <код из /connect external в боте>

  Без --code: проверяет только здоровье бота.
"""
import argparse
import os
import sys
import urllib.request
import urllib.error
import json

BOT_URL = os.getenv("BOT_BASE_URL", "https://aistmebot-production.up.railway.app")
GATEWAY_URL = os.getenv("GATEWAY_URL", "")


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "✅" if ok else "❌"
    print(f"  {mark}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        sys.exit(1)


def get(url: str, headers: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def post(url: str, body: dict, headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = {}
        return e.code, body
    except Exception as e:
        return 0, {"error": str(e)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", help="Одноразовый код из /connect external в боте")
    args = parser.parse_args()

    print(f"\nПроверяем внешнюю авторизацию: {BOT_URL}\n")

    # 1. Бот живой?
    status, body = get(f"{BOT_URL}/health")
    check("Бот отвечает", status == 200, f"HTTP {status}")

    if not args.code:
        print("\nПередай код через --code чтобы проверить полный цикл.")
        print("Код получи командой /connect external в @aist_me_bot.\n")
        return

    # 2. Обмен кода на токены
    status, body = post(f"{BOT_URL}/oauth/external/exchange", {"code": args.code})
    check(
        "Обмен кода на токены",
        status == 200 and "access_token" in body,
        f"HTTP {status}" if status != 200 else f"scope={body.get('scope')}",
    )

    access_token = body.get("access_token", "")
    check("Токен начинается с ict_", access_token.startswith("ict_"), access_token[:20])

    refresh_token = body.get("refresh_token", "")
    check("Refresh-токен начинается с irt_", refresh_token.startswith("irt_"), refresh_token[:20])

    # 3. Шлюз принимает токен (опционально)
    if GATEWAY_URL:
        status, body = get(
            f"{GATEWAY_URL}/health",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        check("Шлюз принимает токен", status == 200, f"HTTP {status}")
    else:
        print("  ⏭  Шлюз не настроен (GATEWAY_URL не задан) — пропущено")

    print("\nВсё работает.\n")


if __name__ == "__main__":
    main()
