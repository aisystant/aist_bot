"""
Трейсинг запросов — request-scoped traces с записью в Neon.

Архитектура:
- Каждый входящий message/callback → Trace (uuid, user_id, command, state)
- Внутри запроса — span("name") добавляет span к текущему trace
- В конце запроса — save_trace() записывает всё в таблицу request_traces
- ContextVar обеспечивает изоляцию между concurrent запросами

Использование:
    from core.tracing import span, start_trace, finish_trace

    # В middleware (автоматически):
    trace = start_trace(user_id=123, command="/mode", state="common.mode_select")
    ...
    await finish_trace(trace)

    # В компонентах:
    async with span("claude.api", max_tokens=2000):
        result = await claude.generate(...)
"""

import asyncio
import json
import time
import uuid
import logging
from contextvars import ContextVar
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List

logger = logging.getLogger(__name__)

# Request-scoped trace context
_current_trace: ContextVar[Optional['Trace']] = ContextVar('_current_trace', default=None)


@dataclass
class Span:
    """Одна операция внутри trace."""
    name: str
    start: float
    end: float = 0.0
    metadata: dict = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        return (self.end - self.start) * 1000


@dataclass
class Trace:
    """Полный trace одного запроса."""
    trace_id: str
    user_id: int
    command: str
    state: str
    spans: List[Span] = field(default_factory=list)
    start: float = field(default_factory=time.perf_counter)

    @property
    def total_ms(self) -> float:
        return (time.perf_counter() - self.start) * 1000


def start_trace(user_id: int, command: str, state: str) -> Trace:
    """Начать новый trace для текущего запроса."""
    trace = Trace(
        trace_id=uuid.uuid4().hex[:12],
        user_id=user_id,
        command=command,
        state=state,
    )
    _current_trace.set(trace)
    return trace


def get_current_trace() -> Optional[Trace]:
    """Получить текущий trace (если есть)."""
    return _current_trace.get()


@asynccontextmanager
async def span(name: str, **metadata):
    """Context manager для замера отдельной операции.

    Добавляет span к текущему trace (если есть).
    Если trace нет — просто логирует время.

    Usage:
        async with span("claude.api", max_tokens=2000):
            result = await claude.generate(...)
    """
    s = Span(name=name, start=time.perf_counter(), metadata=metadata)
    try:
        yield s
    finally:
        s.end = time.perf_counter()
        trace = _current_trace.get()
        if trace:
            trace.spans.append(s)
        if s.duration_ms > 1000:
            logger.info(f"[SPAN] {name}: {s.duration_ms:.0f}ms")
        else:
            logger.debug(f"[SPAN] {name}: {s.duration_ms:.0f}ms")


async def finish_trace(trace: Trace) -> None:
    """Завершить trace — single-write на event-gateway (WP-268 cut-over).

    WP-268 cut-over: legacy INSERT INTO request_traces УДАЛЁН (_save_trace_to_db
    больше не вызывается). Источник истины — event-gateway (request_traced.v1).

    ⚠️ Performance / Latency: legacy был fire-and-forget create_task → ноль
    дополнительной latency на запрос. Cut-over оставляет fire-and-forget
    create_task на gateway (network call), но если gateway медленный —
    create_task'ов накопится много. Не критично для bot-traffic, но мониторить.

    ⚠️ Caller'ы, которые ЧИТАЮТ request_traces (Grafana p50/p95/p99, /traces
    dev-команда) больше не получат свежих traces. Перенести в WP-269 на
    Memory.Observed или Langfuse-only (если есть).
    """
    total = trace.total_ms
    _current_trace.set(None)

    # Логируем summary
    spans_summary = ", ".join(
        f"{s.name}={s.duration_ms:.0f}ms" for s in trace.spans
    )
    logger.info(
        f"[TRACE] {trace.command} | {total:.0f}ms | "
        f"user={trace.user_id} state={trace.state} | {spans_summary}"
    )

    # WP-268 cut-over: единственный writer — event-gateway. Fire-and-forget
    # сохраняем (не блокировать каждый bot-запрос на network call).
    asyncio.create_task(_emit_trace_event(trace))


async def _emit_trace_event(trace: Trace) -> None:
    """Emit request_traced.v1 в event-gateway (single writer, WP-268 cut-over).

    PII: НЕ передаём raw command (может содержать /start ref=email@x.com).
    Передаём только первое слово команды + метрики.
    """
    try:
        from helpers.dual_write import post_event, resolve_ory_id_from_chat

        command_name = (trace.command or "").split()[0] if trace.command else ""

        ory = await resolve_ory_id_from_chat(trace.user_id) if trace.user_id else None
        await post_event(
            source="aist-bot",
            external_id=f"trace-{trace.trace_id}",
            event_type="request_traced",
            schema_version="v1",
            occurred_at=datetime.utcnow(),
            account_id=ory,
            payload={
                "trace_id": trace.trace_id,
                "command": command_name[:50],
                "state": trace.state,
                "total_ms": round(trace.total_ms, 1),
                "spans_count": len(trace.spans),
            },
        )
    except Exception as exc:
        logger.warning(f"[TRACE] emit failed: {exc}")


# WP-268 cut-over: _save_trace_to_db() удалён. legacy request_traces больше не
# заполняется ботом. Caller'ы Grafana / /traces dev-команды должны мигрировать
# на новую БД (см. TODO в finish_trace).
