"""
Smoke test для circuit breaker (_safe_route_heavy).

WP-7 post-peer-session verification (P0 fix).
Проверяет: asyncio.shield continuation, soft/hard timeout пороги,
отсутствие RuntimeError при coroutine reuse.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from handlers.commands import _safe_route_heavy, _HEAVY_SOFT_TIMEOUT


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
        await asyncio.sleep(_HEAVY_SOFT_TIMEOUT + 1.0)
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


@pytest.fixture
def short_timeouts(monkeypatch):
    """Shortened soft / hard / final thresholds for fast tests."""
    monkeypatch.setattr('handlers.commands._HEAVY_SOFT_TIMEOUT', 0.2)
    monkeypatch.setattr('handlers.commands._HEAVY_HARD_TIMEOUT', 0.6)
    monkeypatch.setattr('handlers.commands._HEAVY_FINAL_TIMEOUT', 1.5)


@pytest.fixture
def no_event_log():
    with patch('db.queries.events.log_event', new=AsyncMock()) as log_event:
        yield log_event


@pytest.mark.asyncio
async def test_slow_work_is_not_dropped(mock_message, mock_state, mock_intern, short_timeouts):
    """
    WP-5 F77: work passing the hard timeout is told to the reader and allowed to finish;
    before F77 it was cancelled at the hard timeout and the reader got nothing.
    """
    finished = {'flag': False}

    async def slow_coro():
        await asyncio.sleep(1.0)  # between hard (0.6) and final (1.5)
        finished['flag'] = True

    await _safe_route_heavy(mock_message, mock_state, mock_intern, slow_coro(), 'feed')

    texts = [c['text'] for c in mock_message.answer_calls]
    assert texts == ['feed.updating', 'feed.slow_generation']
    assert finished['flag'], "work past the hard timeout must not be cancelled"


@pytest.mark.asyncio
async def test_final_cap_reports_error(mock_message, mock_state, mock_intern, short_timeouts, no_event_log):
    """
    WP-5 F77: past the final cap the reader gets an explicit error, not silence.
    """
    async def very_slow_coro():
        await asyncio.sleep(10.0)

    with patch('handlers.commands.logger') as mock_logger:
        await _safe_route_heavy(mock_message, mock_state, mock_intern, very_slow_coro(), 'feed')

    texts = [c['text'] for c in mock_message.answer_calls]
    assert texts == ['feed.updating', 'feed.slow_generation', 'errors.generation_timeout']
    error_calls = [str(call) for call in mock_logger.error.call_args_list]
    assert any('HARD TIMEOUT' in c for c in error_calls), error_calls
    assert no_event_log.await_args.args[1] == 'error_shown'


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
        await asyncio.sleep(_HEAVY_SOFT_TIMEOUT + 1.0)
        counter['n'] += 1
        return "done"

    await _safe_route_heavy(mock_message, mock_state, mock_intern, side_effect_coro(), 'feed')

    # Даём event loop обработать завершение background task
    await asyncio.sleep(0.2)

    assert counter['n'] == 1, (
        "Background task не завершился — shield не сработал"
    )


@pytest.mark.asyncio
async def test_final_cap_cancels_task(mock_message, mock_state, mock_intern, short_timeouts, no_event_log):
    """
    P0 (kept by WP-5 F77): past the final cap the background task is cancelled.
    """
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
        "Background task не был отменён при final timeout"
    )


@pytest.mark.asyncio
async def test_repeated_command_while_running_is_skipped(mock_state, mock_intern, short_timeouts):
    """
    WP-5 F77: a second /feed while the first is still building must not start a second build.
    """
    first, second = AsyncMock(), AsyncMock()
    first.chat.id = second.chat.id = 4242
    starts = {'n': 0}

    async def slow_coro():
        starts['n'] += 1
        await asyncio.sleep(0.4)

    running = asyncio.create_task(_safe_route_heavy(first, mock_state, mock_intern, slow_coro(), 'feed'))
    await asyncio.sleep(0.05)
    await _safe_route_heavy(second, mock_state, mock_intern, slow_coro(), 'feed')
    await running

    assert starts['n'] == 1
    second.answer.assert_awaited_once_with('feed.slow_generation')


@pytest.mark.asyncio
async def test_error_after_soft_timeout_reaches_reader(mock_message, mock_state, mock_intern, short_timeouts):
    """
    WP-5 F77: an exception raised after the soft notice is reported, not lost.
    """
    async def failing_coro():
        await asyncio.sleep(0.4)  # past soft (0.2), before hard (0.6)
        raise RuntimeError("boom")

    await _safe_route_heavy(mock_message, mock_state, mock_intern, failing_coro(), 'feed')

    texts = [c['text'] for c in mock_message.answer_calls]
    assert texts == ['feed.updating', 'errors.processing_error']


@pytest.mark.asyncio
async def test_chat_is_released_after_run(mock_message, mock_state, mock_intern):
    async def fast_coro():
        return None

    await _safe_route_heavy(mock_message, mock_state, mock_intern, fast_coro(), 'feed')

    from handlers.commands import _heavy_in_flight
    assert mock_message.chat.id not in _heavy_in_flight
