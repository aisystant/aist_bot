"""Regression guard for runtime SQL that must ignore session search_path."""

from __future__ import annotations

import ast
import re
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
EXCLUDED_PATH_PARTS = {
    ".git",
    ".venv",
    "archive",
    "tests",
    "wheels",
}
RUNTIME_MIGRATIONS = {
    "015_helpdesk_tickets.py",
    "016_canary_state.py",
    "017_retry_exhausted_date.py",
    "018_bot_recheck_at.py",
    "019_projection_dlq.py",
    "023_consent_grant.py",
    "024_reminder_bot_id_not_null.py",
    "025_learning_schema_railway.py",
    "031_onboarder_completion_marks.py",
    "032_notification_queue.py",
    "033_homework_content.py",
    "036_fix_stuck_marathon_progress.py",
    "037_scheduled_post_dedup_lock.py",
    "migrate_products.py",
}
SQL_CALL_METHODS = {"execute", "executemany", "fetch", "fetchrow", "fetchval"}
RELATION_AFTER_SQL_KEYWORD = re.compile(
    r"\b(?:FROM|JOIN|UPDATE|INSERT\s+INTO|DELETE\s+FROM|"
    r"TRUNCATE(?:\s+TABLE)?|ALTER\s+TABLE(?:\s+IF\s+EXISTS)?|"
    r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?|"
    r"DROP\s+TABLE(?:\s+IF\s+EXISTS)?|COMMENT\s+ON\s+TABLE|REFERENCES)"
    r"\s+([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?)",
    re.IGNORECASE,
)
INDEX_TARGET = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?INDEX(?:\s+IF\s+NOT\s+EXISTS)?\s+"
    r"[a-z_][a-z0-9_]*\s+ON\s+"
    r"([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?)",
    re.IGNORECASE,
)
TRIGGER_TARGET = re.compile(
    r"\bCREATE\s+TRIGGER\s+[a-z_][a-z0-9_]*.*?\bON\s+"
    r"([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?)",
    re.IGNORECASE | re.DOTALL,
)
REGCLASS_LITERAL = re.compile(
    r"['\"]([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?)['\"]\s*::\s*regclass",
    re.IGNORECASE,
)
TO_REGCLASS_LITERAL = re.compile(
    r"\bto_regclass\(\s*['\"]"
    r"([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?)['\"]\s*\)",
    re.IGNORECASE,
)
PGCRYPTO_CALL = re.compile(
    r"(?P<qualified>public\.)?pgp_sym_(?P<operation>encrypt|decrypt)\s*\("
    r"(?P<arguments>[^)]*)\)",
    re.IGNORECASE | re.DOTALL,
)
CTE_NAME = re.compile(
    r"(?:\bWITH(?:\s+RECURSIVE)?|,)\s*([a-z_][a-z0-9_]*)\s+AS\s+"
    r"(?:(?:NOT\s+)?MATERIALIZED\s+)?\(",
    re.IGNORECASE,
)
# The lightweight scanner sees these tokens after SQL keywords although they
# are not relations: UPDATE SET, FOR UPDATE SKIP/OF, EXTRACT(... FROM d).
NON_RELATION_SQL_KEYWORDS = {"d", "of", "on", "set", "skip"}


def _runtime_python_files() -> list[Path]:
    return sorted(
        path
        for path in REPO_ROOT.rglob("*.py")
        if EXCLUDED_PATH_PARTS.isdisjoint(path.parts)
        and ("migrations" not in path.parts or path.name in RUNTIME_MIGRATIONS)
    )


def _runtime_queries(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bound_sql = {
        target.id: node.value.value
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in (
            node.targets if isinstance(node, ast.Assign) else [node.target]
        )
        if isinstance(target, ast.Name)
    }
    queries: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Attribute)
            or node.func.attr not in SQL_CALL_METHODS
            or not node.args
        ):
            continue
        query_arg = node.args[0]
        if isinstance(query_arg, ast.Constant) and isinstance(query_arg.value, str):
            query = query_arg.value
        elif isinstance(query_arg, ast.JoinedStr):
            query = "".join(
                part.value if isinstance(part, ast.Constant) else "{expression}"
                for part in query_arg.values
            )
        elif isinstance(query_arg, ast.Name):
            query = bound_sql.get(query_arg.id, "")
        else:
            query = ""
        if query:
            queries.append((query_arg.lineno, query))
    return queries


