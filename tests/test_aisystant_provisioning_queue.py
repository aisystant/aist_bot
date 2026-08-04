"""WP-7 ORY-RT1 queue contract tests."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock


module_names = ("db", "db.connection", "config", "helpers", "helpers.dual_write")
original_modules = {name: sys.modules.get(name) for name in module_names}
try:
    db_module = types.ModuleType("db")
    db_connection = types.ModuleType("db.connection")
    db_connection.get_pool = AsyncMock()
    db_connection.get_persona_pool = AsyncMock()
    config_module = types.ModuleType("config")
    config_module.get_logger = logging.getLogger
    helpers_module = types.ModuleType("helpers")
    dual_write_module = types.ModuleType("helpers.dual_write")
    dual_write_module.post_event = AsyncMock()

    sys.modules["db"] = db_module
    sys.modules["db.connection"] = db_connection
    sys.modules["config"] = config_module
    sys.modules["helpers"] = helpers_module
    sys.modules["helpers.dual_write"] = dual_write_module

    spec = importlib.util.spec_from_file_location(
        "aisystant_queries_under_test",
        Path(__file__).parents[1] / "db/queries/aisystant.py",
    )
    aisystant_queries = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(aisystant_queries)
finally:
    for name, original in original_modules.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original

_enqueue_ory_provisioning = aisystant_queries._enqueue_ory_provisioning


def test_enqueue_uses_stable_suser_key_and_no_email() -> None:
    conn = AsyncMock()

    asyncio.run(_enqueue_ory_provisioning(conn, "10112", 206137832))

    conn.execute.assert_awaited_once()
    sql, suser_id, telegram_id = conn.execute.await_args.args
    assert suser_id == 10112
    assert telegram_id == 206137832
    assert "ON CONFLICT (suser_id)" in sql
    assert "email" not in sql.lower()


def test_enqueue_rejects_non_numeric_aisystant_id_before_database_write() -> None:
    conn = AsyncMock()

    try:
        asyncio.run(_enqueue_ory_provisioning(conn, "not-a-number", 42))
    except ValueError:
        pass
    else:
        raise AssertionError("non-numeric Aisystant id must not reach the queue")

    conn.execute.assert_not_awaited()
