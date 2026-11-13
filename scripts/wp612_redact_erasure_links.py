"""Разовая чистка старых связок «человек → удаление данных» (WP-612).

Без флага только считает. С --apply: по каждой базе резервная копия затронутых
строк, правка одной транзакцией, проверка, что связок не осталось, а число
строк и чужие команды не изменились.

Три базы, три транзакции: наблюдаемость (трассировки, сессии, журнал ошибок),
журнал бота, центральный журнал. Langfuse не трогаем.

Одно и то же имя public.wp612_erasure_link_backup пишется в каждую базу, где
создание таблицы разрешено. В таблице остаётся номер. Пилот снимает копию в
каждой базе после того, как примет результат.

Запуск из корня репозитория бота:
    python -m scripts.wp612_redact_erasure_links
    python -m scripts.wp612_redact_erasure_links --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.erasure_trace import (  # noqa: E402
    REDACTED_POINT,
    bot_count_sql,
    erasure_command_list,
    fill_sql,
    health_count_sql,
    learning_count_sql,
    sensitive_match_sql,
)

_URL_RE = re.compile(r"postgres(?:ql)?://\S+")


def _load_local_env() -> None:
    path = Path(__file__).resolve().parents[1] / ".env"
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _safe(exc: BaseException) -> str:
    text = _URL_RE.sub("postgresql://…", f"{type(exc).__name__}: {exc}")
    return text[:500]


def _commands() -> list[str]:
    return erasure_command_list()


async def _fetch_groups(conn, sql: str) -> list[tuple[str, int]]:
    args = (_commands(),) if "$1" in sql else ()
    rows = await conn.fetch(sql, *args)
    if rows and "command" not in rows[0].keys():
        return [("(все)", int(rows[0]["rows"]))]
    return [(str(row["command"]), int(row["rows"])) for row in rows]


async def _print_counts(title: str, conn, queries: dict[str, str]) -> dict[str, list[tuple[str, int]]]:
    print(f"\n{title}")
    found: dict[str, list[tuple[str, int]]] = {}
    for name, sql in queries.items():
        groups = await _fetch_groups(conn, sql)
        found[name] = groups
        if not groups:
            print(f"  {name}: 0")
            continue
        for command, rows in groups:
            print(f"  {name}: {command} — {rows}")
    return found


def _total(found: dict[str, list[tuple[str, int]]], skip: set[str] | None = None) -> int:
    skip = skip or set()
    return sum(rows for name, groups in found.items() if name not in skip for _, rows in groups)


async def _control(conn, sql: str) -> dict[str, int]:
    args = (_commands(),) if "$1" in sql else ()
    row = await conn.fetchrow(sql, *args)
    return {key: int(row[key]) for key in row.keys()}


def _health_control_sql() -> str:
    return fill_sql(
        """
        SELECT
          (SELECT count(*)::int FROM public.request_traces) AS request_traces_rows,
          (SELECT count(*)::int FROM public.user_sessions) AS user_sessions_rows,
          (SELECT count(*)::int FROM public.error_logs) AS error_logs_rows,
          (SELECT count(*)::int FROM public.request_traces WHERE command = '/start') AS start_traces,
          (
            SELECT count(*)::int
            FROM public.user_sessions AS s,
                 jsonb_array_elements_text(s.commands) AS elem
            WHERE NOT %%MATCH%%
          ) AS other_session_commands
        """,
        MATCH=sensitive_match_sql("elem"),
    )


def _bot_control_sql() -> str:
    return """
        SELECT
          (SELECT count(*)::int FROM development.user_events) AS user_events_rows,
          (
            SELECT count(*)::int FROM development.user_events
            WHERE event_type = 'session_start' AND payload->>'entry_point' = '/start'
          ) AS start_sessions
    """


def _learning_control_sql() -> str:
    return """
        SELECT
          (SELECT count(*)::int FROM public.domain_event) AS domain_event_rows,
          (
            SELECT count(*)::int FROM public.domain_event
            WHERE event_type = 'request_traced'
              AND account_id IS NOT NULL
              AND payload->>'command' = '/start'
          ) AS start_traced
    """


def _write_local_backup(store: str, rows) -> None:
    """Файл остаётся у пилота: в нём ещё есть номера, в репозиторий он не входит."""
    # Путь фиксированный: эти два файла пилот снимет после повторного нуля.
    path = Path(f"/tmp/wp612-erasure-{store}-backup.json")  # nosec B108
    payload = []
    for row in rows:
        snapshot = row["snapshot"]
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        payload.append({
            "store": row["store"],
            "row_key": row["row_key"],
            "snapshot": snapshot,
        })
    path.write_text(json.dumps(payload, ensure_ascii=False, default=str))
    path.chmod(0o600)
    print(f"  резервная копия: {path} ({len(payload)} строк)")


async def _backup_table_allowed(conn) -> bool:
    """CREATE в public может быть закрыт. Ошибка внутри savepoint не отменяет транзакцию."""
    try:
        async with conn.transaction():
            await _ensure_backup(conn)
    except asyncpg.InsufficientPrivilegeError:
        return False
    return True


async def _ensure_backup(conn) -> None:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS public.wp612_erasure_link_backup (
            store TEXT NOT NULL,
            row_key TEXT NOT NULL,
            snapshot JSONB NOT NULL,
            backed_up_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (store, row_key)
        )
    """)


