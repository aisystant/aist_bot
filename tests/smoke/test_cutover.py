"""
WP-268 Phase 2 cut-over — smoke-тесты single-write пути.

Проверяют что после cut-over:
1. Bot writers НЕ выполняют INSERT/UPDATE в legacy public.users / development.user_state /
   notification_log / qa_history / request_traces / development.user_events.
2. Event-gateway POST envelope корректен (PII-инвариант, стабильный external_id).
3. Прямые INSERT в новые БД (где сохранены — Pattern 2: digital_twins на dt_pool)
   работают как ожидается.

Покрытие: статический grep + functional mock.
"""

import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if _PROJECT_ROOT not in sys.path or sys.path.index(_PROJECT_ROOT) > 0:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")
os.environ.setdefault("EVENT_GATEWAY_URL", "https://event-gateway.test.invalid")


# ---------------------------------------------------------------------------
# Static grep: legacy SQL writers удалены из cut-over writers.
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_comments_and_docstrings(text: str) -> str:
    """Удалить line-comments (#) и тройные docstrings/strings из исходника.

    Грубый stripper: не парсит AST, но достаточен для отделения SQL-литералов
    в коде от упоминаний SQL в комментариях/документации.
    """
    # Удалить тройные кавычки '''...''' и \"\"\"...\"\"\" (включая многострочные)
    text = re.sub(r'"""[\s\S]*?"""', "", text)
    text = re.sub(r"'''[\s\S]*?'''", "", text)
    # Удалить line comments
    out_lines = []
    for line in text.splitlines():
        # Грубо: всё после первого # вне строки. Достаточно для нашей задачи.
        idx = line.find("#")
        if idx >= 0:
            out_lines.append(line[:idx])
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


# Маппинг writer'а → строки/паттерны legacy SQL, которые ДОЛЖНЫ ОТСУТСТВОВАТЬ
# в исходном коде после cut-over. Берём только writer-функции (не helpers/reads).
_LEGACY_PATTERNS = {
    "users.py:update_intern_path": {
        "file": "db/queries/users.py",
        # update_intern больше не делает UPDATE на public.users / user_state
        "must_not_match": [
            r"UPDATE public\.users SET .* WHERE telegram_id = \$1",
            r"UPDATE development\.user_state SET .* WHERE chat_id = \$1",
        ],
        "must_match_in_func": "async def update_intern",
    },
    "users.py:update_tg_username": {
        "file": "db/queries/users.py",
        "must_not_match": [
            r"UPDATE public\.users SET tg_username",
        ],
        "must_match_in_func": "async def update_tg_username",
    },
    "identity.py:link_ory": {
        "file": "db/queries/identity.py",
        "must_not_match": [
            r"UPDATE public\.users\s+SET ory_id",
        ],
        "must_match_in_func": "async def link_ory",
    },
    "identity.py:update_user_dt": {
        "file": "db/queries/identity.py",
        "must_not_match": [
            r"UPDATE public\.users SET dt_user_id",
        ],
        "must_match_in_func": "async def update_user_dt",
    },
    "identity.py:update_user_tier": {
        "file": "db/queries/identity.py",
        "must_not_match": [
            r"UPDATE public\.users SET tier",
        ],
        "must_match_in_func": "async def update_user_tier",
    },
    "aisystant.py:save_aisystant_link": {
        "file": "db/queries/aisystant.py",
        "must_not_match": [
            r"UPDATE public\.users\s+SET\s+aisystant_id",
            r"INSERT INTO development\.identity_map",
        ],
        "must_match_in_func": "async def save_aisystant_link",
    },
    "aisystant.py:remove_aisystant_link": {
        "file": "db/queries/aisystant.py",
        "must_not_match": [
            r"UPDATE public\.users\s+SET\s+aisystant_id = NULL",
        ],
        "must_match_in_func": "async def remove_aisystant_link",
    },
    "qa.py:save_qa": {
        "file": "db/queries/qa.py",
        "must_not_match": [
            r"INSERT INTO qa_history",
        ],
        "must_match_in_func": "async def save_qa",
    },
    "qa.py:update_qa_helpful": {
        "file": "db/queries/qa.py",
        "must_not_match": [
            r"UPDATE qa_history SET helpful",
        ],
        "must_match_in_func": "async def update_qa_helpful",
    },
    "qa.py:update_qa_comment": {
        "file": "db/queries/qa.py",
        "must_not_match": [
            r"UPDATE qa_history SET user_comment",
        ],
        "must_match_in_func": "async def update_qa_comment",
    },
    "notifications.py:try_insert_notification": {
        "file": "db/queries/notifications.py",
        "must_not_match": [
            r"INSERT INTO notification_log",
        ],
        "must_match_in_func": "async def try_insert_notification",
    },
    "events.py:log_event": {
        "file": "db/queries/events.py",
        "must_not_match": [
            r"INSERT INTO development\.user_events",
        ],
        "must_match_in_func": "async def log_event",
    },
    "tracing.py:finish_trace": {
        "file": "core/tracing.py",
        "must_not_match": [
            r"INSERT INTO request_traces",
        ],
        "must_match_in_func": "async def finish_trace",
    },
    "subscription.py:upsert_subscription_grant": {
        "file": "db/queries/subscription.py",
        "must_not_match": [
            r"UPDATE subscription_grants",
            r"INSERT INTO subscription_grants",
        ],
        "must_match_in_func": "async def upsert_subscription_grant",
    },
    "oauth_server.py:twin_callback_handler": {
        "file": "oauth_server.py",
        "must_not_match": [
            r"UPDATE public\.users SET dt_connected_at",
        ],
        "must_match_in_func": "twin_callback_handler",
    },
}


