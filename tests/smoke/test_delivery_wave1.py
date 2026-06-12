"""Smoke-тесты Ф4 волны 1 Доставщика (WP-418, peer-сессия 2026-06-12-06).

Три зоны:
1. Рендер канало-нейтрального content_spec в Telegram-kwargs (_build_delivery_kwargs).
2. Сторож очереди — fail-open при недоступной БД.
3. Инвариант миграции: в мигрированных функциях нет прямых bot.send_* (скан исходника,
   паттерн test_marathon_callback_coverage).
"""
import re
from pathlib import Path

import pytest

import core.scheduler as sched

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEDULER_SRC = (PROJECT_ROOT / "core" / "scheduler.py").read_text()

# Волна 1 + 1б: функции, переведённые на enqueue. Расширять при следующих волнах.
MIGRATED_SENDERS = [
    "_send_marathon_nudges",
    "send_engagement_nudges",
    "_send_practice_nudges",
]


# ─────────────────────────── рендер content_spec ───────────────────────────

def test_render_markdown_converts_to_html():
    text, kwargs = sched._build_delivery_kwargs(
        {"text": "*Пауза*", "format": "markdown"})
    assert kwargs["parse_mode"] == "HTML"
    assert "*" not in text            # Markdown-звёздочки не утекли пользователю
    assert "Пауза" in text            # содержимое не потеряно


def test_render_plain_passes_text_untouched():
    text, kwargs = sched._build_delivery_kwargs({"text": "просто текст"})
    assert text == "просто текст"
    assert "parse_mode" not in kwargs
    assert "reply_markup" not in kwargs


def test_render_actions_builds_inline_keyboard():
    _, kwargs = sched._build_delivery_kwargs({
        "text": "урок ждёт",
        "actions": [{"label": "✏️ Перейти к практике",
                     "action": "marathon_practice:3"}],
    })
    kb = kwargs["reply_markup"]
    button = kb.inline_keyboard[0][0]
    assert button.text == "✏️ Перейти к практике"
    assert button.callback_data == "marathon_practice:3"


# ─────────────────────────── сторож очереди ───────────────────────────

@pytest.mark.asyncio
async def test_watch_delivery_queue_fail_open(monkeypatch):
    # Недоступная БД не должна ронять сторожа и не должна слать алерт.
    async def _broken_pool():
        raise ConnectionError("db down")
    monkeypatch.setattr(sched, "get_pool", _broken_pool)

    sent = []
    monkeypatch.setattr(sched, "Bot", lambda token: pytest.fail("алерт при недоступной БД"))

    await sched._watch_delivery_queue()   # не бросает — fail-open
    assert sent == []


# ─────────────────────────── инвариант миграции ───────────────────────────

def _function_body(name: str) -> str:
    m = re.search(rf"async def {name}\(.*?(?=\nasync def |\ndef )", SCHEDULER_SRC, re.S)
    assert m, f"функция {name} не найдена в core/scheduler.py"
    return m.group(0)


@pytest.mark.parametrize("fn_name", MIGRATED_SENDERS)
def test_no_direct_send_in_migrated_senders(fn_name):
    body = _function_body(fn_name)
    direct = re.findall(r"bot\.send_\w+", body)
    assert not direct, (
        f"{fn_name} мигрирована на Доставщик, но содержит прямые отправки: {direct}. "
        "Отправка — только через core.notification_service.enqueue."
    )


@pytest.mark.parametrize("fn_name", MIGRATED_SENDERS)
def test_migrated_senders_use_enqueue(fn_name):
    body = _function_body(fn_name)
    assert "enqueue(" in body, f"{fn_name} не вызывает enqueue — миграция потеряна?"
