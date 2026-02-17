"""
Персистентное хранилище FSM состояний в PostgreSQL.

Включает retry для устойчивости к транзиентным ошибкам Neon (cold start, connection reset).
"""

import asyncio
import json
import logging
from typing import Optional

from aiogram.fsm.storage.base import BaseStorage, StorageKey, StateType

from db import get_pool

logger = logging.getLogger(__name__)

_MAX_RETRIES = 2
_RETRY_DELAY = 0.3  # 300ms между попытками


async def _retry_db(operation, description: str):
    """Выполнить DB-операцию с retry (до _MAX_RETRIES попыток)."""
    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return await operation()
        except Exception as e:
            last_error = e
            if attempt < _MAX_RETRIES:
                logger.warning(f"[FSM] {description} failed (attempt {attempt}): {e}, retrying...")
                await asyncio.sleep(_RETRY_DELAY)
            else:
                logger.error(f"[FSM] {description} failed after {_MAX_RETRIES} attempts: {e}")
                raise last_error


class PostgresStorage(BaseStorage):
    """Персистентное хранилище FSM состояний в PostgreSQL"""

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        """Установить состояние"""
        if state is None:
            state_str = None
        elif isinstance(state, str):
            state_str = state
        else:
            state_str = state.state
        logger.info(f"[FSM] set_state: chat_id={key.chat_id}, user_id={key.user_id}, bot_id={key.bot_id}, state={state_str}")

        async def _do():
            async with (await get_pool()).acquire() as conn:
                await conn.execute('''
                    INSERT INTO fsm_states (chat_id, state, updated_at)
                    VALUES ($1, $2, NOW())
                    ON CONFLICT (chat_id) DO UPDATE SET state = $2, updated_at = NOW()
                ''', key.chat_id, state_str)

        await _retry_db(_do, f"set_state(chat_id={key.chat_id})")

    async def get_state(self, key: StorageKey) -> Optional[str]:
        """Получить состояние"""
        async def _do():
            async with (await get_pool()).acquire() as conn:
                row = await conn.fetchrow(
                    'SELECT state FROM fsm_states WHERE chat_id = $1', key.chat_id
                )
                result = row['state'] if row else None
                logger.info(f"[FSM] get_state: chat_id={key.chat_id}, user_id={key.user_id}, bot_id={key.bot_id}, state={result}")
                return result

        return await _retry_db(_do, f"get_state(chat_id={key.chat_id})")

    async def set_data(self, key: StorageKey, data: dict) -> None:
        """Установить данные состояния"""
        data_str = json.dumps(data, ensure_ascii=False)

        async def _do():
            async with (await get_pool()).acquire() as conn:
                await conn.execute('''
                    INSERT INTO fsm_states (chat_id, data, updated_at)
                    VALUES ($1, $2, NOW())
                    ON CONFLICT (chat_id) DO UPDATE SET data = $2, updated_at = NOW()
                ''', key.chat_id, data_str)

        await _retry_db(_do, f"set_data(chat_id={key.chat_id})")

    async def get_data(self, key: StorageKey) -> dict:
        """Получить данные состояния"""
        async def _do():
            async with (await get_pool()).acquire() as conn:
                row = await conn.fetchrow(
                    'SELECT data FROM fsm_states WHERE chat_id = $1', key.chat_id
                )
                if row and row['data']:
                    return json.loads(row['data'])
                return {}

        return await _retry_db(_do, f"get_data(chat_id={key.chat_id})")

    async def close(self) -> None:
        """Закрыть соединение (не требуется, используем общий пул)"""
        pass