@pytest.mark.parametrize("name,spec", list(_LEGACY_PATTERNS.items()))
def test_cutover_legacy_writes_removed(name, spec):
    """Каждый cut-over writer не содержит legacy SQL writes в исходном коде
    (комментарии и docstrings игнорируются — там можно упоминать legacy для
    документации removal)."""
    path = Path(_PROJECT_ROOT) / spec["file"]
    raw = _read(path)
    # Sanity: целевая функция всё ещё в файле
    assert spec["must_match_in_func"] in raw, (
        f"{name}: целевая функция/handler '{spec['must_match_in_func']}' "
        f"не найдена в {spec['file']}"
    )
    code = _strip_comments_and_docstrings(raw)
    for pattern in spec["must_not_match"]:
        match = re.search(pattern, code)
        assert match is None, (
            f"{name}: legacy pattern `{pattern}` всё ещё присутствует в "
            f"{spec['file']} (вне комментариев). Cut-over удалил все writes "
            f"на legacy БД (line содержит: '{match.group(0)}')."
        )


def test_cutover_dt_sync_writes_only_to_dt_pool():
    """dt_sync.py: digital_twins INSERT остаётся (Pattern 2 — это уже новая БД,
    digitaltwin pool через get_dt_pool, не aist_bot legacy)."""
    text = _read(Path(_PROJECT_ROOT) / "db" / "queries" / "dt_sync.py")
    # INSERT INTO digital_twins есть (Pattern 2)
    assert "INSERT INTO digital_twins" in text, (
        "dt_sync.py: INSERT INTO digital_twins пропал — Pattern 2 (новая БД) "
        "должен остаться. dt_pool/get_dt_pool — это digitaltwin Neon БД "
        "(WP-227), не legacy aist_bot."
    )
    # И dt_conn используется (через get_dt_pool, новая БД)
    assert "dt_pool.acquire()" in text or "dt_conn" in text


def test_cutover_get_intern_bootstrap_remains():
    """users.py:get_intern: SELECT/INSERT в public.users остаётся как
    Pattern 2-bootstrap (state read path до WP-269 follow-up)."""
    text = _read(Path(_PROJECT_ROOT) / "db" / "queries" / "users.py")
    # get_intern всё ещё имеет INSERT INTO public.users (bootstrap)
    assert "INSERT INTO public.users" in text, (
        "users.py: bootstrap INSERT в public.users пропал — get_intern "
        "должен сохранить Pattern 2-bootstrap для FSM/scheduler state read path."
    )


# ---------------------------------------------------------------------------
# Functional: post_event envelope корректен для cut-over writers.
# ---------------------------------------------------------------------------


