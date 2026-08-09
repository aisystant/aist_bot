"""Safe SQL query builders with identifier validation.

Used to avoid Bandit B608 false positives when table/column names come from
hard-coded whitelists and cannot be passed as PostgreSQL parameters.
"""
from __future__ import annotations

import re

_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_QUALIFIED_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")


def _validate_identifier(name: str) -> str:
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


def _validate_qualified(name: str) -> str:
    if not _QUALIFIED_RE.match(name):
        raise ValueError(f"Invalid qualified SQL identifier: {name!r}")
    return name


def _validate_table(name: str) -> str:
    return _validate_qualified(name)


def update(table: str, columns: list[str], where: str,
           extra_set: list[str] | None = None) -> str:
    """Build ``UPDATE table SET col=$i ... WHERE ...``.

    ``table`` must be a qualified identifier (schema.table). Dynamic SQL must
    never rely on the connection's session ``search_path``.
    ``columns`` are validated against a strict identifier regex.
    ``extra_set`` contains raw SQL fragments appended after parameterised parts
    (e.g. ``updated_at = NOW()``). Values themselves must still be passed as
    parameters by the caller.
    """
    _validate_table(table)
    parts = []
    for i, col in enumerate(columns, start=2):
        _validate_identifier(col)
        parts.append(f"{col} = ${i}")
    if extra_set:
        parts.extend(extra_set)
    return f"UPDATE {table} SET {', '.join(parts)} WHERE {where}"  # nosec B608


def delete_from(table: str, where: str) -> str:
    """Build ``DELETE FROM table WHERE ...`` with a validated table name."""
    _validate_table(table)
    return f"DELETE FROM {table} WHERE {where}"  # nosec B608


def select_count_from(table: str) -> str:
    """Build ``SELECT COUNT(*) AS cnt FROM table`` with a validated table name."""
    _validate_table(table)
    return f"SELECT COUNT(*) AS cnt FROM {table}"  # nosec B608


def select_from(table: str, select_parts: list[str]) -> str:
    """Build ``SELECT parts FROM table``.

    ``select_parts`` are caller-controlled SQL fragments (e.g. column names or
    expressions). The table name is validated; fragments must be sanitised by
    the caller before passing.
    """
    _validate_table(table)
    return f"SELECT {', '.join(select_parts)} FROM {table}"  # nosec B608


def insert_into(
    table: str,
    columns: list[str],
    placeholders: str,
    extra_values: str,
    on_conflict: str,
) -> str:
    """Build ``INSERT INTO table (...) VALUES (...) ... ON CONFLICT ...``.

    ``columns`` are validated as identifiers. ``placeholders``,
    ``extra_values`` and ``on_conflict`` are caller-controlled SQL fragments
    that must be sanitised by the caller.
    """
    _validate_table(table)
    for col in columns:
        _validate_identifier(col)
    cols = ", ".join(columns)
    return (  # nosec B608
        f"INSERT INTO {table} ({cols}, {extra_values}) "  # nosec B608
        f"VALUES ({placeholders}, {extra_values}) "  # nosec B608
        f"{on_conflict}"  # nosec B608
    )