async def _apply_health(conn) -> None:
    match_command = sensitive_match_sql("command")
    match_entry = sensitive_match_sql("entry_point")
    match_exit = sensitive_match_sql("exit_point")
    match_elem = sensitive_match_sql("elem")
    match_ctx = sensitive_match_sql("context->>'command'")
    commands = _commands()
    before = await _control(conn, _health_control_sql())
    use_table = await _backup_table_allowed(conn)
    if not use_table:
        rows = await conn.fetch(
            fill_sql(
                """
                SELECT 'request_traces' AS store, trace_id::text AS row_key, to_jsonb(t) AS snapshot
                FROM public.request_traces AS t
                WHERE %%COMMAND%% AND user_id IS DISTINCT FROM 0
                UNION ALL
                SELECT 'user_sessions', id::text, to_jsonb(s)
                FROM public.user_sessions AS s
                WHERE EXISTS (
                        SELECT 1 FROM jsonb_array_elements_text(s.commands) AS elem
                        WHERE %%ELEM%%
                      )
                   OR %%ENTRY%%
                   OR %%EXIT%%
                UNION ALL
                SELECT 'error_logs', id::text, to_jsonb(e)
                FROM public.error_logs AS e
                WHERE jsonb_typeof(context) = 'object'
                  AND %%CTX%%
                  AND COALESCE(context->>'user_id', '0') <> '0'
                """,
                COMMAND=match_command,
                ELEM=match_elem,
                ENTRY=match_entry,
                EXIT=match_exit,
                CTX=match_ctx,
            ),
            commands,
        )
        _write_local_backup("health", rows)
    if use_table:
        await conn.execute(
            fill_sql(
                """
                INSERT INTO public.wp612_erasure_link_backup (store, row_key, snapshot)
                SELECT 'request_traces', trace_id::text, to_jsonb(t)
                FROM public.request_traces AS t
                WHERE %%COMMAND%% AND user_id IS DISTINCT FROM 0
                ON CONFLICT (store, row_key) DO NOTHING
                """,
                COMMAND=match_command,
            ),
            commands,
        )
        await conn.execute(
            fill_sql(
                """
                INSERT INTO public.wp612_erasure_link_backup (store, row_key, snapshot)
                SELECT 'user_sessions', id::text, to_jsonb(s)
                FROM public.user_sessions AS s
                WHERE EXISTS (
                        SELECT 1 FROM jsonb_array_elements_text(s.commands) AS elem
                        WHERE %%ELEM%%
                      )
                   OR %%ENTRY%%
                   OR %%EXIT%%
                ON CONFLICT (store, row_key) DO NOTHING
                """,
                ELEM=match_elem,
                ENTRY=match_entry,
                EXIT=match_exit,
            ),
            commands,
        )
        await conn.execute(
            fill_sql(
                """
                INSERT INTO public.wp612_erasure_link_backup (store, row_key, snapshot)
                SELECT 'error_logs', id::text, to_jsonb(e)
                FROM public.error_logs AS e
                WHERE jsonb_typeof(context) = 'object'
                  AND %%CTX%%
                  AND COALESCE(context->>'user_id', '0') <> '0'
                ON CONFLICT (store, row_key) DO NOTHING
                """,
                CTX=match_ctx,
            ),
            commands,
        )
    trace_rows = await conn.fetchval(
        fill_sql(
            """
            SELECT count(*)::int FROM public.request_traces
            WHERE %%COMMAND%% AND user_id IS DISTINCT FROM 0
            """,
            COMMAND=match_command,
        ),
        commands,
    )
    if trace_rows:
        await conn.execute(
            fill_sql(
                """
                UPDATE public.request_traces
                SET user_id = 0
                WHERE %%COMMAND%% AND user_id IS DISTINCT FROM 0
                """,
                COMMAND=match_command,
            ),
            commands,
        )
    await conn.execute(
        fill_sql(
            """
            UPDATE public.user_sessions AS s
            SET commands = (
                    SELECT COALESCE(jsonb_agg(to_jsonb(elem) ORDER BY ordinality), '[]'::jsonb)
                    FROM jsonb_array_elements_text(s.commands) WITH ORDINALITY AS listed(elem, ordinality)
                    WHERE NOT %%ELEM%%
                ),
                entry_point = CASE WHEN %%ENTRY%% THEN $2 ELSE entry_point END,
                exit_point = CASE WHEN %%EXIT%% THEN $2 ELSE exit_point END
            WHERE EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(s.commands) AS elem
                    WHERE %%ELEM%%
                  )
               OR %%ENTRY%%
               OR %%EXIT%%
            """,
            ELEM=match_elem,
            ENTRY=match_entry,
            EXIT=match_exit,
        ),
        commands,
        REDACTED_POINT,
    )
    error_rows = await conn.fetchval(
        fill_sql(
            """
            SELECT count(*)::int FROM public.error_logs
            WHERE jsonb_typeof(context) = 'object'
              AND %%CTX%%
              AND COALESCE(context->>'user_id', '0') <> '0'
            """,
            CTX=match_ctx,
        ),
        commands,
    )
    if error_rows:
        await conn.execute(
            fill_sql(
                """
                UPDATE public.error_logs
                SET context = jsonb_set(context, '{user_id}', '0'::jsonb, true)
                WHERE jsonb_typeof(context) = 'object'
                  AND %%CTX%%
                  AND COALESCE(context->>'user_id', '0') <> '0'
                """,
                CTX=match_ctx,
            ),
            commands,
        )
    left = await _print_counts("проверка наблюдаемости", conn, health_count_sql())
    after = await _control(conn, _health_control_sql())
    if _total(left) != 0 or after != before:
        raise RuntimeError(
            f"health verify failed: remaining={_total(left)} before={before} after={after}"
        )


