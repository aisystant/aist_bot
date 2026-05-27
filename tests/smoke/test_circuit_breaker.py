"""
Smoke test для circuit breaker (_safe_route_heavy).

WP-7 post-peer-session verification (P0 fix).
Проверяет: asyncio.shield continuation, soft/hard timeout пороги,
отсутствие RuntimeError при coroutine reuse.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from handlers.commands import _safe_route_heavy, _HEAVY_SOFT_TIMEOUT, _HEAVY_HARD_TIMEOUT


@pytest.fixture
def mock_message():
    m = AsyncMock()
    m.answer_calls = []

    async def _answer(text, **kwargs):
        m.answer_calls.append({
            'text': text,
            'time': asyncio.get_event_loop().time(),
        })

    m.answer.side_effect = _answer
    return m


@pytest.fixture
def mock_state():
    return AsyncMock()


@pytest.fixture
def mock_intern():
    return {'language': 'ru'}


@pytest.fixture(autouse=True)
def mock_t():
    """Мокаем i18n.t для handlers.commands"""
    with patch('handlers.commands.t', side_effect=lambda key, lang='ru', **kw: key):
        yield


@pytest.mark.asyncio
async def test_soft_timeout_answers_immediately(mock_message, mock_state, mock_intern):
    """
    P0: route_coro > soft_timeout (3с) → message.answer('feed.updating') вызывается
    в пределах soft timeout. Функция возвращает управление только после завершения
    background task (continuation через shield).
    """
    async def slow_coro():
        await asyncio.sleep(4.0)
        return "done"

    start = asyncio.get_event_loop().time()
    await _safe_route_heavy(mock_message, mock_state, mock_intern, slow_coro(), 'feed')
    elapsed = asyncio.get_event_loop().time() - start

    # Проверяем, что промежуточное сообщение было отправлено в пределах soft timeout
    updating_calls = [c for c in mock_message.answer_calls if c['text'] == 'feed.updating']
    assert len(updating_calls) == 1, (
        f"Ожидался ровно один вызов feed.updating, получили: {mock_message.answer_calls}"
    )
    updating_elapsed = updating_calls[0]['time'] - start
    assert updating_elapsed < _HEAVY_SOFT_TIMEOUT + 0.5, (
        f"Промежуточное сообщение отправлено через {updating_elapsed:.2f}с, "
        f"ожидалось < {_HEAVY_SOFT_TIMEOUT + 0.5}с"
    )

    # Функция ждёт завершения background task (4с)
    assert elapsed >= 3.5, (
        f"Функция вернула управление слишком рано ({elapsed:.2f}с), "
        "shield continuation не сработал"
    )


@pytest.mark.asyncio
async def test_hard_timeout_aborts_and_logs(mock_message, mock_state, mock_intern, monkeypatch):
    """
    P0: route_coro > hard_timeout → abort + error log.
    Используем укороченные таймауты для скорости теста.
    """
    monkeypatch.setattr('handlers.commands._HEAVY_SOFT_TIMEOUT', 0.5)
    monkeypatch.setattr('handlers.commands._HEAVY_HARD_TIMEOUT', 1.5)

    async def very_slow_coro():
        await asyncio.sleep(10.0)
        return "done"

    with patch('handlers.commands.logger') as mock_logger:
        await _safe_route_heavy(mock_message, mock_state, mock_intern, very_slow_coro(), 'feed')

        assert mock_logger.error.called
        error_calls = [str(call) for call in mock_logger.error.call_args_list]
        assert any('HARD TIMEOUT' in c for c in error_calls), (
            f"Ожидался лог 'HARD TIMEOUT', получили: {error_calls}"
        )


@pytest.mark.asyncio
async def test_no_timeout_completes_silently(mock_message, mock_state, mock_intern):
    """
    P0: route_coro < soft_timeout → нормальное завершение, без message.answer.
    """
    async def fast_coro():
        await asyncio.sleep(0.1)
        return "done"

    await _safe_route_heavy(mock_message, mock_state, mock_intern, fast_coro(), 'feed')
    assert len(mock_message.answer_calls) == 0, (
        f"Не ожидалось message.answer, получили: {mock_message.answer_calls}"
    )


@pytest.mark.asyncio
async def test_shield_allows_continuation(mock_message, mock_state, mock_intern):
    """
    P0: asyncio.shield(task) позволяет корутине продолжить после soft timeout.
    Проверяем через side-effect (counter инкремент).
    """
    counter = {'n': 0}

    async def side_effect_coro():
        await asyncio.sleep(4.0)
        counter['n'] += 1
        return "done"

    await _safe_route_heavy(mock_message, mock_state, mock_intern, side_effect_coro(), 'feed')

    # Даём event loop обработать завершение background task
    await asyncio.sleep(0.2)

    assert counter['n'] == 1, (
        "Background task не завершился — shield не сработал"
    )


@pytest.mark.asyncio
async def test_hard_timeout_cancels_background_task(mock_message, mock_state, mock_intern, monkeypatch):
    """
    P0: При hard timeout background task должен быть отменён.
    Используем укороченные таймауты для скорости теста.
    """
    monkeypatch.setattr('handlers.commands._HEAVY_SOFT_TIMEOUT', 0.5)
    monkeypatch.setattr('handlers.commands._HEAVY_HARD_TIMEOUT', 1.5)

    cancelled = {'flag': False}

    async def cancellable_coro():
        try:
            await asyncio.sleep(10.0)
        except asyncio.CancelledError:
            cancelled['flag'] = True
            raise

    await _safe_route_heavy(mock_message, mock_state, mock_intern, cancellable_coro(), 'feed')

    # Даём event loop обработать отмену
    await asyncio.sleep(0.2)

    assert cancelled['flag'], (
        "Background task не был отменён при hard timeout"
    )
