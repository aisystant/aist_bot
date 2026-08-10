import asyncio

import pytest

from readiness import readiness_snapshot


@pytest.mark.asyncio
async def test_readiness_reports_ready_dependencies() -> None:
    async def database_ready() -> bool:
        return True

    payload, status = await readiness_snapshot(
        database_probe=database_ready,
        scheduler_probe=lambda: "ready",
    )

    assert status == 200
    assert payload == {
        "status": "ready",
        "components": {"database": "ready", "scheduler": "ready"},
    }


@pytest.mark.asyncio
async def test_readiness_bounds_database_wait() -> None:
    async def database_hangs() -> bool:
        await asyncio.Event().wait()
        return True

    payload, status = await readiness_snapshot(
        database_probe=database_hangs,
        scheduler_probe=lambda: "ready",
        timeout_seconds=0.001,
    )

    assert status == 503
    assert payload == {
        "status": "degraded",
        "components": {"database": "timeout", "scheduler": "ready"},
    }


@pytest.mark.asyncio
async def test_readiness_hides_dependency_exception_details() -> None:
    async def database_fails() -> bool:
        raise RuntimeError("postgres://user:secret@example.invalid/database")

    payload, status = await readiness_snapshot(
        database_probe=database_fails,
        scheduler_probe=lambda: "ready",
    )

    assert status == 503
    assert payload == {
        "status": "degraded",
        "components": {"database": "unavailable", "scheduler": "ready"},
    }
    assert "secret" not in str(payload)