async def _apply_bot(conn) -> None:
    match = sensitive_match_sql("payload->>'entry_point'")
    commands = _commands()
    before = await _control(conn, _bot_control_sql())
    await _ensure_backup(conn)
    await conn.execute(
        fill_sql(
            """
            INSERT INTO public.wp612_erasure_link_backup (store, row_key, snapshot)
            SELECT 'user_events', id::text, to_jsonb(e)
            FROM development.user_events AS e
            WHERE event_type = 'session_start' AND %%MATCH%%
            ON CONFLICT (store, row_key) DO NOTHING
            """,
            MATCH=match,
        ),
        commands,
    )
    await conn.execute(
        fill_sql(
            """
            UPDATE development.user_events
            SET payload = jsonb_set(payload, '{entry_point}', to_jsonb($2::text), true)
            WHERE event_type = 'session_start' AND %%MATCH%%
            """,
            MATCH=match,
        ),
        commands,
        REDACTED_POINT,
    )
    left = await _print_counts("проверка журнала бота", conn, bot_count_sql())
    after = await _control(conn, _bot_control_sql())
    if _total(left) != 0 or after != before:
        raise RuntimeError(
            f"bot verify failed: remaining={_total(left)} before={before} after={after}"
        )


async def _learning_can_write(conn) -> bool:
    row = await conn.fetchrow("""
        SELECT has_table_privilege(current_user, 'public.domain_event', 'UPDATE') AS can_update,
               has_table_privilege(current_user, 'public.domain_event_admin_audit', 'INSERT') AS can_audit
    """)
    return bool(row["can_update"] and row["can_audit"])


