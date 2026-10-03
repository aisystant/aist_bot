"""
Регрессия: повторный callback установки GitHub App не должен падать
`UnboundLocalError`, когда репозиторий уже привязан и остаётся в selection
(WP-406 Ф22, критическая находка код-ревью peer-сессии 2026-09-10-08).

`selected_repo` раньше присваивался только в ветке "первая привязка / repos[0]" —
ветка "сохранить текущий app_repo_full_name" использовала `selected_repo.get(...)`
ниже по функции без присвоения, что валилось с `UnboundLocalError` именно в
сценарии, ради которого фикс и делался (повторный callback для уже привязанной
установки, например GitHub Configure добавил репозиторий для заметок к
installation, обслуживающей ещё и Персональное руководство).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oauth_server import github_app_callback_handler


def _make_request(installation_id: str = "777", state: str = "valid-state"):
    request = MagicMock()
    request.query = {
        "installation_id": installation_id,
        "setup_action": "update",
        "state": state,
    }
    return request


@pytest.mark.asyncio
async def test_repeat_callback_preserves_existing_repo_without_crashing():
    """Установка уже привязана к repoA; repoA всё ещё в selection → repo_full_name
    остаётся repoA, `selected_repo` разрешается (не UnboundLocalError)."""
    request = _make_request()

    repos = [
        {"full_name": "owner/repoA", "private": False},
        {"full_name": "owner/repoB", "private": True},
    ]

    with patch("oauth_server._verify_app_setup_state", return_value=999), \
         patch("clients.github_app.get_installation_repos", new_callable=AsyncMock) as mock_repos, \
         patch("clients.github_app.set_repo_private", new_callable=AsyncMock) as mock_set_private, \
         patch("db.queries.github_app.find_user_by_installation_id", new_callable=AsyncMock) as mock_find, \
         patch("db.queries.github_app.save_app_installation", new_callable=AsyncMock) as mock_save:
        mock_repos.return_value = repos
        mock_find.return_value = {"app_repo_full_name": "owner/repoA"}
        mock_save.return_value = (True, "updated")

        response = await github_app_callback_handler(request)

    assert response.status == 200
    mock_save.assert_awaited_once()
    saved_repo_full_name = mock_save.await_args.args[2]
    assert saved_repo_full_name == "owner/repoA"
    # repoA уже private=False в фикстуре выше, но мы проверяем что private-check
    # не упал на UnboundLocalError, а реально вызвался с правильным репо.
    mock_set_private.assert_awaited_once()
    assert mock_set_private.await_args.args[1] == "owner/repoA"


@pytest.mark.asyncio
async def test_first_time_callback_uses_first_repo():
    """Нет предыдущей привязки → используем repos[0], как раньше (поведение не меняется)."""
    request = _make_request()

    repos = [{"full_name": "owner/only-repo", "private": True}]

    with patch("oauth_server._verify_app_setup_state", return_value=999), \
         patch("clients.github_app.get_installation_repos", new_callable=AsyncMock) as mock_repos, \
         patch("db.queries.github_app.find_user_by_installation_id", new_callable=AsyncMock) as mock_find, \
         patch("db.queries.github_app.save_app_installation", new_callable=AsyncMock) as mock_save:
        mock_repos.return_value = repos
        mock_find.return_value = None  # первая привязка — записи ещё нет
        mock_save.return_value = (True, "inserted")

        response = await github_app_callback_handler(request)

    assert response.status == 200
    saved_repo_full_name = mock_save.await_args.args[2]
    assert saved_repo_full_name == "owner/only-repo"


@pytest.mark.asyncio
async def test_previous_repo_dropped_from_selection_falls_back_to_first():
    """Прежний репо больше не в installation (пользователь убрал его на GitHub) →
    fallback на repos[0], не падаем на отсутствующем repoOld."""
    request = _make_request()

    repos = [{"full_name": "owner/repoNew", "private": False}]

    with patch("oauth_server._verify_app_setup_state", return_value=999), \
         patch("clients.github_app.get_installation_repos", new_callable=AsyncMock) as mock_repos, \
         patch("clients.github_app.set_repo_private", new_callable=AsyncMock), \
         patch("db.queries.github_app.find_user_by_installation_id", new_callable=AsyncMock) as mock_find, \
         patch("db.queries.github_app.save_app_installation", new_callable=AsyncMock) as mock_save:
        mock_repos.return_value = repos
        mock_find.return_value = {"app_repo_full_name": "owner/repoOld"}
        mock_save.return_value = (True, "updated")

        response = await github_app_callback_handler(request)

    assert response.status == 200
    saved_repo_full_name = mock_save.await_args.args[2]
    assert saved_repo_full_name == "owner/repoNew"
