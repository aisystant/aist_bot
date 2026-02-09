"""
Персистентное хранилище FSM состояний в PostgreSQL.
"""

import json
import logging
from typing import Optional

from aiogram.fsm.storage.base import BaseStorage, StorageKey, StateType

from db import get_pool

logger = logging.getLogger(__name__)


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
        async with (await get_pool()).acquire() as conn:
            await conn.execute('''
                INSERT INTO fsm_states (chat_id, state, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (chat_id) DO UPDATE SET state = $2, updated_at = NOW()
            ''', key.chat_id, state_str)

    async def get_state(self, key: StorageKey) -> Optional[str]:
        """Получить состояние"""
        async with (await get_pool()).acquire() as conn:
            row = await conn.fetchrow(
                'SELECT state FROM fsm_states WHERE chat_id = $1', key.chat_id
            )
            result = row['state'] if row else None
            logger.info(f"[FSM] get_state: chat_id={key.chat_id}, user_id={key.user_id}, bot_id={key.bot_id}, state={result}")
            return result

    async def set_data(self, key: StorageKey, data: dict) -> None:
        """Установить данные состояния"""
        data_str = json.dumps(data, ensure_ascii=False)
        async with (await get_pool()).acquire() as conn:
            await conn.execute('''
                INSERT INTO fsm_states (chat_id, data, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (chat_id) DO UPDATE SET data = $2, updated_at = NOW()
            ''', key.chat_id, data_str)

    async def get_data(self, key: StorageKey) -> dict:
        """Получить данные состояния"""
        async with (await get_pool()).acquire() as conn:
            row = await conn.fetchrow(
                'SELECT data FROM fsm_states WHERE chat_id = $1', key.chat_id
            )
            if row and row['data']:
                return json.loads(row['data'])
            return {}

    async def close(self) -> None:
        """Закрыть соединение (не требуется, используем общий пул)"""
        pass