def _unqualified_runtime_relations(path: Path) -> list[tuple[int, str]]:
    violations: list[tuple[int, str]] = []
    for query_lineno, query in _runtime_queries(path):
        cte_names = {match.group(1).lower() for match in CTE_NAME.finditer(query)}
        for match in RELATION_AFTER_SQL_KEYWORD.finditer(query):
            relation = match.group(1).lower()
            followed_by_call = query[match.end(1):].lstrip().startswith("(")
            if (
                "." not in relation
                and relation not in cte_names
                and relation not in NON_RELATION_SQL_KEYWORDS
                and not followed_by_call
            ):
                violations.append((query_lineno, relation))
        for pattern in (
            INDEX_TARGET,
            TRIGGER_TARGET,
            REGCLASS_LITERAL,
            TO_REGCLASS_LITERAL,
        ):
            for match in pattern.finditer(query):
                relation = match.group(1).lower()
                if "." not in relation:
                    violations.append((query_lineno, relation))
    return violations


def test_guard_detects_unqualified_runtime_relation(tmp_path: Path) -> None:
    source = tmp_path / "query.py"
    source.write_text(
        'conn.fetch("SELECT * FROM training_setting WHERE chat_id = $1")\n',
        encoding="utf-8",
    )

    assert _unqualified_runtime_relations(source) == [(1, "training_setting")]


def test_guard_accepts_explicit_runtime_schema(tmp_path: Path) -> None:
    source = tmp_path / "query.py"
    source.write_text(
        'conn.fetch("SELECT * FROM public.training_setting WHERE chat_id = $1")\n',
        encoding="utf-8",
    )

    assert _unqualified_runtime_relations(source) == []


def test_guard_covers_ddl_and_regclass_targets(tmp_path: Path) -> None:
    source = tmp_path / "query.py"
    source.write_text(
        "\n".join(
            (
                'conn.execute("CREATE INDEX idx_training ON training_setting(chat_id)")',
                'conn.execute("CREATE TRIGGER trg_training AFTER INSERT ON training_setting EXECUTE FUNCTION notify()")',
                'conn.fetch("SELECT \'training_setting\'::regclass")',
                'conn.fetch("SELECT to_regclass(\'training_setting\')")',
            )
        ),
        encoding="utf-8",
    )

    assert _unqualified_runtime_relations(source) == [
        (1, "training_setting"),
        (2, "training_setting"),
        (3, "training_setting"),
        (4, "training_setting"),
    ]


def test_runtime_queries_are_independent_from_search_path() -> None:
    violations = [
        f"{path.relative_to(REPO_ROOT)}:{line}: {relation}"
        for path in _runtime_python_files()
        for line, relation in _unqualified_runtime_relations(path)
    ]

    assert not violations, (
        "Runtime SQL must use explicit schema.<table>; "
        "session search_path is not stable behind a transaction pool:\n"
        + "\n".join(violations)
    )


def test_runtime_pgcrypto_calls_are_schema_qualified_and_typed() -> None:
    violations: list[str] = []
    call_count = 0
    for path in _runtime_python_files():
        for _line, query in _runtime_queries(path):
            for match in PGCRYPTO_CALL.finditer(query):
                call_count += 1
                operation = match.group("operation").lower()
                required_text_casts = 2 if operation == "encrypt" else 1
                if not match.group("qualified"):
                    violations.append(
                        f"{path.relative_to(REPO_ROOT)}: public schema is missing"
                    )
                if match.group("arguments").count("::text") < required_text_casts:
                    violations.append(
                        f"{path.relative_to(REPO_ROOT)}: {operation} arguments are not explicit"
                    )

    assert call_count == 5, "Update the pgcrypto guard when adding a runtime call"
    assert not violations, "\n".join(violations)
