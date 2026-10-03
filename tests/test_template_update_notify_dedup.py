"""
Регрессия: notify-update.yml пересылает один и тот же релиз повторно, если
окно "сегодня/вчера" ещё не сдвинулось (версия 0.40.1 ушла подписчикам и
17.09, и 18.09 — issue из peer-сессии 2026-09-18). template_update_handler
раньше слал сообщение всем подписчикам безусловно, без проверки "версия уже
разослана". Фикс — идемпотентность по (chat_id, version) через общий
log-before-send механизм (db.queries.notifications.send_idempotent).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oauth_server import template_update_handler


def _make_request(version="0.40.1", changelog="- пункт первый\n- пункт второй", commit_count=3):
    request = MagicMock()
    request.headers = {"X-Webhook-Secret": "test-secret"}
    request.json = AsyncMock(return_value={
        "version": version,
        "changelog": changelog,
        "commit_count": commit_count,
    })
    return request


@pytest.mark.asyncio
async def test_second_run_skips_subscriber_already_notified_for_version(monkeypatch):
    """Первый подписчик ещё не получал эту версию — доставляем.
    Второй уже получал (дубль повторного ранна экшена) — пропускаем, не шлём."""
    monkeypatch.setenv("TEMPLATE_WEBHOOK_SECRET", "test-secret")
    request = _make_request()

    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock()

    with patch("oauth_server._bot_instance", fake_bot), \
         patch("clients.claude.claude.generate", new_callable=AsyncMock) as mock_generate, \
         patch("db.queries.users.get_template_update_subscribers", new_callable=AsyncMock) as mock_subs, \
         patch("db.queries.notifications.try_insert_notification", new_callable=AsyncMock) as mock_try_insert:
        mock_generate.side_effect = Exception("LLM недоступна в тесте — используем regex fallback")
        mock_subs.return_value = [111, 222]
        # 111 — новая запись (True), 222 — уже разослана ранее (False)
        mock_try_insert.side_effect = [True, False]

        response = await template_update_handler(request)

    assert response.status == 200
    body = response.text
    assert '"sent": 1' in body
    assert '"skipped": 1' in body
    assert '"failed": 0' in body

    fake_bot.send_message.assert_awaited_once()
    assert fake_bot.send_message.await_args.kwargs["chat_id"] == 111

    assert mock_try_insert.await_count == 2
    keys = [call.args[2] for call in mock_try_insert.await_args_list]
    assert keys == ["template_update:111:0.40.1", "template_update:222:0.40.1"]


@pytest.mark.asyncio
async def test_rerun_with_same_version_notifies_nobody(monkeypatch):
    """Полный повтор ранна (все подписчики уже получали эту версию) — 0 отправок."""
    monkeypatch.setenv("TEMPLATE_WEBHOOK_SECRET", "test-secret")
    request = _make_request()

    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock()

    with patch("oauth_server._bot_instance", fake_bot), \
         patch("clients.claude.claude.generate", new_callable=AsyncMock) as mock_generate, \
         patch("db.queries.users.get_template_update_subscribers", new_callable=AsyncMock) as mock_subs, \
         patch("db.queries.notifications.try_insert_notification", new_callable=AsyncMock) as mock_try_insert:
        mock_generate.side_effect = Exception("LLM недоступна в тесте — используем regex fallback")
        mock_subs.return_value = [111, 222]
        mock_try_insert.return_value = False

        response = await template_update_handler(request)

    assert response.status == 200
    body = response.text
    assert '"sent": 0' in body
    assert '"skipped": 2' in body
    fake_bot.send_message.assert_not_awaited()
