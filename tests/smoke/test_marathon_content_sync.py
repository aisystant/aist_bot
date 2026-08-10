"""Regression tests for the marathon content source-of-truth sync script."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).parents[2]
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync-marathon-content.sh"


def _write_content(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"days": {"1": {"lesson": marker}}}),
        encoding="utf-8",
    )


def _workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    bot_dir = tmp_path / "workspace" / "bot"
    script = bot_dir / "scripts" / "sync-marathon-content.sh"
    source = (
        tmp_path
        / "workspace"
        / "DS-marathon-v2-tseren"
        / "materials"
        / "participants"
        / "marathon-content.json"
    )
    destination = bot_dir / "data" / "marathon-content.json"
    script.parent.mkdir(parents=True)
    shutil.copy2(SYNC_SCRIPT, script)
    return script, source, destination


def _check(script: Path, *, source_override: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("MARATHON_CONTENT_SRC", None)
    if source_override is not None:
        environment["MARATHON_CONTENT_SRC"] = str(source_override)
    return subprocess.run(
        ["bash", str(script), "--check"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_check_accepts_equal_sibling_source(tmp_path: Path) -> None:
    script, source, destination = _workspace(tmp_path)
    _write_content(source, "same")
    _write_content(destination, "same")

    result = _check(script)

    assert result.returncode == 0
    assert "расхождений нет" in result.stdout


def test_check_reports_real_content_drift(tmp_path: Path) -> None:
    script, source, destination = _workspace(tmp_path)
    _write_content(source, "source")
    _write_content(destination, "runtime")

    result = _check(script)

    assert result.returncode == 1
    assert "Расхождение" in result.stdout


def test_check_reports_missing_source_separately(tmp_path: Path) -> None:
    script, _source, destination = _workspace(tmp_path)
    _write_content(destination, "runtime")

    result = _check(script)

    assert result.returncode == 2
    assert "Авторский файл не найден" in result.stdout


def test_explicit_source_override_has_priority(tmp_path: Path) -> None:
    script, _source, destination = _workspace(tmp_path)
    override = tmp_path / "authoritative" / "marathon-content.json"
    _write_content(override, "override")
    _write_content(destination, "override")

    result = _check(script, source_override=override)

    assert result.returncode == 0


def test_invalid_source_json_is_not_reported_as_content_drift(tmp_path: Path) -> None:
    script, source, destination = _workspace(tmp_path)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("{not-json", encoding="utf-8")
    _write_content(destination, "runtime")

    result = _check(script)

    assert result.returncode == 3
    assert "не прошёл JSON-валидацию" in result.stdout


def test_comparison_error_is_not_reported_as_content_drift(tmp_path: Path) -> None:
    script, source, destination = _workspace(tmp_path)
    _write_content(source, "source")
    destination.mkdir(parents=True)

    result = _check(script)

    assert result.returncode == 3
    assert "Не удалось сравнить" in result.stdout
