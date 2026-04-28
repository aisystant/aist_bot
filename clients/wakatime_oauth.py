"""
WakaTime OAuth клиент — интеграция для Activity Hub (WP-109).

Использует OAuth 2.0 Authorization Code Flow.
Scope 'read_stats' даёт доступ к статистике времени.

Токены хранятся в development.user_integrations (общая таблица Activity Hub).

Использование:
    from clients.wakatime_oauth import wakatime_oauth

    # Получить URL для авторизации
    auth_url, state = wakatime_oauth.get_authorization_url(telegram_user_id=123456)

    # После callback обменять code на токены
    tokens = await wakatime_oauth.exchange_code(code, state, telegram_user_id)
"""

import secrets
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import aiohttp

from config import (
    WAKATIME_CLIENT_ID,
    WAKATIME_CLIENT_SECRET,
    WAKATIME_REDIRECT_URI,
    get_logger,
)

logger = get_logger(__name__)

# WakaTime OAuth endpoints
WAKATIME_AUTHORIZE_URL = "https://wakatime.com/oauth/authorize"
WAKATIME_TOKEN_URL = "https://wakatime.com/oauth/token"
WAKATIME_API_URL = "https://wakatime.com/api/v1"

WAKATIME_SCOPES = ["read_stats", "read_logged_time"]


