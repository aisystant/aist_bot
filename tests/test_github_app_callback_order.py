"""WP-406: порядок проверок в github_app_callback_handler.

Регрессионный тест на находку code review (пир-сессия 2026-09-10-21-wp406-
github-app-slug-fix): ownership-check (сетевой, fail-closed) обязан идти
ПОСЛЕ graceful-fallback ветки «stale state, но installation уже сохранена» —
иначе транзиентная сетевая ошибка превращает дружественный «уже установлено»
в 503 для пользователя с давно и корректно подключённым приложением.
"""

from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

import oauth_server


def _request(installation_id: int = 555, state: str = "", setup_action: str = "install"):
    query = f"installation_id={installation_id}&setup_action={setup_action}"
    if state:
        query += f"&state={state}"
    return make_mocked_request("GET", f"/auth/github_app/callback?{query}")


@pytest.fixture(autouse=True)
def _no_state_secret(monkeypatch):
    # _make_app_setup_state/_verify_app_setup_state без секрета работают в
    # согласованном dev-режиме ("<chat_id>.<ts>.dev") — не нужен реальный HMAC.
    monkeypatch.delenv("GITHUB_APP_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("INTERNAL_NOTIFY_SECRET", raising=False)


async def test_stale_state_with_existing_installation_skips_network_ownership_check():
    """Graceful fallback (installation уже в БД) не должен звать GitHub API."""
    verify_mock = AsyncMock(return_value=False)  # если бы вызвался — провалил бы запрос
    with patch("clients.github_app.verify_installation_belongs_to_app", verify_mock), \
         patch("db.queries.github_app.find_user_by_installation_id",
               AsyncMock(return_value={"chat_id": 42})):
        resp = await oauth_server.github_app_callback_handler(_request(state="garbage-invalid-state"))

    assert resp.status == 200
    assert "уже установлен" in resp.text.lower()
    verify_mock.assert_not_called()


async def test_expired_state_without_existing_installation_returns_session_expired():
    with patch("clients.github_app.verify_installation_belongs_to_app",
               AsyncMock(return_value=False)) as verify_mock, \
         patch("db.queries.github_app.find_user_by_installation_id",
               AsyncMock(return_value=None)):
        resp = await oauth_server.github_app_callback_handler(_request(state="garbage-invalid-state"))

    assert resp.status == 400
    assert "истекла" in resp.text.lower()
    verify_mock.assert_not_called()


async def test_fresh_install_ownership_check_failure_returns_503_without_saving():
    state = oauth_server._make_app_setup_state(42)
    save_mock = AsyncMock()
    with patch("clients.github_app.verify_installation_belongs_to_app",
               AsyncMock(return_value=False)) as verify_mock, \
         patch("db.queries.github_app.save_app_installation", save_mock):
        resp = await oauth_server.github_app_callback_handler(_request(state=state))

    assert resp.status == 503
    assert "не подтверждена" in resp.text.lower()
    verify_mock.assert_awaited_once_with(555)
    save_mock.assert_not_called()


async def test_fresh_install_ownership_check_success_proceeds_to_save():
    state = oauth_server._make_app_setup_state(42)
    with patch("clients.github_app.verify_installation_belongs_to_app",
               AsyncMock(return_value=True)), \
         patch("clients.github_app.get_installation_repos",
               AsyncMock(return_value=[{"full_name": "tseren/DS-personal-guide", "private": True}])), \
         patch("db.queries.github_app.save_app_installation",
               AsyncMock(return_value=(True, "ok"))) as save_mock:
        resp = await oauth_server.github_app_callback_handler(_request(state=state))

    assert resp.status == 200
    assert "установлен" in resp.text.lower()
    save_mock.assert_awaited_once_with(42, 555, "tseren/DS-personal-guide", "tseren")
