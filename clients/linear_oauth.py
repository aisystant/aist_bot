"""
Linear OAuth клиент — тестовая интеграция.

Этот код временный, для проверки OAuth flow перед интеграцией с Digital Twin.
После успешного теста будет удалён или адаптирован.

Использование:
    from clients.linear_oauth import linear_oauth

    # Получить URL для авторизации
    auth_url, state = linear_oauth.get_authorization_url(telegram_user_id=123456)

    # После callback обменять code на токены
    tokens = await linear_oauth.exchange_code(code, state)

    # Использовать API Linear
    issues = await linear_oauth.get_my_issues(telegram_user_id=123456)
"""

import secrets
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import aiohttp

from config import (
    LINEAR_CLIENT_ID,
    LINEAR_CLIENT_SECRET,
    LINEAR_REDIRECT_URI,
    get_logger,
)

logger = get_logger(__name__)

# Linear OAuth endpoints
LINEAR_AUTHORIZE_URL = "https://linear.app/oauth/authorize"
LINEAR_TOKEN_URL = "https://api.linear.app/oauth/token"
LINEAR_API_URL = "https://api.linear.app/graphql"

# Scopes для Linear OAuth
LINEAR_SCOPES = ["read", "issues:create"]


class LinearOAuthClient:
    """OAuth клиент для Linear API.

    Реализует Authorization Code Flow:
    1. Генерация URL авторизации
    2. Обмен code на access_token
    3. Хранение токенов в памяти (для теста)
    4. Вызовы Linear API
    """

    def __init__(self):
        self.client_id = LINEAR_CLIENT_ID
        self.client_secret = LINEAR_CLIENT_SECRET
        self.redirect_uri = LINEAR_REDIRECT_URI

        # Временное хранилище state -> telegram_user_id
        # В продакшене должно быть в Redis/DB с TTL
        self._pending_states: Dict[str, Dict[str, Any]] = {}

        # Хранилище токенов: telegram_user_id -> tokens
        # В продакшене должно быть в БД
        self._tokens: Dict[int, Dict[str, Any]] = {}

    def get_authorization_url(self, telegram_user_id: int) -> Tuple[str, str]:
        """Генерирует URL для OAuth авторизации.

        Args:
            telegram_user_id: ID пользователя Telegram

        Returns:
            Tuple[auth_url, state]
        """
        if not self.client_id:
            raise ValueError("LINEAR_CLIENT_ID not configured")

        # Генерируем уникальный state
        state = secrets.token_urlsafe(32)

        # Сохраняем mapping state -> user_id
        self._pending_states[state] = {
            "telegram_user_id": telegram_user_id,
            "created_at": time.time()
        }

        # Чистим старые states (старше 10 минут)
        self._cleanup_old_states()

        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": ",".join(LINEAR_SCOPES),
            "state": state,
        }

        auth_url = f"{LINEAR_AUTHORIZE_URL}?{urlencode(params)}"
        logger.info(f"Generated auth URL for user {telegram_user_id}")

        return auth_url, state

    def _cleanup_old_states(self):
        """Удаляет просроченные states."""
        now = time.time()
        expired = [
            state for state, data in self._pending_states.items()
            if now - data["created_at"] > 600  # 10 минут
        ]
        for state in expired:
            del self._pending_states[state]

    def validate_state(self, state: str) -> Optional[int]:
        """Проверяет state и возвращает telegram_user_id.

        Args:
            state: State из callback

        Returns:
            telegram_user_id или None если state невалидный
        """
        data = self._pending_states.get(state)
        if not data:
            logger.warning(f"Invalid or expired state: {state[:10]}...")
            return None

        # Проверяем TTL
        if time.time() - data["created_at"] > 600:
            del self._pending_states[state]
            logger.warning(f"Expired state: {state[:10]}...")
            return None

        return data["telegram_user_id"]

    async def exchange_code(self, code: str, state: str) -> Optional[Dict[str, Any]]:
        """Обменивает authorization code на access token.

        Args:
            code: Authorization code из callback
            state: State для верификации

        Returns:
            Токены или None при ошибке
        """
        # Валидируем state
        telegram_user_id = self.validate_state(state)
        if not telegram_user_id:
            return None

        # Удаляем использованный state
        del self._pending_states[state]

        payload = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "code": code,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    LINEAR_TOKEN_URL,
                    data=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        tokens = await resp.json()

                        # Сохраняем токены
                        self._tokens[telegram_user_id] = {
                            "access_token": tokens.get("access_token"),
                            "token_type": tokens.get("token_type", "Bearer"),
                            "scope": tokens.get("scope"),
                            "created_at": time.time(),
                            # Linear не возвращает refresh_token для public apps
                        }

                        logger.info(f"Successfully exchanged code for user {telegram_user_id}")
                        return self._tokens[telegram_user_id]
                    else:
                        error = await resp.text()
                        logger.error(f"Token exchange failed: {resp.status} - {error}")
                        return None

        except Exception as e:
            logger.error(f"Token exchange exception: {e}")
            return None

    def get_access_token(self, telegram_user_id: int) -> Optional[str]:
        """Возвращает access_token для пользователя.

        Args:
            telegram_user_id: ID пользователя Telegram

        Returns:
            Access token или None
        """
        tokens = self._tokens.get(telegram_user_id)
        if tokens:
            return tokens.get("access_token")
        return None

    def is_connected(self, telegram_user_id: int) -> bool:
        """Проверяет, подключён ли пользователь к Linear."""
        return telegram_user_id in self._tokens

    async def graphql_query(
        self,
        telegram_user_id: int,
        query: str,
        variables: Optional[Dict] = None
    ) -> Optional[Dict[str, Any]]:
        """Выполняет GraphQL запрос к Linear API.

        Args:
            telegram_user_id: ID пользователя
            query: GraphQL запрос
            variables: Переменные запроса

        Returns:
            Результат запроса или None
        """
        access_token = self.get_access_token(telegram_user_id)
        if not access_token:
            logger.warning(f"No access token for user {telegram_user_id}")
            return None

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    LINEAR_API_URL,
                    json={"query": query, "variables": variables or {}},
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                    },
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if "errors" in data:
                            logger.error(f"GraphQL errors: {data['errors']}")
                            return None
                        return data.get("data")
                    else:
                        error = await resp.text()
                        logger.error(f"GraphQL request failed: {resp.status} - {error}")
                        return None

        except Exception as e:
            logger.error(f"GraphQL exception: {e}")
            return None

    async def get_viewer(self, telegram_user_id: int) -> Optional[Dict[str, Any]]:
        """Получает информацию о текущем пользователе Linear.

        Returns:
            Данные пользователя или None
        """
        query = """
        query {
            viewer {
                id
                name
                email
            }
        }
        """
        data = await self.graphql_query(telegram_user_id, query)
        if data:
            return data.get("viewer")
        return None

    async def get_my_issues(
        self,
        telegram_user_id: int,
        limit: int = 10
    ) -> Optional[list]:
        """Получает задачи пользователя.

        Args:
            telegram_user_id: ID пользователя
            limit: Максимальное количество задач

        Returns:
            Список задач или None
        """
        query = """
        query($limit: Int!) {
            viewer {
                assignedIssues(first: $limit) {
                    nodes {
                        id
                        identifier
                        title
                        state {
                            name
                        }
                        priority
                        url
                    }
                }
            }
        }
        """
        data = await self.graphql_query(
            telegram_user_id,
            query,
            {"limit": limit}
        )
        if data and data.get("viewer"):
            return data["viewer"].get("assignedIssues", {}).get("nodes", [])
        return None

    async def create_issue(
        self,
        telegram_user_id: int,
        title: str,
        description: Optional[str] = None,
        team_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Создаёт новую задачу в Linear.

        Args:
            telegram_user_id: ID пользователя
            title: Заголовок задачи
            description: Описание
            team_id: ID команды (если не указан — нужно получить)

        Returns:
            Созданная задача или None
        """
        # Если team_id не указан, получаем первую доступную команду
        if not team_id:
            teams_query = """
            query {
                teams {
                    nodes {
                        id
                        name
                    }
                }
            }
            """
            teams_data = await self.graphql_query(telegram_user_id, teams_query)
            if teams_data and teams_data.get("teams", {}).get("nodes"):
                team_id = teams_data["teams"]["nodes"][0]["id"]
            else:
                logger.error("No teams found for user")
                return None

        mutation = """
        mutation($title: String!, $teamId: String!, $description: String) {
            issueCreate(input: {
                title: $title
                teamId: $teamId
                description: $description
            }) {
                success
                issue {
                    id
                    identifier
                    title
                    url
                }
            }
        }
        """
        data = await self.graphql_query(
            telegram_user_id,
            mutation,
            {
                "title": title,
                "teamId": team_id,
                "description": description,
            }
        )
        if data and data.get("issueCreate", {}).get("success"):
            return data["issueCreate"].get("issue")
        return None

    def disconnect(self, telegram_user_id: int):
        """Отключает пользователя от Linear."""
        if telegram_user_id in self._tokens:
            del self._tokens[telegram_user_id]
            logger.info(f"Disconnected user {telegram_user_id} from Linear")


# Singleton instance
linear_oauth = LinearOAuthClient()
