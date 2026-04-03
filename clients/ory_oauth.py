"""
Ory OAuth клиент — регистрация/авторизация через Ory (Hydra + Kratos).

WP-187: связка telegram_id → ory_id при переходе T0→T1.

Flow:
1. Бот отправляет пользователю ссылку на /authorize
2. Пользователь логинится/регистрируется в Ory (Kratos form)
3. Ory редиректит на /auth/ory/callback с code
4. Callback обменивает code на access_token
5. Из /userinfo получаем ory_id (sub) + email
6. Вызываем link_ory(telegram_id, ory_id, email)

Использование:
    from clients.ory_oauth import ory_oauth

    auth_url, state = ory_oauth.get_authorization_url(telegram_user_id=123456)
    tokens = await ory_oauth.exchange_code(code, state)
    userinfo = await ory_oauth.get_userinfo(access_token)
"""

import secrets
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import aiohttp

from config import (
    ORY_BASE_URL,
    ORY_CLIENT_ID,
    ORY_CLIENT_SECRET,
    ORY_REDIRECT_URI,
    get_logger,
)

logger = get_logger(__name__)

# Ory OAuth endpoints (Hydra)
ORY_AUTHORIZE_URL = f"{ORY_BASE_URL}/oauth2/auth"
ORY_TOKEN_URL = f"{ORY_BASE_URL}/oauth2/token"
ORY_USERINFO_URL = f"{ORY_BASE_URL}/userinfo"

ORY_SCOPES = ["openid", "email", "profile"]

STATE_TTL = 600  # 10 minutes


class OryOAuthClient:
    """OAuth клиент для Ory (Authorization Code Flow)."""

    def __init__(self):
        self.client_id = ORY_CLIENT_ID
        self.client_secret = ORY_CLIENT_SECRET
        self.redirect_uri = ORY_REDIRECT_URI

        # state -> {telegram_user_id, created_at}
        self._pending_states: Dict[str, Dict[str, Any]] = {}

    def get_authorization_url(self, telegram_user_id: int) -> Tuple[str, str]:
        """Генерирует URL для авторизации через Ory.

        Returns:
            (auth_url, state)
        """
        self._cleanup_expired_states()

        state = secrets.token_urlsafe(32)
        self._pending_states[state] = {
            "telegram_user_id": telegram_user_id,
            "created_at": time.time(),
        }

        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(ORY_SCOPES),
            "state": state,
        }

        url = f"{ORY_AUTHORIZE_URL}?{urlencode(params)}"
        return url, state

    def validate_state(self, state: str) -> Optional[int]:
        """Проверяет state и возвращает telegram_user_id."""
        data = self._pending_states.pop(state, None)
        if not data:
            return None
        if time.time() - data["created_at"] > STATE_TTL:
            return None
        return data["telegram_user_id"]

    async def exchange_code(self, code: str, state: str) -> Optional[Dict[str, Any]]:
        """Обменивает authorization code на access_token.

        Returns:
            {"access_token": ..., "token_type": ..., "id_token": ...} or None
        """
        telegram_user_id = self.validate_state(state)
        if not telegram_user_id:
            logger.warning("[OryOAuth] Invalid or expired state")
            return None

        async with aiohttp.ClientSession() as session:
            data = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }

            async with session.post(ORY_TOKEN_URL, data=data) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    logger.error(f"[OryOAuth] Token exchange failed: {resp.status} {err}")
                    return None

                tokens = await resp.json()
                tokens["telegram_user_id"] = telegram_user_id
                logger.info(f"[OryOAuth] Token exchanged for user {telegram_user_id}")
                return tokens

    async def get_userinfo(self, access_token: str) -> Optional[Dict[str, Any]]:
        """Получает профиль пользователя из Ory /userinfo.

        Returns:
            {"sub": ory_id, "email": ..., ...} or None
        """
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {access_token}"}
            async with session.get(ORY_USERINFO_URL, headers=headers) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    logger.error(f"[OryOAuth] Userinfo failed: {resp.status} {err}")
                    return None
                return await resp.json()

    def _cleanup_expired_states(self):
        """Очищает просроченные state."""
        now = time.time()
        expired = [s for s, d in self._pending_states.items() if now - d["created_at"] > STATE_TTL]
        for s in expired:
            del self._pending_states[s]


# Singleton
ory_oauth = OryOAuthClient()