class WakaTimeOAuthClient:
    """OAuth клиент для WakaTime API.

    Реализует Authorization Code Flow:
    1. Генерация URL авторизации
    2. Обмен code на access_token + refresh_token
    3. Хранение токенов в development.user_integrations
    """

    def __init__(self):
        self.client_id = WAKATIME_CLIENT_ID
        self.client_secret = WAKATIME_CLIENT_SECRET
        self.redirect_uri = WAKATIME_REDIRECT_URI

        # state -> telegram_user_id (TTL 10 мин)
        self._pending_states: Dict[str, Dict[str, Any]] = {}

    def get_authorization_url(self, telegram_user_id: int) -> Tuple[str, str]:
        """Генерирует URL для OAuth авторизации."""
        if not self.client_id:
            raise ValueError("WAKATIME_CLIENT_ID not configured")

        state = secrets.token_urlsafe(32)

        self._pending_states[state] = {
            "telegram_user_id": telegram_user_id,
            "created_at": time.time(),
        }

        self._cleanup_old_states()

        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": ",".join(WAKATIME_SCOPES),
            "response_type": "code",
            "state": state,
        }

        auth_url = f"{WAKATIME_AUTHORIZE_URL}?{urlencode(params)}"
        logger.info(f"Generated WakaTime auth URL for user {telegram_user_id}")

        return auth_url, state

    def _cleanup_old_states(self):
        """Удаляет просроченные states (>10 мин)."""
        now = time.time()
        expired = [
            state
            for state, data in self._pending_states.items()
            if now - data["created_at"] > 600
        ]
        for state in expired:
            del self._pending_states[state]

    def validate_state(self, state: str) -> Optional[int]:
        """Проверяет state и возвращает telegram_user_id."""
        data = self._pending_states.get(state)
        if not data:
            logger.warning(f"Invalid or expired WakaTime state: {state[:10]}...")
            return None

        if time.time() - data["created_at"] > 600:
            del self._pending_states[state]
            logger.warning(f"Expired WakaTime state: {state[:10]}...")
            return None

        return data["telegram_user_id"]

    async def exchange_code(self, code: str, state: str, telegram_user_id: int) -> Optional[Dict[str, Any]]:
        """Обменивает authorization code на access token и сохраняет в user_integrations.

        Args:
            code: Authorization code из OAuth callback.
            state: OAuth state (используется для cleanup pending_states).
            telegram_user_id: ID пользователя (уже проверен через validate_state в callback handler).
        """
        # Cleanup state (validate_state не удаляет из dict, только проверяет)
        self._pending_states.pop(state, None)

        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    WAKATIME_TOKEN_URL,
                    data=payload,
                    headers={"Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        tokens = await resp.json()

                        if "error" in tokens:
                            logger.error(f"WakaTime token error: {tokens['error']}")
                            return None

                        access_token = tokens.get("access_token")
                        refresh_token = tokens.get("refresh_token")
                        scope = tokens.get("scope", "")

                        # Сохраняем в user_integrations
                        await self._save_connection(
                            telegram_user_id=telegram_user_id,
                            access_token=access_token,
                            refresh_token=refresh_token,
                            scope=scope,
                        )

                        logger.info(
                            f"Successfully exchanged WakaTime code for user {telegram_user_id}"
                        )
                        return {
                            "access_token": access_token,
                            "refresh_token": refresh_token,
                            "telegram_user_id": telegram_user_id,
                        }
                    else:
                        error = await resp.text()
                        logger.error(
                            f"WakaTime token exchange failed: {resp.status} - {error}"
                        )
                        return None

        except Exception as e:
            logger.error(f"WakaTime token exchange exception: {e}")
            return None

    async def _save_connection(
        self,
        telegram_user_id: int,
        access_token: str,
        refresh_token: str = None,
        scope: str = "",
    ) -> None:
        """Сохраняет WakaTime-токен в persona.user_integrations."""
        from db.connection import get_persona_pool

        try:
            pool = await get_persona_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    'SELECT account_id FROM ory_identity WHERE telegram_id = $1', telegram_user_id
                )
                if not row or not row['account_id']:
                    logger.warning(
                        f"WakaTime save: no account_id in ory_identity for telegram_id={telegram_user_id}"
                    )
                    return

                account_id = row['account_id']

                await conn.execute('''
                    INSERT INTO user_integrations
                        (account_id, service, access_token, refresh_token, scope,
                         metadata, connected_at, updated_at, active)
                    VALUES ($1, 'wakatime', $2, $3, $4, '{}', NOW(), NOW(), TRUE)
                    ON CONFLICT (account_id, service) DO UPDATE SET
                        access_token = $2,
                        refresh_token = COALESCE($3, user_integrations.refresh_token),
                        scope = $4,
                        updated_at = NOW(),
                        active = TRUE
                ''',
                    account_id,
                    access_token,
                    refresh_token,
                    scope,
                )
                logger.info(f"Saved WakaTime connection for telegram_id={telegram_user_id}")
        except Exception as e:
            if 'does not exist' in str(e):
                logger.warning(
                    f"WakaTime save skipped: persona tables not available (pilot?): {e}"
                )
            else:
                raise

    async def is_connected(self, telegram_user_id: int) -> bool:
        """Проверяет, подключён ли пользователь к WakaTime."""
        from db.connection import get_persona_pool

        pool = await get_persona_pool()
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow('''
                    SELECT 1 FROM user_integrations ui
                    JOIN ory_identity oi ON oi.account_id = ui.account_id
                    WHERE oi.telegram_id = $1 AND ui.service = 'wakatime' AND ui.active = TRUE
                ''', telegram_user_id)
                return row is not None
        except Exception as e:
            logger.warning(f"WakaTime is_connected error: {e}")
            return False

    async def disconnect(self, telegram_user_id: int) -> None:
        """Отключает пользователя от WakaTime."""
        from db.connection import get_persona_pool

        try:
            pool = await get_persona_pool()
            async with pool.acquire() as conn:
                await conn.execute('''
                    UPDATE user_integrations
                    SET active = FALSE, updated_at = NOW()
                    WHERE account_id = (
                        SELECT account_id FROM ory_identity WHERE telegram_id = $1
                    )
                    AND service = 'wakatime'
                ''', telegram_user_id)
            logger.info(f"Disconnected telegram_id={telegram_user_id} from WakaTime")
        except Exception as e:
            if 'does not exist' in str(e):
                logger.warning(
                    f"WakaTime disconnect skipped: persona tables not available (pilot?): {e}"
                )
            else:
                raise


# Singleton instance
wakatime_oauth = WakaTimeOAuthClient()
