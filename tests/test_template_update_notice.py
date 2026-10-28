"""
WP-529: анонс выпуска шаблона. Текст собирает CI шаблона, бот проверяет границу
и ставит сообщение в Доставщик. Регрессии:
- 0.41.10: оборванный текст модели и ограждение ```html ушли подписчикам;
- 0.40.1: один релиз ушёл дважды (17.09 и 18.09) — ключ (chat_id, version).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers.template_notice import (
    TELEGRAM_MESSAGE_LIMIT,
    build_template_notice,
    is_safe_telegram_html,
    parse_release_version,
    short_notice,
)
from oauth_server import template_update_handler

GOOD_HTML = (
    "🔄 <b>Обновление шаблона IWE 0.41.10</b>\n\n"
    "1. При открытии дня появляется предупреждение, если рабочие данные не удалось обновить.\n\n"
    '<a href="https://github.com/TserenTserenov/FMT-exocortex-template/releases/tag/v0.41.10">Все изменения</a>'
)


def _request(body, secret="test-secret"):
    request = MagicMock()
    request.headers = {"X-Webhook-Secret": secret}
    request.json = AsyncMock(return_value=body)
    return request


async def _run(body, subscribers=(111, 222), enqueue_side_effect=None):
    with patch("db.queries.users.get_template_update_subscribers", new_callable=AsyncMock) as subs, \
         patch("core.notification_service.enqueue", new_callable=AsyncMock) as enqueue:
        subs.return_value = list(subscribers)
        enqueue.side_effect = enqueue_side_effect or (lambda *a, **k: {"notification_id": 1, "status": "queued"})
        response = await template_update_handler(_request(body))
    return response, enqueue


# ── boundary check ────────────────────────────────────────────────────────────

def test_good_html_passes():
    assert is_safe_telegram_html(GOOD_HTML)


@pytest.mark.parametrize("bad", [
    "```html\n<b>Обновление</b>\n```",
    "<b>Обновление</b>\n3. Встроенный <i>",
    "<b>жирный</i></b>",
    "<script>alert(1)</script>",
    '<a href="javascript:alert(1)">ссылка</a>',
    '<b class="x">текст</b>',
    "Сравнение: a < b",
    "Tom & Jerry",
    "x" * (TELEGRAM_MESSAGE_LIMIT + 1),
    "",
])
def test_unsafe_html_rejected(bad):
    assert not is_safe_telegram_html(bad)


def test_escaped_entities_allowed():
    assert is_safe_telegram_html("Tom &amp; Jerry &lt;3")


def test_build_falls_back_to_short_notice_for_truncated_text():
    text, kind = build_template_notice("0.41.10", "<b>Обновление</b>\n3. Встроенный <i>")
    assert kind == "short"
    assert text == short_notice("0.41.10")
    assert "releases/tag/v0.41.10" in text
    assert is_safe_telegram_html(text)


@pytest.mark.parametrize("raw,expected", [
    ("0.41.10", "0.41.10"),
    ("[0.41.10] — 2026-10-27", "0.41.10"),
    ("[0.42.0-rc.1] — 2026-11-01", "0.42.0-rc.1"),
    ("Unreleased", None),
])
def test_parse_release_version(raw, expected):
    assert parse_release_version(raw) == expected


# ── handler ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_built_text_is_enqueued_as_is_with_version_key(monkeypatch):
    monkeypatch.setenv("TEMPLATE_WEBHOOK_SECRET", "test-secret")
    response, enqueue = await _run({"version": "0.41.10", "message_html": GOOD_HTML})

    assert response.status == 200
    assert '"notice": "built"' in response.text
    assert '"queued": 2' in response.text
    first = enqueue.await_args_list[0]
    assert first.args[0] == 111
    assert first.args[2] == {"text": GOOD_HTML, "format": "html"}
    assert first.kwargs["journal_key"] == "template_update:111:0.41.10"
    assert first.kwargs["dedup_key"] == "template_update:111:0.41.10"
    assert first.kwargs["journal_type"] == "template_update"


@pytest.mark.asyncio
async def test_fenced_text_from_ci_becomes_short_notice(monkeypatch):
    monkeypatch.setenv("TEMPLATE_WEBHOOK_SECRET", "test-secret")
    response, enqueue = await _run({"version": "0.41.10", "message_html": "```html\n" + GOOD_HTML + "\n```"})

    assert '"notice": "short"' in response.text
    assert enqueue.await_args_list[0].args[2]["text"] == short_notice("0.41.10")


@pytest.mark.asyncio
async def test_legacy_changelog_payload_gets_short_notice(monkeypatch):
    """Старый notify-update.yml шлёт заголовок CHANGELOG и сырой текст — сырой текст не рассылается."""
    monkeypatch.setenv("TEMPLATE_WEBHOOK_SECRET", "test-secret")
    response, enqueue = await _run(
        {"version": "[0.41.10] — 2026-10-27", "changelog": "### Added\n- **Day Open** ...", "commit_count": 3},
        subscribers=(111,),
    )

    assert '"notice": "short"' in response.text
    call = enqueue.await_args_list[0]
    assert call.args[2]["text"] == short_notice("0.41.10")
    assert call.kwargs["journal_key"] == "template_update:111:0.41.10"


@pytest.mark.asyncio
async def test_already_notified_subscriber_is_skipped(monkeypatch):
    monkeypatch.setenv("TEMPLATE_WEBHOOK_SECRET", "test-secret")
    results = iter([
        {"notification_id": 1, "status": "queued"},
        {"notification_id": None, "status": "suppressed", "reason": "duplicate"},
    ])
    response, _ = await _run({"version": "0.41.10", "message_html": GOOD_HTML},
                             enqueue_side_effect=lambda *a, **k: next(results))

    assert '"queued": 1' in response.text
    assert '"skipped": 1' in response.text


@pytest.mark.asyncio
async def test_enqueue_failure_counts_and_continues(monkeypatch):
    monkeypatch.setenv("TEMPLATE_WEBHOOK_SECRET", "test-secret")
    results = iter([RuntimeError("db down"), {"notification_id": 2, "status": "queued"}])

    def side_effect(*args, **kwargs):
        item = next(results)
        if isinstance(item, Exception):
            raise item
        return item

    response, enqueue = await _run({"version": "0.41.10", "message_html": GOOD_HTML},
                                   enqueue_side_effect=side_effect)

    assert enqueue.await_count == 2
    assert '"failed": 1' in response.text
    assert '"queued": 1' in response.text


@pytest.mark.asyncio
async def test_missing_version_is_rejected(monkeypatch):
    monkeypatch.setenv("TEMPLATE_WEBHOOK_SECRET", "test-secret")
    response, enqueue = await _run({"message_html": GOOD_HTML})

    assert response.status == 400
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_secret_is_forbidden(monkeypatch):
    monkeypatch.setenv("TEMPLATE_WEBHOOK_SECRET", "test-secret")
    with patch("core.notification_service.enqueue", new_callable=AsyncMock) as enqueue:
        response = await template_update_handler(_request({"version": "0.41.10"}, secret="wrong"))

    assert response.status == 403
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_goes_only_to_developer_chat(monkeypatch):
    monkeypatch.setenv("TEMPLATE_WEBHOOK_SECRET", "test-secret")
    with patch("config.DEVELOPER_CHAT_ID", 999):
        response, enqueue = await _run({"version": "0.41.10", "message_html": GOOD_HTML, "preview": True})

    assert '"preview": true' in response.text
    assert enqueue.await_count == 1
    call = enqueue.await_args_list[0]
    assert call.args[0] == 999
    assert call.kwargs["journal_key"].startswith("template_update_preview:0.41.10:")


@pytest.mark.asyncio
async def test_changed_preview_text_gets_new_key(monkeypatch):
    monkeypatch.setenv("TEMPLATE_WEBHOOK_SECRET", "test-secret")
    with patch("config.DEVELOPER_CHAT_ID", 999):
        _, first = await _run({"version": "0.41.10", "message_html": GOOD_HTML, "preview": True})
        _, second = await _run({"version": "0.41.10", "message_html": GOOD_HTML + " ", "preview": True})

    assert first.await_args.kwargs["journal_key"] != second.await_args.kwargs["journal_key"]


@pytest.mark.asyncio
async def test_preview_without_developer_chat_is_refused(monkeypatch):
    monkeypatch.setenv("TEMPLATE_WEBHOOK_SECRET", "test-secret")
    with patch("config.DEVELOPER_CHAT_ID", 0):
        response, enqueue = await _run({"version": "0.41.10", "message_html": GOOD_HTML, "preview": True})

    assert response.status == 503
    enqueue.assert_not_awaited()
