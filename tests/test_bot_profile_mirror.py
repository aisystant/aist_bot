"""WP-253 Ф12.6 фаза A — тесты зеркалирования профиля в persona.bot_profile.

Без живой БД: статические проверки конфигурации + mocked-пулы для поведения.
"""

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.queries import bot_profile


# === Статика: список полей ===

def test_mirror_fields_are_exactly_18_and_subset_of_ddl():
    ddl_columns = {
        "chat_id", "account_id", "dt_user_id", "email", "name", "occupation", "role",
        "domain", "interests", "motivation", "goals", "language", "timezone",
        "experience_level", "difficulty_preference", "learning_style", "study_duration",
        "current_problems", "desires", "tg_username", "aisystant_id",
        "aisystant_linked_at", "dt_connected_at", "tier", "delivery_format",
        "detail_level", "created_at", "updated_at",
    }
    assert len(bot_profile.PROFILE_MIRROR_FIELDS) == 18
    assert set(bot_profile.PROFILE_MIRROR_FIELDS) <= ddl_columns


def test_mirror_fields_exclude_fields_owned_elsewhere():
    excluded = {"tier", "email", "aisystant_id", "aisystant_linked_at", "dt_connected_at", "dt_user_id"}
    assert set(bot_profile.PROFILE_MIRROR_FIELDS).isdisjoint(excluded)


def test_mirror_sql_has_monotonic_guard_and_fk_coalesce():
    assert "ON CONFLICT (chat_id) DO UPDATE SET" in bot_profile.MIRROR_SQL
    assert "WHERE bot_profile.updated_at IS NULL OR EXCLUDED.updated_at >= bot_profile.updated_at" in bot_profile.MIRROR_SQL
    assert "account_id = COALESCE(EXCLUDED.account_id, bot_profile.account_id)" in bot_profile.MIRROR_SQL


# === mirror_profile_row: поведение ===

class _Conn:
    def __init__(self, on_execute=None):
        self._on_execute = on_execute

    async def execute(self, query, *params):
        if self._on_execute:
            return await self._on_execute(query, *params)
        return "INSERT 0 1"


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_exc_info):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


