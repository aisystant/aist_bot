"""Bot startup issues no DDL against the Neon databases (WP-544 F16).

The bot's own Neon roles have no right to create objects. Two startup steps used to
run DDL there: create_tables_health() on the health database and migration 037 on
the publication database. Both sets of objects already exist, so startup must not
call them any more. No test runs main() of bot.py, so a source check guards it.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _bot_source() -> str:
    return (REPO_ROOT / "bot.py").read_text(encoding="utf-8")


def _string_constants(source: str) -> set[str]:
    return {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_startup_does_not_create_health_tables():
    tree = ast.parse(_bot_source())
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert "create_tables_health" not in names
    assert "create_tables_health" not in imported


def test_startup_does_not_run_migration_037():
    constants = _string_constants(_bot_source())

    assert "db.migrations.037_scheduled_post_dedup_lock" not in constants


def test_migration_037_kept_as_manual_script():
    path = REPO_ROOT / "db" / "migrations" / "037_scheduled_post_dedup_lock.py"

    assert path.is_file()
    assert "Manual run:" in path.read_text(encoding="utf-8")


def test_create_tables_health_removed_from_models():
    source = (REPO_ROOT / "db" / "models.py").read_text(encoding="utf-8")
    functions = {
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "create_tables" in functions
    assert "create_tables_health" not in functions
