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
    """Завершить trace и записать в Neon."""
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

    # Записываем в БД (true fire-and-forget: не ждём DB write)
    asyncio.create_task(_save_trace_to_db(trace))

    # WP-268 Phase 2 Phase B: dual-write request_traced.v1.
    # Параллельно с legacy записью в request_traces. Lazy import helpers/db
    # чтобы не создавать circular dependency на module-load (tracing
    # импортируется очень рано, db.queries.identity — позже).
    asyncio.create_task(_dual_write_trace(trace))


async def _dual_write_trace(trace: Trace) -> None:
    """Dual-write trace в event-gateway. Fire-and-forget, не блокирует.

    PII: НЕ передаём raw command (может содержать /linked? хвост с PII или
    callback_data). Передаём только первое слово (сама команда) + метрики.
    """
    try:
        from helpers.dual_write import post_event, resolve_ory_id_from_chat

        # Извлекаем имя команды (первое слово), не весь текст.
        # `trace.command` уже обрезан до 100 символов в _save_trace_to_db,
        # но raw полный — здесь дополнительно убираем потенциальный
        # PII-хвост (например `/start ref=email@x.com`).
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
                "command": command_name[:50],  # safety bound
                "state": trace.state,
                "total_ms": round(trace.total_ms, 1),
                "spans_count": len(trace.spans),
            },
        )
    except Exception as exc:
        logger.warning(f"[TRACE] dual-write failed: {exc}")


async def _save_trace_to_db(trace: Trace) -> None:
    """Записать trace в health.request_traces (fire-and-forget task).

    WP-253 G4 health migration: writer переключён с bot_data на Neon health БД.
    """
    try:
        from db.connection import get_health_pool

        spans_json = json.dumps([
            {"name": s.name, "duration_ms": round(s.duration_ms, 1), **s.metadata}
            for s in trace.spans
        ])

        pool = await get_health_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO request_traces
                   (trace_id, user_id, command, state, total_ms, spans, created_at)
                   VALUES ($1, $2, $3, $4, $5, $6::jsonb, NOW())""",
                trace.trace_id,
                trace.user_id,
                trace.command[:100],
                trace.state,
                round(trace.total_ms, 1),
                spans_json,
            )
    except Exception as e:
        logger.warning(f"[TRACE] Failed to save trace: {e}")