def _load_module_isolated(name, relative_path):
    """Загрузить модуль напрямую без триггера db/queries/__init__.py
    (который импортирует feed.py — там Python 3.10 union syntax `dict | None`,
    несовместим с 3.9)."""
    import importlib.util
    import types
    # Создаём минимальные namespace package shells
    sys.modules.setdefault("db", types.ModuleType("db"))
    sys.modules.setdefault("db.queries", types.ModuleType("db.queries"))
    sys.modules.setdefault("db.connection", types.ModuleType("db.connection"))
    # db.connection.get_pool stub (для модулей, которые делают from db.connection import get_pool)
    if not hasattr(sys.modules["db.connection"], "get_pool"):
        async def _stub_get_pool():
            raise AssertionError("get_pool stub: should not be called in cut-over tests")
        sys.modules["db.connection"].get_pool = _stub_get_pool
        sys.modules["db.connection"].get_dt_pool = _stub_get_pool
        sys.modules["db.connection"].get_platform_pool = _stub_get_pool
        sys.modules["db.connection"].acquire = _stub_get_pool
    abs_path = Path(_PROJECT_ROOT) / relative_path
    spec = importlib.util.spec_from_file_location(name, abs_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_cutover_user_updated_emits_event_only(monkeypatch):
    """update_intern → один post_event POST, никаких legacy DB-операций."""
    users_module = _load_module_isolated("db.queries.users", "db/queries/users.py")

    captured = []

    async def fake_post_event(**kwargs):
        captured.append(kwargs)

    async def fake_resolve_ory(chat_id):
        return "ory-uuid-555"

    monkeypatch.setattr(users_module, "post_event", fake_post_event)
    monkeypatch.setattr(users_module, "resolve_ory_id_from_chat", fake_resolve_ory)

    # gateway_mcp.is_connected → False, чтобы не лезть в DT sync
    import types as _types
    class FakeGW:
        def is_connected(self, chat_id):
            return False

    fake_module = _types.ModuleType("clients.gateway_mcp")
    fake_module.gateway_mcp = FakeGW()
    sys.modules["clients"] = _types.ModuleType("clients")
    sys.modules["clients.gateway_mcp"] = fake_module

    await users_module.update_intern(
        chat_id=555,
        mode="marathon",
        schedule_time="07:30",
    )

    assert len(captured) == 1
    env = captured[0]
    assert env["event_type"] == "user_updated"
    assert env["external_id"].startswith("user-updated-555-")
    assert env["account_id"] == "ory-uuid-555"
    assert "telegram_id" not in env["payload"]
    assert "mode" in env["payload"]["fields_updated"]
    assert "schedule_time" in env["payload"]["fields_updated"]


@pytest.mark.asyncio
async def test_cutover_save_qa_event_only_no_legacy_insert(monkeypatch):
    """save_qa → один post_event POST, qa_history НЕ затрагивается."""
    qa_module = _load_module_isolated("db.queries.qa", "db/queries/qa.py")

    captured = []

    async def fake_post_event(**kwargs):
        captured.append(kwargs)

    async def fake_resolve_ory(chat_id):
        return "ory-q1"

    pool_calls = {"n": 0}

    async def spying_get_pool():
        pool_calls["n"] += 1
        raise AssertionError("get_pool should NOT be called in cut-over save_qa")

    monkeypatch.setattr(qa_module, "post_event", fake_post_event)
    monkeypatch.setattr(qa_module, "resolve_ory_id_from_chat", fake_resolve_ory)
    monkeypatch.setattr(qa_module, "get_pool", spying_get_pool)

    qa_id = await qa_module.save_qa(
        chat_id=999, mode="marathon", context_topic="topic-1",
        question="Q?", answer="A.",
    )

    assert pool_calls["n"] == 0
    assert qa_id is not None
    assert isinstance(qa_id, int)
    assert len(captured) == 1
    env = captured[0]
    assert env["event_type"] == "qa_query"
    assert env["account_id"] == "ory-q1"
    assert "question" not in env["payload"]
    assert "answer" not in env["payload"]
    assert env["payload"]["question_length"] == 2
    assert env["payload"]["answer_length"] == 2


@pytest.mark.asyncio
async def test_cutover_log_event_no_legacy_insert(monkeypatch):
    """log_event → post_event только; никакого INSERT в development.user_events."""
    events_module = _load_module_isolated("db.queries.events", "db/queries/events.py")

    captured = []

    async def fake_post_event(**kwargs):
        captured.append(kwargs)

    async def fake_get_user_uuid(user_id):
        return "11111111-2222-3333-4444-555555555555"

    monkeypatch.setattr(events_module, "post_event", fake_post_event)

    import types as _types
    fake_id = _types.ModuleType("db.queries.identity")
    fake_id.get_user_uuid = fake_get_user_uuid
    sys.modules["db.queries.identity"] = fake_id

    eid = await events_module.log_event(
        user_id=42, event_type="marathon_step",
        payload={"topic_id": 1}, source="bot",
    )

    assert eid is not None
    assert isinstance(eid, int)
    assert len(captured) == 1
    env = captured[0]
    assert env["event_type"] == "marathon_step"
    assert env["account_id"] == "11111111-2222-3333-4444-555555555555"
    assert env["payload"]["payload_keys"] == ["topic_id"]
    assert env["external_id"].startswith("bot-42-marathon_step-")


@pytest.mark.asyncio
async def test_cutover_try_insert_notification_event_only(monkeypatch):
    """try_insert_notification → post_event; notification_log INSERT удалён."""
    notif_module = _load_module_isolated(
        "db.queries.notifications", "db/queries/notifications.py"
    )

    captured = []

    async def fake_post_event(**kwargs):
        captured.append(kwargs)

    async def fake_resolve_ory(chat_id):
        return "ory-n1"

    monkeypatch.setattr(notif_module, "post_event", fake_post_event)
    monkeypatch.setattr(notif_module, "resolve_ory_id_from_chat", fake_resolve_ory)

    inserted = await notif_module.try_insert_notification(
        chat_id=111, notification_type="marathon",
        idempotency_key="marathon:111:2026-04-26:lesson_1",
        payload={"topic": "Lesson 1"},
    )

    assert inserted is True
    assert len(captured) == 1
    env = captured[0]
    assert env["event_type"] == "notification_sent"
    assert env["external_id"] == "notification-marathon:111:2026-04-26:lesson_1"
    assert env["payload"]["payload_keys"] == ["topic"]
    assert "Lesson 1" not in str(env["payload"])


@pytest.mark.asyncio
async def test_cutover_link_ory_event_only(monkeypatch):
    """link_ory → post_event; UPDATE public.users удалён, всегда возвращает True."""
    identity_module = _load_module_isolated(
        "db.queries.identity", "db/queries/identity.py"
    )

    captured = []

    async def fake_post_event(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(identity_module, "post_event", fake_post_event)

    result = await identity_module.link_ory(
        telegram_id=777,
        ory_id="aaaa-bbbb-cccc",
        email="user@example.com",
    )

    assert result is True
    assert len(captured) == 1
    env = captured[0]
    assert env["event_type"] == "ory_linked"
    assert env["external_id"] == "ory-linked-aaaa-bbbb-cccc"
    assert env["account_id"] == "aaaa-bbbb-cccc"
    assert env["payload"]["tier_to"] == "T1"
    assert env["payload"]["email_present"] is True
    assert "telegram_id" not in env["payload"]


@pytest.mark.asyncio
async def test_cutover_upsert_subscription_grant_event_only(monkeypatch):
    """upsert_subscription_grant → post_event; legacy UPDATE/INSERT удалены."""
    sub_module = _load_module_isolated(
        "db.queries.subscription", "db/queries/subscription.py"
    )
    from datetime import datetime

    captured = []

    async def fake_post_event(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(sub_module, "post_event", fake_post_event)

    valid_until = datetime(2026, 7, 1, 0, 0, 0)
    await sub_module.upsert_subscription_grant(
        telegram_id=42,
        valid_until=valid_until,
        source="tg_stars",
    )

    assert len(captured) == 1
    env = captured[0]
    assert env["event_type"] == "subscription_granted"
    assert env["payload"]["mode"] == "upsert"
    assert env["payload"]["source"] == "tg_stars"
    assert "tg_stars" in env["external_id"]
    assert "2026-07-01" in env["external_id"]
