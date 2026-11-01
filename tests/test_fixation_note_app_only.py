"""Фиксации пишутся пользователю только с GitHub App (без OAuth-токена).

Дефект (РП-262, ревью 03.10.2026): write_fixation_note проверял OAuth-токен
до записи и молча пропускал пользователей, у которых есть только App-установка,
хотя append_note умеет писать через установку.
"""

from unittest.mock import AsyncMock, patch

import pytest

from clients.github_auth import GitHubAuthUnavailable
from core.evaluator import write_fixation_note


async def _write():
    await write_fixation_note(
        telegram_user_id=123,
        topic_title="Тема",
        bloom_level=2,
        fixation_text="вывод",
    )


@pytest.mark.asyncio
async def test_app_only_user_gets_fixation_written():
    """OAuth-токена нет, App-установка есть: заметка записывается."""
    with patch("clients.github_oauth.github_oauth") as oauth, \
         patch("clients.github_api.github_notes") as notes:
        oauth.get_access_token = AsyncMock(return_value=None)
        notes.append_note = AsyncMock(return_value={"repo": "u/r", "path": "p", "sha": "s"})

        await _write()

        notes.append_note.assert_awaited_once()
        kwargs = notes.append_note.await_args.kwargs
        assert kwargs["telegram_user_id"] == 123
        assert "Тема" in kwargs["text"]
        assert "вывод" in kwargs["text"]


@pytest.mark.asyncio
async def test_no_connection_is_skipped_without_error():
    """Нет ни App, ни OAuth: запись пропущена, исключение наружу не уходит."""
    with patch("clients.github_api.github_notes") as notes:
        notes.append_note = AsyncMock(side_effect=GitHubAuthUnavailable("no_connection"))

        await _write()

        notes.append_note.assert_awaited_once()


@pytest.mark.asyncio
async def test_unexpected_error_is_swallowed_fire_and_forget():
    """Прочие сбои записи не ломают ответ пользователю (fire-and-forget)."""
    with patch("clients.github_api.github_notes") as notes:
        notes.append_note = AsyncMock(side_effect=RuntimeError("boom"))

        await _write()
