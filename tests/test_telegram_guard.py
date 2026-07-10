"""Тесты safe_edit_message (BDR4, WP-7 Block BDR).

asyncio.run вместо pytest-asyncio — модуль самодостаточен, без зависимости от плагина.
"""
import asyncio

import pytest
from aiogram.exceptions import TelegramBadRequest

from core.telegram_guard import safe_edit_message


class _FakeBadRequest(TelegramBadRequest):
    """TelegramBadRequest с заданным текстом, без тяжёлого конструктора aiogram."""

    def __init__(self, message: str):
        self._msg = message

    def __str__(self) -> str:
        return self._msg


class _FakeEdit:
    """Имитация bound edit-метода: считает вызовы, опционально бросает исключение."""

    def __init__(self, exc: Exception | None = None):
        self.exc = exc
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.exc:
            raise self.exc


def test_success_returns_true():
    edit = _FakeEdit()
    result = asyncio.run(safe_edit_message(edit, "новый текст", reply_markup=None))
    assert result is True
    assert edit.calls == 1


def test_not_modified_swallowed_returns_false():
    edit = _FakeEdit(_FakeBadRequest("Bad Request: message is not modified"))
    result = asyncio.run(safe_edit_message(edit, "тот же текст"))
    assert result is False          # no-op, не упало
    assert edit.calls == 1


def test_other_bad_request_reraised():
    edit = _FakeEdit(_FakeBadRequest("Bad Request: message to edit not found"))
    with pytest.raises(TelegramBadRequest):
        asyncio.run(safe_edit_message(edit, "текст"))


def test_non_telegram_exception_propagates():
    edit = _FakeEdit(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        asyncio.run(safe_edit_message(edit, "текст"))


def test_not_modified_logs_debug(caplog):
    import logging
    edit = _FakeEdit(_FakeBadRequest("Bad Request: message is not modified"))
    with caplog.at_level(logging.DEBUG, logger="core.telegram_guard"):
        asyncio.run(safe_edit_message(edit, "тот же текст"))
    assert "double-tap" in caplog.text
