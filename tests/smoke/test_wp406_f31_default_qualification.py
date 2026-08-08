"""
WP-406 Ф31: дефолтная квалификация «Ученик» при завершении онбординга.

Правило (решение пилота, peer-session 2026-08-08-01):
  - Уже есть квалификация (ЦД 2_collected.2_2_courses.qualification_level
    или живой LMS-уровень Работник+) → НЕ трогать и НЕ понижать.
  - Квалификации нет → назначить «Ученик» (живая шкала: 4).
  - Автоназначение НИКОГДА не ставит Работник(5)+.
  - Fail-open: ошибка записи квалификации не ломает онбординг.

Точки срабатывания — там же, где логируется onboarding_completed (Ф18):
  core/onboarder/x2.py:_finish_x2 (x3 закрыт раньше) и
  handlers/onboarding.py:on_x3_confirm (x2 закрыт раньше).
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import db.queries.dt_sync as dt_sync


# ─────────────────────────── фейковый пул ───────────────────────────

class FakeConn:
    """Подменяет asyncpg connection: возвращает подготовленные ответы, копит execute."""

    def __init__(self, user_row=None, existing_qual=None, engagement_uuid=None):
        self._user_row = user_row
        self._existing_qual = existing_qual
        self._engagement_uuid = engagement_uuid
        self.executed = []  # [(sql, args)]

    async def fetchrow(self, sql, *args):
        if "FROM public.users" in sql:
            return self._user_row
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def fetchval(self, sql, *args):
        if "qualification_level" in sql:
            return self._existing_qual
        if "development.engagement" in sql:
            return self._engagement_uuid
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "INSERT 0 1"


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Acq:
            async def __aenter__(self_):
                return conn

            async def __aexit__(self_, *a):
                return False
        return _Acq()


def _patch_pools(monkeypatch, conn, dt_user_id=None):
    async def _get_pool():
        return FakePool(conn)
    monkeypatch.setattr(dt_sync, "get_pool", _get_pool)

    # secrets pool (dt_tokens) — отдельный fetchval
    class _SecretsConn:
        async def fetchval(self, sql, *args):
            return dt_user_id

    async def _get_secrets_pool():
        return FakePool(_SecretsConn())
    import db.connection
    monkeypatch.setattr(db.connection, "get_secrets_pool", _get_secrets_pool)


# ─────────────────────── ensure_default_qualification ───────────────────────

@pytest.mark.asyncio
async def test_default_written_when_absent(monkeypatch):
    """Нет квалификации в ЦД и LMS → пишется «Ученик» deep-merge'ем в 2_2_courses."""
    conn = FakeConn(user_row={"ory_id": "ory-uuid-1", "aisystant_id": None})
    _patch_pools(monkeypatch, conn)

    result = await dt_sync.ensure_default_qualification(111)

    assert result is True
    assert len(conn.executed) == 1
    sql, args = conn.executed[0]
    assert args[0] == "ory-uuid-1"
    payload = json.loads(args[1])
    qual = payload["2_collected"]["2_2_courses"]["qualification_level"]
    assert qual["level"] == "Ученик"
    assert qual["numeric"] == 20  # LMS numeric для Ученика, < 25 (не LMS-managed)
    assert qual["code"] == "L2"
    # merge углублён до 2_2_courses — соседние ключи не затираются
    assert "2_2_courses" in sql and "2_collected" in sql


@pytest.mark.asyncio
async def test_existing_qualification_untouched(monkeypatch):
    """Уже есть qualification_level в ЦД → никакой записи (не трогаем, не понижаем)."""
    existing = json.dumps({"level": "Работник", "numeric": 25})
    conn = FakeConn(
        user_row={"ory_id": "ory-uuid-2", "aisystant_id": None},
        existing_qual=existing,
    )
    _patch_pools(monkeypatch, conn)

    result = await dt_sync.ensure_default_qualification(222)

    assert result is False
    assert conn.executed == []


@pytest.mark.asyncio
async def test_live_lms_worker_plus_skips_default(monkeypatch):
    """LMS даёт Работник+ (numeric >= 25), ещё не синкнут в ЦД → дефолт не пишем."""
    conn = FakeConn(user_row={"ory_id": "ory-uuid-3", "aisystant_id": "42"})
    _patch_pools(monkeypatch, conn)

    async def _lms(ids):
        return {"42": {"level": "Стратег", "code": "L3", "numeric": 30}}
    monkeypatch.setattr(dt_sync, "_preload_lms_qualifications", _lms)

    result = await dt_sync.ensure_default_qualification(333)

    assert result is False
    assert conn.executed == []


