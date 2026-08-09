"""Regression guard for Neon reference queries that must ignore search_path."""

from __future__ import annotations

import ast
import re
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
EXCLUDED_PATH_PARTS = {".git", "archive", "migrations", "tests", "wheels"}
REFERENCE_TABLES = {
    "activity_domain_multipliers",
    "bh_dimension_map",
    "event_schemas",
    "event_type_domain_map",
    "feature_flags",
    "learning_club_action_limits",
    "loyalty_pool_config",
    "payment_kind",
    "product",
    "projection_rules",
    "qualification_level",
    "qualification_levels_v4",
    "qualification_multipliers",
    "repo_domain_map",
    "reward_rules",
    "reward_rules_audit",
    "rewards_action_catalog",
    "student_stage_multipliers",
    "tariffs",
    "training_child",
    "training_setting",
}
RELATION_AFTER_SQL_KEYWORD = re.compile(
    r"\b(?:FROM|JOIN|UPDATE|INSERT\s+INTO|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?)"
    r"\s+([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?)",
    re.IGNORECASE,
)


def _runtime_python_files() -> list[Path]:
    return sorted(
        path
        for path in REPO_ROOT.rglob("*.py")
        if EXCLUDED_PATH_PARTS.isdisjoint(path.parts)
    )


def _unqualified_reference_relations(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        for match in RELATION_AFTER_SQL_KEYWORD.finditer(node.value):
            relation = match.group(1).lower()
            if "." not in relation and relation in REFERENCE_TABLES:
                violations.append((node.lineno, relation))
    return violations


def test_guard_detects_unqualified_reference_relation(tmp_path: Path) -> None:
    source = tmp_path / "query.py"
    source.write_text(
        'QUERY = "SELECT * FROM training_setting WHERE chat_id = $1"\n',
        encoding="utf-8",
    )

    assert _unqualified_reference_relations(source) == [(1, "training_setting")]


def test_guard_accepts_explicit_reference_schema(tmp_path: Path) -> None:
    source = tmp_path / "query.py"
    source.write_text(
        'QUERY = "SELECT * FROM public.training_setting WHERE chat_id = $1"\n',
        encoding="utf-8",
    )

    assert _unqualified_reference_relations(source) == []


def test_reference_queries_are_independent_from_search_path() -> None:
    violations = [
        f"{path.relative_to(REPO_ROOT)}:{line}: {relation}"
        for path in _runtime_python_files()
        for line, relation in _unqualified_reference_relations(path)
    ]

    assert not violations, (
        "Neon reference queries must use explicit public.<table>; "
        "session search_path is not stable behind a transaction pool:\n"
        + "\n".join(violations)
    )
