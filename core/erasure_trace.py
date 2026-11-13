"""Команды удаления данных Аналитика чата не хранят номер человека.

WP-612. Две команды и четыре кнопки подтверждения: время, имя действия и
результат остаются, номер человека в эти записи не попадает. Обычные команды
и отметка «был активен сегодня» не меняются.

Ноль в колонке user_id — не человек. У Telegram нет пользователя с номером 0.
"""

from __future__ import annotations

import re

# Совпадают с handlers/chat_analyst.py (ERASE_* / RESUME_*). Префикс cb: —
# это форма, которую TracingMiddleware кладёт в журнал.
ERASURE_TRACE_COMMANDS = frozenset({
    "/erase_me",
    "/resume_me",
    "cb:chat_analyst:erase:confirm",
    "cb:chat_analyst:erase:cancel",
    "cb:chat_analyst:resume:confirm",
    "cb:chat_analyst:resume:cancel",
})

# Числовая колонка не принимает пусто. 0 не является номером Telegram.
OMITTED_USER_ID = 0

# В текстовых полях сессии вместо названия действия.
REDACTED_POINT = "скрыто"

_COMMAND_EXPR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(->>'[A-Za-z0-9_]+')?$")


def canonical_trace_command(command: str) -> str:
    """Первое слово команды. У /команды@бот отбрасывается имя бота."""
    token = (command or "").split()[0] if command else ""
    if token.startswith("/") and "@" in token:
        token = token.split("@", 1)[0]
    return token


def is_erasure_trace_command(command: str) -> bool:
    return canonical_trace_command(command) in ERASURE_TRACE_COMMANDS


def redact_command_list(commands: list[str]) -> list[str]:
    """Убрать шесть действий из списка команд сессии, остальное сохранить."""
    return [item for item in commands if not is_erasure_trace_command(item)]


def redact_entry_point(value: str | None) -> str | None:
    if value is None:
        return None
    if is_erasure_trace_command(value):
        return REDACTED_POINT
    return value


def erasure_command_list() -> list[str]:
    return sorted(ERASURE_TRACE_COMMANDS)


def health_count_sql() -> dict[str, str]:
    """Подсчёт оставшихся связок в базе наблюдаемости. Ключ — место, значение — SQL."""
    return {
        "request_traces": f"""
            SELECT command, count(*)::int AS rows
            FROM public.request_traces
            WHERE {sensitive_match_sql("command")}
              AND user_id IS DISTINCT FROM {OMITTED_USER_ID}
            GROUP BY command
            ORDER BY command
        """,
        "user_sessions.commands": f"""
            SELECT elem AS command, count(*)::int AS rows
            FROM public.user_sessions AS s,
                 jsonb_array_elements_text(s.commands) AS elem
            WHERE {sensitive_match_sql("elem")}
            GROUP BY elem
            ORDER BY elem
        """,
        "user_sessions.entry_point": f"""
            SELECT entry_point AS command, count(*)::int AS rows
            FROM public.user_sessions
            WHERE {sensitive_match_sql("entry_point")}
            GROUP BY entry_point
            ORDER BY entry_point
        """,
        "user_sessions.exit_point": f"""
            SELECT exit_point AS command, count(*)::int AS rows
            FROM public.user_sessions
            WHERE {sensitive_match_sql("exit_point")}
            GROUP BY exit_point
            ORDER BY exit_point
        """,
        "error_logs": f"""
            SELECT context->>'command' AS command, count(*)::int AS rows
            FROM public.error_logs
            WHERE jsonb_typeof(context) = 'object'
              AND {sensitive_match_sql("context->>'command'")}
              AND COALESCE(context->>'user_id', '0') <> '0'
            GROUP BY 1
            ORDER BY 1
        """,
    }


def bot_count_sql() -> dict[str, str]:
    """Журнал бота: событие начала сессии хранит название команды."""
    return {
        "user_events.session_start": f"""
            SELECT payload->>'entry_point' AS command, count(*)::int AS rows
            FROM development.user_events
            WHERE event_type = 'session_start'
              AND {sensitive_match_sql("payload->>'entry_point'")}
            GROUP BY 1
            ORDER BY 1
        """,
    }


def learning_count_sql() -> dict[str, str]:
    """Центральный журнал. session_start здесь название команды удаления не несёт.

    Запрос session_start_mentions только проверяет это. Ненулевое число
    останавливает чистку журнала: правка payload ломает цепочку хэшей.
    """
    return {
        "domain_event.request_traced": f"""
            SELECT payload->>'command' AS command, count(*)::int AS rows
            FROM public.domain_event
            WHERE event_type = 'request_traced'
              AND account_id IS NOT NULL
              AND {sensitive_match_sql("payload->>'command'")}
            GROUP BY 1
            ORDER BY 1
        """,
        "domain_event.session_start_mentions": """
            SELECT count(*)::int AS rows
            FROM public.domain_event
            WHERE event_type = 'session_start'
              AND (
                    payload::text LIKE '%/erase_me%'
                 OR payload::text LIKE '%/resume_me%'
                 OR payload::text LIKE '%chat_analyst:erase%'
                 OR payload::text LIKE '%chat_analyst:resume%'
              )
        """,
    }


def sensitive_match_sql(expression: str) -> str:
    """Условие «это одно из шести действий». expression — доверенный SQL-скаляр.

    Список команд передаётся параметром $1, не склейкой в текст запроса.
    Суффикс @бот покрывает старые строки, куда команда попала с именем бота.
    """
    if not _COMMAND_EXPR.fullmatch(expression):
        raise ValueError(f"unsafe SQL expression: {expression!r}")
    return (
        f"({expression} = ANY($1::text[]) "
        f"OR {expression} LIKE '/erase_me@%' "
        f"OR {expression} LIKE '/resume_me@%')"
    )