@pytest.mark.asyncio
async def test_lms_below_worker_still_assigns_default(monkeypatch):
    """LMS-уровень < Работника (managed by stage_transitions, в ЦД не попадает) →
    дефолт «Ученик» назначается."""
    conn = FakeConn(user_row={"ory_id": "ory-uuid-4", "aisystant_id": "43"})
    _patch_pools(monkeypatch, conn)

    async def _lms(ids):
        return {"43": {"level": "Интересант", "code": "L05", "numeric": 5}}
    monkeypatch.setattr(dt_sync, "_preload_lms_qualifications", _lms)

    result = await dt_sync.ensure_default_qualification(444)

    assert result is True
    assert len(conn.executed) == 1


@pytest.mark.asyncio
async def test_no_dt_identity_skips(monkeypatch):
    """Нет ни dt_user_id, ни ory_id, ни engagement uuid → запись невозможна, тихий skip."""
    conn = FakeConn(user_row={"ory_id": None, "aisystant_id": None})
    _patch_pools(monkeypatch, conn)

    result = await dt_sync.ensure_default_qualification(555)

    assert result is False
    assert conn.executed == []


@pytest.mark.asyncio
async def test_never_auto_assigns_worker_or_above(monkeypatch):
    """Guard живой шкалы: дефолт с уровнем >= Работник(5) не пишется никогда."""
    conn = FakeConn(user_row={"ory_id": "ory-uuid-6", "aisystant_id": None})
    _patch_pools(monkeypatch, conn)
    monkeypatch.setattr(dt_sync, "_DEFAULT_ONBOARDING_QUALIFICATION", "Работник")

    result = await dt_sync.ensure_default_qualification(666)

    assert result is False
    assert conn.executed == []


@pytest.mark.asyncio
async def test_fail_open_on_db_error(monkeypatch):
    """Ошибка БД внутри — функция глотает и возвращает False (онбординг не ломается)."""
    async def _broken_pool():
        raise RuntimeError("db down")
    monkeypatch.setattr(dt_sync, "get_pool", _broken_pool)

    result = await dt_sync.ensure_default_qualification(777)

    assert result is False


# ─────────────────── интеграция с точками onboarding_completed ───────────────────

def _mock_callback(data: str, chat_id: int = 317106357):
    callback = AsyncMock()
    callback.data = data
    callback.from_user = MagicMock(id=chat_id)
    callback.message = AsyncMock()
    return callback


@pytest.mark.asyncio
async def test_x3_completion_assigns_default_qualification():
    """Х2 закрыт → подтверждение Х3 (onboarding_completed) вызывает назначение квалификации."""
    callback = _mock_callback("x3_confirm:marathon:0")

    with patch("core.onboarder.storage.get_status", new_callable=AsyncMock,
               return_value={"x2_done": True, "x3_done": False}), \
         patch("core.onboarder.storage.mark_x3_done", new_callable=AsyncMock), \
         patch("core.onboarder.storage.save_onboarding_context", new_callable=AsyncMock), \
         patch("core.onboarder.storage.get_onboarding_context", new_callable=AsyncMock,
               return_value={"entry_type": "direct"}), \
         patch("handlers.onboarding.get_intern", new_callable=AsyncMock,
               return_value={"language": "ru"}), \
         patch("db.queries.events.log_event", new_callable=AsyncMock), \
         patch("db.queries.dt_sync.ensure_default_qualification",
               new_callable=AsyncMock) as mock_qual:
        from handlers.onboarding import on_x3_confirm
        await on_x3_confirm(callback)

    mock_qual.assert_awaited_once_with(317106357)


