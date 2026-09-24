import asyncio
from pathlib import Path

import pytest

from readiness import readiness_snapshot


REPO_ROOT = Path(__file__).parents[1]


@pytest.fixture(autouse=True)
def _reset_mentorship_archive_counters():
    """_dropped_counters (engines.mentorship.archive_tap) — процессный
    singleton, растёт от побочных эффектов ЛЮБОГО теста в сьюте, который
    гоняет archive_tap worker против фейковой БД (найдено пир-сессией 24.09
    при первом же прогоне полного tests/: значения этого модуля не нулевые
    уже до readiness-тестов). Тесты `/ready` не владеют этим счётчиком —
    изолируем на время каждого теста, не полагаясь на порядок запуска."""
    import engines.mentorship.archive_tap as archive_tap

    original = dict(archive_tap._dropped_counters)
    archive_tap._dropped_counters.update({k: 0 for k in original})
    yield
    archive_tap._dropped_counters.clear()
    archive_tap._dropped_counters.update(original)


def test_readiness_module_is_packaged_in_runtime_image() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY readiness.py ." in dockerfile


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
        "components": {
            "database": "ready",
            "scheduler": "ready",
            "mentorship_archive": {
                "status": "ready",
                "queue_depth": 0,
                "queue_maxsize": 1000,
                "dropped_queue_full": 0,
                "dropped_write_failed": 0,
            },
        },
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
        "components": {
            "database": "timeout",
            "scheduler": "ready",
            "mentorship_archive": {
                "status": "ready",
                "queue_depth": 0,
                "queue_maxsize": 1000,
                "dropped_queue_full": 0,
                "dropped_write_failed": 0,
            },
        },
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
        "components": {
            "database": "unavailable",
            "scheduler": "ready",
            "mentorship_archive": {
                "status": "ready",
                "queue_depth": 0,
                "queue_maxsize": 1000,
                "dropped_queue_full": 0,
                "dropped_write_failed": 0,
            },
        },
    }
    assert "secret" not in str(payload)


@pytest.mark.asyncio
async def test_readiness_mentorship_archive_degraded_does_not_change_http_status() -> None:
    """Консенсус пир-сессии 24.09: dropped>0 — сигнал оператору внутри JSON
    (degraded), но НЕ повод возвращать 503 — счётчик исторический, не
    отражает текущее состояние бота (см. mentorship_archive_readiness()).
    _reset_mentorship_archive_counters (autouse) гарантирует чистый ноль
    до этого теста и восстанавливает исходное значение после."""
    import engines.mentorship.archive_tap as archive_tap

    archive_tap._dropped_counters["write_failed"] += 1

    async def database_ready() -> bool:
        return True

    payload, status = await readiness_snapshot(
        database_probe=database_ready,
        scheduler_probe=lambda: "ready",
    )

    assert status == 200
    assert payload["status"] == "ready"
    assert payload["components"]["mentorship_archive"]["status"] == "degraded"
    assert payload["components"]["mentorship_archive"]["dropped_write_failed"] == 1
