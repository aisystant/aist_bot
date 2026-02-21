"""
L1 Наладчик: автоматическое восстановление застрявших пользователей.

Два механизма обнаружения:
1. Repeated errors — 3+ ошибки для одного user_id за 5 минут
2. Stuck detection — пользователь в одном state >30 мин без новой активности

Действие: reset FSM → common.mode_select + извинение пользователю.

Вызывается из scheduler каждые 5 минут.
"""

import os
import logging
from datetime import datetime, timezone

from db.connection import get_pool

logger = logging.getLogger(__name__)

# Состояния, в которых пользователь считается "в безопасности" (не stuck)
SAFE_STATES = {
    'common.mode_select',
    'common.start',
    None,
    'unknown',
}

RECOVERY_TARGET_STATE = 'common.mode_select'
ERROR_THRESHOLD = 3
ERROR_WINDOW_MINUTES = 5
STUCK_TIMEOUT_MINUTES = 60  # Консервативно: 60 мин (не 30) чтобы не мешать длинным сессиям


async def detect_repeated_errors(minutes: int = ERROR_WINDOW_MINUTES,
                                  threshold: int = ERROR_THRESHOLD) -> list[dict]:
    """Найти пользователей с threshold+ ошибками за последние minutes минут.

    Returns:
        [{user_id, error_count, last_error}]
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT
                context->>'user_id' as user_id,
                COUNT(*) as error_count,
                MAX(message) as last_error
            FROM error_logs
            WHERE last_seen_at > NOW() - ($1 || ' minutes')::INTERVAL
              AND context->>'user_id' IS NOT NULL
              AND context->>'user_id' != '0'
            GROUP BY context->>'user_id'
            HAVING COUNT(*) >= $2
        ''', str(minutes), threshold)

        return [
            {
                'user_id': int(r['user_id']),
                'error_count': r['error_count'],
                'last_error': r['last_error'][:100] if r['last_error'] else '',
            }
            for r in rows
            if r['user_id'] and r['user_id'].isdigit()
        ]


async def detect_stuck_users(timeout_minutes: int = STUCK_TIMEOUT_MINUTES) -> list[dict]:
    """Найти пользователей, застрявших в одном state без активности.

    Логика: последний trace > timeout_minutes назад И state не в SAFE_STATES.
    Только пользователи, которые были активны сегодня (не уснувшие вчера).

    Returns:
        [{user_id, state, last_activity}]
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            WITH latest_traces AS (
                SELECT DISTINCT ON (user_id)
                    user_id, state, created_at
                FROM request_traces
                WHERE created_at > NOW() - INTERVAL '24 hours'
                ORDER BY user_id, created_at DESC
            )
            SELECT user_id, state, created_at as last_activity
            FROM latest_traces
            WHERE created_at < NOW() - ($1 || ' minutes')::INTERVAL
              AND created_at > NOW() - INTERVAL '4 hours'
              AND state NOT IN ('common.mode_select', 'common.start', 'unknown')
        ''', str(timeout_minutes))

        return [
            {
                'user_id': r['user_id'],
                'state': r['state'],
                'last_activity': r['last_activity'],
            }
            for r in rows
        ]


async def recover_user(chat_id: int, reason: str):
    """Сбросить FSM → RECOVERY_TARGET_STATE + отправить извинение.

    Args:
        chat_id: ID чата пользователя
        reason: Причина восстановления (для лога)
    """
    from aiogram import Bot
    from db.queries.users import get_intern, update_intern
    from i18n import t

    intern = await get_intern(chat_id)
    if not intern:
        return

    lang = intern.get('language', 'ru') or 'ru'
    current_state = intern.get('current_state', '')

    # Не восстанавливать если уже в safe state
    if current_state in SAFE_STATES:
        return

    # Reset state в БД
    await update_intern(chat_id, current_state=RECOVERY_TARGET_STATE)

    # Отправить извинение
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if bot_token:
        bot = Bot(token=bot_token)
        try:
            await bot.send_message(
                chat_id,
                t('errors.auto_recovery', lang),
            )
        except Exception as e:
            error_msg = str(e).lower()
            if 'blocked' not in error_msg and 'deactivated' not in error_msg:
                logger.warning(f"[Unstick] Failed to send recovery message to {chat_id}: {e}")
        finally:
            await bot.session.close()

    logger.warning(f"[Unstick] Recovered user {chat_id} from '{current_state}': {reason}")


# Отслеживание уже восстановленных пользователей (предотвращение спама)
_recently_recovered: dict[int, datetime] = {}
_RECOVERY_COOLDOWN_MINUTES = 30


async def check_and_recover_users():
    """Основная функция: detect + recover. Вызывается из scheduler каждые 5 мин."""
    now = datetime.now(timezone.utc)

    # Очистить старые записи из cooldown
    expired = [uid for uid, ts in _recently_recovered.items()
               if (now - ts).total_seconds() > _RECOVERY_COOLDOWN_MINUTES * 60]
    for uid in expired:
        del _recently_recovered[uid]

    recovered_count = 0

    # 1. Repeated errors
    try:
        error_users = await detect_repeated_errors()
        for user in error_users:
            uid = user['user_id']
            if uid in _recently_recovered:
                continue
            await recover_user(uid, f"repeated_errors ({user['error_count']}x): {user['last_error']}")
            _recently_recovered[uid] = now
            recovered_count += 1
    except Exception as e:
        logger.error(f"[Unstick] Error detection failed: {e}")

    # 2. Stuck users
    try:
        stuck_users = await detect_stuck_users()
        for user in stuck_users:
            uid = user['user_id']
            if uid in _recently_recovered:
                continue
            await recover_user(uid, f"stuck in '{user['state']}' since {user['last_activity']}")
            _recently_recovered[uid] = now
            recovered_count += 1
    except Exception as e:
        logger.error(f"[Unstick] Stuck detection failed: {e}")

    if recovered_count > 0:
        logger.info(f"[Unstick] Recovered {recovered_count} users this cycle")