@pytest.mark.asyncio
async def test_x3_first_no_qualification_call():
    """Х2 ещё открыт → onboarding_completed нет → квалификация не назначается."""
    callback = _mock_callback("x3_confirm:marathon:0")

    with patch("core.onboarder.storage.get_status", new_callable=AsyncMock,
               return_value={"x2_done": False, "x3_done": False}), \
         patch("core.onboarder.storage.mark_x3_done", new_callable=AsyncMock), \
         patch("core.onboarder.storage.save_onboarding_context", new_callable=AsyncMock), \
         patch("core.onboarder.storage.get_onboarding_context", new_callable=AsyncMock,
               return_value={"entry_type": "direct"}), \
         patch("handlers.onboarding.get_intern", new_callable=AsyncMock,
               return_value={"language": "ru"}), \
         patch("db.queries.events.log_event", new_callable=AsyncMock), \
         patch("db.queries.dt_sync.ensure_default_qualification",
               new_callable=AsyncMock) as mock_qual:
        from handlers.onboarding import on_x3_confirm
        await on_x3_confirm(callback)

    mock_qual.assert_not_awaited()


@pytest.mark.asyncio
async def test_x2_completion_assigns_default_qualification():
    """Х3 закрыт → финиш Х2 (onboarding_completed) вызывает назначение квалификации."""
    bot = AsyncMock()
    chat_id = 317106357

    with patch("core.onboarder.storage.get_status", new_callable=AsyncMock,
               return_value={"x2_done": False, "x3_done": True}), \
         patch("core.onboarder.storage.mark_x2_done", new_callable=AsyncMock), \
         patch("core.onboarder.storage.get_onboarding_context", new_callable=AsyncMock,
               return_value={"entry_type": "direct"}), \
         patch("db.queries.users.get_intern", new_callable=AsyncMock,
               return_value={"language": "ru"}), \
         patch("db.queries.events.log_event", new_callable=AsyncMock), \
         patch("db.queries.dt_sync.ensure_default_qualification",
               new_callable=AsyncMock) as mock_qual:
        from core.onboarder.x2 import _finish_x2
        await _finish_x2(bot, chat_id)

    mock_qual.assert_awaited_once_with(chat_id)


@pytest.mark.asyncio
async def test_qualification_error_does_not_break_x3_flow():
    """Fail-open: ensure_default_qualification упал → поток Х3 доходит до ответа пользователю."""
    callback = _mock_callback("x3_confirm:marathon:0")

    with patch("core.onboarder.storage.get_status", new_callable=AsyncMock,
               return_value={"x2_done": True, "x3_done": False}), \
         patch("core.onboarder.storage.mark_x3_done", new_callable=AsyncMock), \
         patch("core.onboarder.storage.save_onboarding_context", new_callable=AsyncMock), \
         patch("core.onboarder.storage.get_onboarding_context", new_callable=AsyncMock,
               return_value={"entry_type": "direct"}), \
         patch("handlers.onboarding.get_intern", new_callable=AsyncMock,
               return_value={"language": "ru"}), \
         patch("db.queries.events.log_event", new_callable=AsyncMock) as mock_log, \
         patch("db.queries.dt_sync.ensure_default_qualification",
               new_callable=AsyncMock, side_effect=RuntimeError("dt down")):
        from handlers.onboarding import on_x3_confirm
        await on_x3_confirm(callback)

    logged_events = [call.args[1] for call in mock_log.await_args_list]
    assert "onboarding_completed" in logged_events
    # Пользователь получил подтверждение выбора курса, а не сообщение об ошибке
    answered = [call.args[0] for call in callback.message.answer.await_args_list]
    assert any("Курс выбран" in text for text in answered)


@pytest.mark.asyncio
async def test_qualification_error_does_not_break_x2_flow():
    """Fail-open: ensure_default_qualification упал → финиш Х2 отправляет поздравление."""
    bot = AsyncMock()
    chat_id = 317106357

    with patch("core.onboarder.storage.get_status", new_callable=AsyncMock,
               return_value={"x2_done": False, "x3_done": True}), \
         patch("core.onboarder.storage.mark_x2_done", new_callable=AsyncMock), \
         patch("core.onboarder.storage.get_onboarding_context", new_callable=AsyncMock,
               return_value={"entry_type": "direct"}), \
         patch("db.queries.users.get_intern", new_callable=AsyncMock,
               return_value={"language": "ru"}), \
         patch("db.queries.events.log_event", new_callable=AsyncMock), \
         patch("db.queries.dt_sync.ensure_default_qualification",
               new_callable=AsyncMock, side_effect=RuntimeError("dt down")):
        from core.onboarder.x2 import _finish_x2
        await _finish_x2(bot, chat_id)

    bot.send_message.assert_awaited()  # поздравление ушло несмотря на ошибку квалификации