async def _apply_learning(conn) -> None:
    if not await _learning_can_write(conn):
        raise PermissionError(
            "нет права править domain_event. Нужно подключение владельца таблицы, "
            "не роль записи событий."
        )
    mentions = await conn.fetchval(
        learning_count_sql()["domain_event.session_start_mentions"]
    )
    if mentions:
        raise RuntimeError(
            f"session_start в центральном журнале содержит команду удаления: {mentions}. "
            "Правка payload сломает цепочку хэшей, чистка журнала остановлена."
        )
    match = sensitive_match_sql("payload->>'command'")
    commands = _commands()
    before = await _control(conn, _learning_control_sql())
    hmac_row = await conn.fetchval("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'domain_event'
          AND column_name = 'account_hmac'
    """)
    clear_hmac = ", account_hmac = NULL" if hmac_row else ""
    await _ensure_backup(conn)
    await conn.execute(
        fill_sql(
            """
            INSERT INTO public.wp612_erasure_link_backup (store, row_key, snapshot)
            SELECT 'domain_event', id::text, to_jsonb(d)
            FROM public.domain_event AS d
            WHERE event_type = 'request_traced'
              AND account_id IS NOT NULL
              AND %%MATCH%%
            ON CONFLICT (store, row_key) DO NOTHING
            """,
            MATCH=match,
        ),
        commands,
    )
    planned = await conn.fetchval(
        fill_sql(
            """
            SELECT count(*)::int FROM public.domain_event
            WHERE event_type = 'request_traced'
              AND account_id IS NOT NULL
              AND %%MATCH%%
            """,
            MATCH=match,
        ),
        commands,
    )
    await conn.execute(
        """
        INSERT INTO public.domain_event_admin_audit (via_function, target, reason)
        VALUES (
            'forget_account',
            jsonb_build_object(
                'scope', 'wp612-request-traced-erasure',
                'rows', $1::int,
                'note', 'account_id cleared on erasure commands only; payload kept'
            ),
            'WP-612: clear account_id for erasure commands; event type stays request_traced'
        )
        """,
        planned,
    )
    await conn.execute("SELECT set_config('iwe.domain_event_admin', 'forget_account', true)")
    await conn.execute(
        fill_sql(
            """
            UPDATE public.domain_event
            SET account_id = NULL%%HMAC%%
            WHERE event_type = 'request_traced'
              AND account_id IS NOT NULL
              AND %%MATCH%%
            """,
            HMAC=clear_hmac,
            MATCH=match,
        ),
        commands,
    )
    await conn.execute("SELECT set_config('iwe.domain_event_admin', '', true)")
    left = await _print_counts(
        "проверка центрального журнала",
        conn,
        {"domain_event.request_traced": learning_count_sql()["domain_event.request_traced"]},
    )
    after = await _control(conn, _learning_control_sql())
    if _total(left) != 0 or after != before:
        raise RuntimeError(
            f"learning verify failed: remaining={_total(left)} before={before} after={after}"
        )


async def _connect(url: str):
    return await asyncpg.connect(url, statement_cache_size=0)


async def _run_store(name: str, url: str, counts: dict[str, str], apply, do_apply: bool) -> str:
    """Вернуть ok, empty, failed или blocked."""
    if not url:
        print(f"\n{name}: адрес базы не задан, пропуск")
        return "failed"
    try:
        conn = await _connect(url)
    except Exception as exc:
        print(f"\n{name}: нет соединения ({_safe(exc)})")
        return "failed"
    try:
        found = await _print_counts(name, conn, counts)
        pending = _total(found, skip={"domain_event.session_start_mentions"})
        mentions = found.get("domain_event.session_start_mentions")
        if mentions and mentions[0][1]:
            print(
                "  session_start в центральном журнале упоминает команду удаления. "
                "Эти строки чистка не меняет."
            )
        if not do_apply:
            return "ok"
        if pending == 0 and not (mentions and mentions[0][1]):
            print("  чистить нечего")
            return "empty"
        async with conn.transaction():
            await apply(conn)
        print(f"  {name}: записано")
        return "ok"
    except PermissionError as exc:
        print(f"  {name}: остановлено ({exc})")
        return "blocked"
    except Exception as exc:
        print(f"  {name}: откат ({_safe(exc)})")
        return "failed"
    finally:
        await conn.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description="WP-612: подсчёт и чистка связок удаления данных")
    parser.add_argument("--apply", action="store_true", help="после подсчёта записать правку")
    args = parser.parse_args()
    _load_local_env()
    health = os.environ.get("HEALTH_URL", "")
    bot = os.environ.get("DATABASE_URL", "")
    learning = os.environ.get("LEARNING_URL", "")
    results = []
    results.append(await _run_store(
        "Наблюдаемость (HEALTH_URL)", health, health_count_sql(), _apply_health, args.apply,
    ))
    results.append(await _run_store(
        "Журнал бота (DATABASE_URL)", bot, bot_count_sql(), _apply_bot, args.apply,
    ))
    results.append(await _run_store(
        "Центральный журнал (LEARNING_URL)", learning, learning_count_sql(), _apply_learning, args.apply,
    ))
    if any(item in ("failed", "blocked") for item in results):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