def _profile_row(**overrides) -> dict:
    row = {"chat_id": 123, "ory_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "updated_at": None}
    for field in bot_profile.PROFILE_MIRROR_FIELDS:
        row[field] = ""
    row.update(overrides)
    return row


def test_mirror_disabled_by_flag_skips_without_touching_persona_pool(monkeypatch):
    monkeypatch.setattr("config.BOT_PROFILE_DUAL_WRITE_ENABLED", False, raising=False)
    monkeypatch.setattr("config.BOT_PROFILE_DUAL_WRITE_CHAT_IDS", set(), raising=False)

    async def _forbidden_get_persona_pool():
        raise AssertionError("get_persona_pool должен быть недостижим при выключенном флаге")

    monkeypatch.setattr("db.connection.get_persona_pool", _forbidden_get_persona_pool, raising=False)

    outcome = asyncio.run(bot_profile.mirror_profile_row(_profile_row()))
    assert outcome == "skipped_flag"


def test_mirror_allowlist_excludes_chat_id_not_listed(monkeypatch):
    monkeypatch.setattr("config.BOT_PROFILE_DUAL_WRITE_ENABLED", True, raising=False)
    monkeypatch.setattr("config.BOT_PROFILE_DUAL_WRITE_CHAT_IDS", {999}, raising=False)

    outcome = asyncio.run(bot_profile.mirror_profile_row(_profile_row(chat_id=123)))
    assert outcome == "skipped_flag"


def test_mirror_skips_t0_without_ory_id(monkeypatch):
    monkeypatch.setattr("config.BOT_PROFILE_DUAL_WRITE_ENABLED", True, raising=False)
    monkeypatch.setattr("config.BOT_PROFILE_DUAL_WRITE_CHAT_IDS", set(), raising=False)

    async def _forbidden_get_persona_pool():
        raise AssertionError("T0 (ory_id=None) не должен обращаться к persona pool")

    monkeypatch.setattr("db.connection.get_persona_pool", _forbidden_get_persona_pool, raising=False)

    outcome = asyncio.run(bot_profile.mirror_profile_row(_profile_row(ory_id=None)))
    assert outcome == "skipped_t0"


def test_mirror_fk_violation_is_skipped_not_retried(monkeypatch):
    monkeypatch.setattr("config.BOT_PROFILE_DUAL_WRITE_ENABLED", True, raising=False)
    monkeypatch.setattr("config.BOT_PROFILE_DUAL_WRITE_CHAT_IDS", set(), raising=False)

    call_count = {"n": 0}

    async def _execute(query, *params):
        call_count["n"] += 1
        raise asyncpg.ForeignKeyViolationError("fk")

    async def _get_persona_pool():
        return _Pool(_Conn(on_execute=_execute))

    monkeypatch.setattr("db.connection.get_persona_pool", _get_persona_pool, raising=False)

    outcome = asyncio.run(bot_profile.mirror_profile_row(_profile_row()))
    assert outcome == "fk_skipped"
    assert call_count["n"] == 1  # ни одной повторной попытки


def test_mirror_pool_failure_is_caught_not_raised(monkeypatch):
    monkeypatch.setattr("config.BOT_PROFILE_DUAL_WRITE_ENABLED", True, raising=False)
    monkeypatch.setattr("config.BOT_PROFILE_DUAL_WRITE_CHAT_IDS", set(), raising=False)

    async def _get_persona_pool():
        raise OSError("connection refused")

    monkeypatch.setattr("db.connection.get_persona_pool", _get_persona_pool, raising=False)

    outcome = asyncio.run(bot_profile.mirror_profile_row(_profile_row()))
    assert outcome == "failed"


def test_mirror_stale_guard_is_not_reported_as_mirrored(monkeypatch):
    """0-row upsert (guard в MIRROR_SQL отклонил как устаревшую) — отдельный
    исход, не "mirrored" (cold-review: command tag раньше не разбирался).
    """
    monkeypatch.setattr("config.BOT_PROFILE_DUAL_WRITE_ENABLED", True, raising=False)
    monkeypatch.setattr("config.BOT_PROFILE_DUAL_WRITE_CHAT_IDS", set(), raising=False)

    async def _execute(query, *params):
        return "INSERT 0 0"  # ON CONFLICT DO UPDATE ... WHERE не совпало

    async def _get_persona_pool():
        return _Pool(_Conn(on_execute=_execute))

    monkeypatch.setattr("db.connection.get_persona_pool", _get_persona_pool, raising=False)

    before = bot_profile.MIRROR_COUNTS["mirrored"]
    outcome = asyncio.run(bot_profile.mirror_profile_row(_profile_row()))
    assert outcome == "stale_guard"
    assert bot_profile.MIRROR_COUNTS["mirrored"] == before  # не инкрементирован


def test_mirror_success_passes_utc_aware_updated_at(monkeypatch):
    monkeypatch.setattr("config.BOT_PROFILE_DUAL_WRITE_ENABLED", True, raising=False)
    monkeypatch.setattr("config.BOT_PROFILE_DUAL_WRITE_CHAT_IDS", set(), raising=False)

    captured_params = {}

    async def _execute(query, *params):
        captured_params["params"] = params
        return "INSERT 0 1"

    async def _get_persona_pool():
        return _Pool(_Conn(on_execute=_execute))

    monkeypatch.setattr("db.connection.get_persona_pool", _get_persona_pool, raising=False)

    naive_updated_at = datetime(2026, 9, 17, 12, 0, 0)  # naive, как public.users.updated_at
    outcome = asyncio.run(bot_profile.mirror_profile_row(_profile_row(updated_at=naive_updated_at)))

    assert outcome == "mirrored"
    updated_at_param = captured_params["params"][-1]
    assert updated_at_param.tzinfo is not None
    assert updated_at_param.tzinfo == timezone.utc
    assert updated_at_param.replace(tzinfo=None) == naive_updated_at


# === per-chat_id лок: сериализация ===

def test_chat_lock_serializes_two_concurrent_mirrors_for_same_chat_id():
    order = []

    async def worker(name, delay):
        lock = await bot_profile.chat_lock(42)
        async with lock:
            order.append(f"{name}-start")
            await asyncio.sleep(delay)
            order.append(f"{name}-end")

    async def run():
        await asyncio.gather(worker("A", 0.02), worker("B", 0.0))

    asyncio.run(run())

    # Кто бы ни начал первым — его "end" идёт раньше "start" второго (нет
    # чередования start-A, start-B, end-A, end-B).
    assert order[1].endswith("-end")


def test_chat_lock_serializes_three_concurrent_mirrors_no_overlap(monkeypatch):
    """Регрессия на найденную cold-review гонку: очистка словаря _chat_locks
    по `not lock.locked()` ломала взаимоисключение ровно при третьем
    конкурирующем вызывающем (A выходит, чистит запись, пока B ещё ждёт на
    СТАРОМ Lock; C создаёт НОВЫЙ Lock через setdefault и заходит одновременно
    с B). Фикс: никогда не удалять запись из _chat_locks — этот тест
    проверяет наблюдаемый эффект (нет перекрытия), не реализацию."""
    monkeypatch.setattr(bot_profile, "_chat_locks", {}, raising=False)
    active = set()
    max_concurrent = 0
    overlap_events = []

    async def worker(name, start_delay, hold):
        nonlocal max_concurrent
        await asyncio.sleep(start_delay)
        lock = await bot_profile.chat_lock(99)
        async with lock:
            active.add(name)
            max_concurrent = max(max_concurrent, len(active))
            if len(active) > 1:
                overlap_events.append(frozenset(active))
            await asyncio.sleep(hold)
            active.discard(name)

    async def run():
        await asyncio.gather(
            worker("A", 0.0, 0.03),
            worker("B", 0.005, 0.0),   # приходит пока A ещё держит лок
            worker("C", 0.01, 0.0),    # приходит пока A ещё держит лок, после B встал в очередь
        )

    asyncio.run(run())

    assert max_concurrent == 1, f"overlap detected: {overlap_events}"
