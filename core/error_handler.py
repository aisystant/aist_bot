"""
Async logging handler: captures ERROR+ logs → Neon DB (error_logs table).

Architecture:
- AsyncDBLogHandler extends logging.Handler
- Buffers LogRecords in asyncio.Queue (thread-safe)
- Background asyncio task flushes buffer to error_logs every 5 sec
- Deduplicates by error_key (hash of logger + traceback first line)
- Enriches with request context from core.tracing (user_id, command, etc.)

CRITICAL: Internal errors go to sys.stderr ONLY (never logging → infinite loop).

Usage:
    from core.error_handler import setup_error_handler, shutdown_error_handler
    await setup_error_handler()   # after init_db()
    ...
    await shutdown_error_handler()  # before shutdown
"""

import sys
import json
import asyncio
import hashlib
import logging
import traceback as tb_module
from datetime import datetime, timezone
from typing import Optional

_handler_instance: Optional['AsyncDBLogHandler'] = None


class AsyncDBLogHandler(logging.Handler):
    """Logging handler that writes ERROR+ to Neon via asyncpg."""

    def __init__(self, flush_interval: float = 5.0, max_queue: int = 1000):
        super().__init__(level=logging.ERROR)
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue)
        self._flush_interval = flush_interval
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def start(self):
        self._loop = asyncio.get_event_loop()
        self._task = asyncio.create_task(self._flush_loop())

    def emit(self, record: logging.LogRecord):
        """Non-blocking: extract error info → queue."""
        try:
            # Format traceback
            tb_text = None
            if record.exc_info and record.exc_info[2]:
                tb_text = ''.join(tb_module.format_exception(*record.exc_info))

            # Error key for deduplication
            error_key = self._make_error_key(record, tb_text)

            # Request context (if inside traced request)
            context = self._get_request_context()

            item = {
                'error_key': error_key,
                'level': record.levelname,
                'logger_name': record.name,
                'message': record.getMessage()[:2000],
                'traceback': tb_text[:4000] if tb_text else None,
                'context': context,
                'timestamp': datetime.now(timezone.utc),
            }

            # Non-blocking put
            if self._loop and self._loop.is_running():
                self._queue.put_nowait(item)
            # else: loop not running yet, skip
        except asyncio.QueueFull:
            print(f"[ErrorHandler] Queue full, dropping error: {record.getMessage()[:100]}", file=sys.stderr)
        except Exception as e:
            print(f"[ErrorHandler] emit() failed: {e}", file=sys.stderr)

    def _make_error_key(self, record: logging.LogRecord, tb_text: Optional[str]) -> str:
        """SHA-256 hash of logger + first traceback line (or message)."""
        if tb_text:
            lines = tb_text.strip().splitlines()
            # Last line usually has the exception type + message
            key_source = f"{record.name}:{lines[-1] if lines else ''}"
        else:
            key_source = f"{record.name}:{record.getMessage()[:200]}"
        return hashlib.sha256(key_source.encode()).hexdigest()[:16]

    def _get_request_context(self) -> dict:
        """Extract current request context from tracing ContextVar."""
        try:
            from core.tracing import get_current_trace
            trace = get_current_trace()
            if trace:
                return {
                    'trace_id': trace.trace_id,
                    'user_id': trace.user_id,
                    'command': trace.command,
                    'state': trace.state,
                }
        except Exception:
            pass
        return {}

    async def _flush_loop(self):
        """Background task: drain queue every flush_interval seconds."""
        while True:
            try:
                await asyncio.sleep(self._flush_interval)
                await self.flush()
            except asyncio.CancelledError:
                # Final flush on shutdown
                await self.flush()
                return
            except Exception as e:
                print(f"[ErrorHandler] flush_loop error: {e}", file=sys.stderr)

    async def flush(self):
        """Drain queue and batch-upsert to error_logs."""
        items = []
        while not self._queue.empty():
            try:
                items.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not items:
            return

        # Group by error_key for dedup
        grouped: dict = {}
        for item in items:
            key = item['error_key']
            if key in grouped:
                grouped[key]['occurrence_count'] += 1
                grouped[key]['timestamp'] = item['timestamp']
            else:
                item['occurrence_count'] = 1
                grouped[key] = item

        try:
            from db.connection import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                for item in grouped.values():
                    ctx_json = json.dumps(item['context'])
                    # Try update existing recent row first
                    updated = await conn.execute('''
                        UPDATE error_logs
                        SET occurrence_count = occurrence_count + $2,
                            last_seen_at = $3,
                            message = $4
                        WHERE error_key = $1
                          AND last_seen_at > NOW() - INTERVAL '1 hour'
                    ''', item['error_key'], item['occurrence_count'],
                        item['timestamp'], item['message'])

                    # If no row updated → insert new
                    if updated == 'UPDATE 0':
                        await conn.execute('''
                            INSERT INTO error_logs
                            (error_key, level, logger_name, message, traceback,
                             context, occurrence_count, first_seen_at, last_seen_at)
                            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $8)
                        ''', item['error_key'], item['level'], item['logger_name'],
                            item['message'], item['traceback'], ctx_json,
                            item['occurrence_count'], item['timestamp'])
        except Exception as e:
            print(f"[ErrorHandler] DB flush failed: {e}", file=sys.stderr)


async def setup_error_handler():
    """Register async DB error handler on root logger. Call after init_db()."""
    global _handler_instance
    handler = AsyncDBLogHandler(flush_interval=5.0, max_queue=1000)
    logging.getLogger().addHandler(handler)
    handler.start()
    _handler_instance = handler
    logging.getLogger(__name__).info("[ErrorHandler] Error monitoring initialized")


async def shutdown_error_handler():
    """Flush remaining errors before shutdown."""
    if _handler_instance and _handler_instance._task:
        _handler_instance._task.cancel()
        try:
            await _handler_instance._task
        except asyncio.CancelledError:
            pass
