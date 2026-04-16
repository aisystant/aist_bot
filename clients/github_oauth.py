"""
GitHub OAuth клиент — интеграция для записи в репозитории.

Использует OAuth App (Authorization Code Flow), аналогично Linear.
Scope 'repo' даёт доступ ко всем репо пользователя.

Токены хранятся в PostgreSQL (таблица github_connections).
In-memory кеш ускоряет повторные обращения.

Использование:
    from clients.github_oauth import github_oauth

    # Получить URL для авторизации
    auth_url, state = await github_oauth.get_authorization_url(telegram_user_id=123456)

    # После callback обменять code на токены
    tokens = await github_oauth.exchange_code(code, telegram_user_id)

    # Получить список репо
    repos = await github_oauth.get_repos(telegram_user_id=123456)
"""

import secrets
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import aiohttp

from config import (
    GITHUB_CLIENT_ID,
    GITHUB_CLIENT_SECRET,
    GITHUB_REDIRECT_URI,
    get_logger,
)

logger = get_logger(__name__)

# GitHub OAuth endpoints
GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API_URL = "https://api.github.com"

GITHUB_SCOPES = ["repo"]


class GitHubOAuthClient:
    """OAuth клиент для GitHub API.

    Реализует Authorization Code Flow:
    1. Генерация URL авторизации
    2. Обмен code на access_token
    3. Хранение токенов в PostgreSQL + in-memory кеш
    4. Вызовы GitHub API (REST)
    """

    def __init__(self):
        self.client_id = GITHUB_CLIENT_ID
        self.client_secret = GITHUB_CLIENT_SECRET
        self.redirect_uri = GITHUB_REDIRECT_URI

        # telegram_user_id -> cached data (in-memory кеш)
        self._cache: Dict[int, Dict[str, Any]] = {}

    async def _load_from_db(self, telegram_user_id: int) -> Optional[Dict[str, Any]]:
        """Загружает подключение из БД в кеш."""
        from db.queries.github import get_github_connection

        row = await get_github_connection(telegram_user_id)
        if row:
            self._cache[telegram_user_id] = {
                "access_token": row["access_token"],
                "token_type": row.get("token_type", "bearer"),
                "scope": row.get("scope"),
                "github_username": row.get("github_username"),
                "target_repo": row.get("target_repo"),
                "notes_path": row.get("notes_path") or "inbox/fleeting-notes.md",
                "strategy_repo": row.get("strategy_repo"),
                "strategy_default_branch": row.get("strategy_default_branch") or "main",
                "knowledge_repo": row.get("knowledge_repo"),
                "default_branch": row.get("default_branch") or "main",
            }
            return self._cache[telegram_user_id]
        return None

    async def _get_cached(self, telegram_user_id: int) -> Optional[Dict[str, Any]]:
        """Возвращает данные из кеша или загружает из БД."""
        if telegram_user_id in self._cache:
            return self._cache[telegram_user_id]
        return await self._load_from_db(telegram_user_id)

    async def get_authorization_url(self, telegram_user_id: int) -> Tuple[str, str]:
        """Генерирует URL для OAuth авторизации."""
        if not self.client_id:
            raise ValueError("GITHUB_CLIENT_ID not configured")

        state = secrets.token_urlsafe(32)

        from db.queries.oauth_states import save_oauth_state
        await save_oauth_state(state, 'github', telegram_user_id)

        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(GITHUB_SCOPES),
            "state": state,
        }

        auth_url = f"{GITHUB_AUTHORIZE_URL}?{urlencode(params)}"
        logger.info(f"Generated GitHub auth URL for user {telegram_user_id}")

        return auth_url, state

    async def validate_state(self, state: str) -> Optional[int]:
        """Проверяет state и возвращает telegram_user_id."""
        from db.queries.oauth_states import validate_oauth_state
        telegram_user_id = await validate_oauth_state(state)
        if not telegram_user_id:
            logger.warning(f"Invalid or expired GitHub state: {state[:10]}...")
        return telegram_user_id

    async def exchange_code(self, code: str, telegram_user_id: int) -> Optional[Dict[str, Any]]:
        """Обменивает authorization code на access token и сохраняет в БД.

        Args:
            code: Authorization code из OAuth callback.
            telegram_user_id: ID пользователя (уже проверен через validate_state в callback handler).
        """

        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    GITHUB_TOKEN_URL,
                    json=payload,
                    headers={"Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        tokens = await resp.json()

                        if "error" in tokens:
                            logger.error(f"GitHub token error: {tokens['error']}")
                            return None

                        access_token = tokens.get("access_token")
                        token_type = tokens.get("token_type", "bearer")
                        scope = tokens.get("scope")

                        # Кешируем
                        self._cache[telegram_user_id] = {
                            "access_token": access_token,
                            "token_type": token_type,
                            "scope": scope,
                            "target_repo": None,
                            "notes_path": "inbox/fleeting-notes.md",
                        }

                        # Сохраняем в БД
                        from db.queries.github import save_github_connection

                        await save_github_connection(
                            chat_id=telegram_user_id,
                            access_token=access_token,
                            token_type=token_type,
                            scope=scope,
                        )

                        logger.info(
                            f"Successfully exchanged GitHub code for user {telegram_user_id}"
                        )
                        return self._cache[telegram_user_id]
                    else:
                        error = await resp.text()
                        logger.error(
                            f"GitHub token exchange failed: {resp.status} - {error}"
                        )
                        return None

        except Exception as e:
            logger.error(f"GitHub token exchange exception: {e}")
            return None

    async def get_access_token(self, telegram_user_id: int) -> Optional[str]:
        """Возвращает access_token для пользователя."""
        data = await self._get_cached(telegram_user_id)
        if data:
            return data.get("access_token")
        return None

    async def is_connected(self, telegram_user_id: int) -> bool:
        """Проверяет, подключён ли пользователь к GitHub."""
        data = await self._get_cached(telegram_user_id)
        return data is not None

    async def api_request(
        self,
        telegram_user_id: int,
        method: str,
        endpoint: str,
        json_data: Optional[Dict] = None,
    ) -> Optional[Dict[str, Any]]:
        """Выполняет запрос к GitHub REST API."""
        access_token = await self.get_access_token(telegram_user_id)
        if not access_token:
            logger.warning(f"No GitHub access token for user {telegram_user_id}")
            return None

        url = f"{GITHUB_API_URL}{endpoint}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method,
                    url,
                    json=json_data,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status in (200, 201):
                        return await resp.json()
                    else:
                        error = await resp.text()
                        logger.error(
                            f"GitHub API {method} {endpoint} failed: {resp.status} - {error}"
                        )
                        return None

        except Exception as e:
            logger.error(f"GitHub API exception: {e}")
            return None

    async def get_user(self, telegram_user_id: int) -> Optional[Dict[str, Any]]:
        """Получает информацию о текущем пользователе GitHub."""
        return await self.api_request(telegram_user_id, "GET", "/user")

    async def get_repos(
        self, telegram_user_id: int, limit: int = 30
    ) -> Optional[List[Dict[str, Any]]]:
        """Получает список репозиториев пользователя."""
        access_token = await self.get_access_token(telegram_user_id)
        if not access_token:
            return None

        url = f"{GITHUB_API_URL}/user/repos?per_page={limit}&sort=updated&type=owner"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return None
        except Exception as e:
            logger.error(f"GitHub get repos exception: {e}")
            return None

    async def get_target_repo(self, telegram_user_id: int) -> Optional[str]:
        """Возвращает целевой репо для заметок (owner/repo)."""
        data = await self._get_cached(telegram_user_id)
        if data:
            return data.get("target_repo")
        return None

    async def set_target_repo(self, telegram_user_id: int, repo_full_name: str):
        """Устанавливает целевой репо для заметок. Определяет default_branch через API."""
        # Определяем default_branch через GitHub API
        default_branch = "main"
        repo_info = await self.api_request(
            telegram_user_id, "GET", f"/repos/{repo_full_name}"
        )
        if repo_info:
            default_branch = repo_info.get("default_branch", "main")

        data = await self._get_cached(telegram_user_id)
        if data:
            data["target_repo"] = repo_full_name
            data["default_branch"] = default_branch

        from db.queries.github import update_github_repo

        await update_github_repo(telegram_user_id, repo_full_name, default_branch)
        logger.info(
            f"Set target repo for user {telegram_user_id}: {repo_full_name} (branch: {default_branch})"
        )

    async def get_notes_path(self, telegram_user_id: int) -> str:
        """Возвращает путь к файлу заметок."""
        data = await self._get_cached(telegram_user_id)
        if data and data.get("notes_path"):
            return data["notes_path"]
        return "inbox/fleeting-notes.md"

    async def get_default_branch(self, telegram_user_id: int) -> str:
        """Возвращает дефолтную ветку репозитория."""
        data = await self._get_cached(telegram_user_id)
        if data and data.get("default_branch"):
            return data["default_branch"]
        return "main"

    async def set_notes_path(self, telegram_user_id: int, path: str):
        """Устанавливает путь к файлу заметок."""
        data = await self._get_cached(telegram_user_id)
        if data:
            data["notes_path"] = path

        from db.queries.github import update_github_notes_path

        await update_github_notes_path(telegram_user_id, path)

    async def get_strategy_repo(self, telegram_user_id: int) -> Optional[str]:
        """Возвращает репо стратега (owner/repo)."""
        data = await self._get_cached(telegram_user_id)
        if data:
            return data.get("strategy_repo")
        return None

    async def set_strategy_repo(self, telegram_user_id: int, repo_full_name: str):
        """Устанавливает репо стратега. Определяет default_branch через API."""
        strategy_default_branch = "main"
        repo_info = await self.api_request(
            telegram_user_id, "GET", f"/repos/{repo_full_name}"
        )
        if repo_info:
            strategy_default_branch = repo_info.get("default_branch", "main")

        data = await self._get_cached(telegram_user_id)
        if data:
            data["strategy_repo"] = repo_full_name
            data["strategy_default_branch"] = strategy_default_branch

        from db.queries.github import update_github_strategy_repo

        await update_github_strategy_repo(telegram_user_id, repo_full_name, strategy_default_branch)
        logger.info(
            f"Set strategy repo for user {telegram_user_id}: {repo_full_name} (branch: {strategy_default_branch})"
        )

    async def get_strategy_default_branch(self, telegram_user_id: int) -> str:
        """Возвращает дефолтную ветку strategy-репозитория.

        Lazy backfill: если branch не определён (дефолт 'main') и strategy_repo задан,
        делает один API-вызов, обновляет кеш и БД. Повторных вызовов не будет.
        """
        data = await self._get_cached(telegram_user_id)
        if not data:
            return "main"

        stored = data.get("strategy_default_branch") or "main"
        strategy_repo = data.get("strategy_repo")

        # Lazy backfill: дефолт 'main' + есть repo → проверить через API
        if stored == "main" and strategy_repo:
            repo_info = await self.api_request(
                telegram_user_id, "GET", f"/repos/{strategy_repo}"
            )
            if repo_info:
                real_branch = repo_info.get("default_branch", "main")
                if real_branch != stored:
                    data["strategy_default_branch"] = real_branch
                    from db.queries.github import update_github_strategy_repo
                    await update_github_strategy_repo(telegram_user_id, strategy_repo, real_branch)
                    logger.info(
                        f"Lazy backfill strategy branch for {telegram_user_id}: {stored} → {real_branch}"
                    )
                    return real_branch

        return stored

    async def get_knowledge_repo(self, telegram_user_id: int) -> Optional[str]:
        """Возвращает репо индекса знаний (owner/repo) для Публикатора."""
        data = await self._get_cached(telegram_user_id)
        if data:
            return data.get("knowledge_repo")
        return None

    async def set_knowledge_repo(self, telegram_user_id: int, repo_full_name: str):
        """Устанавливает репо индекса знаний для Публикатора."""
        data = await self._get_cached(telegram_user_id)
        if data:
            data["knowledge_repo"] = repo_full_name

        from db.queries.github import update_github_knowledge_repo

        await update_github_knowledge_repo(telegram_user_id, repo_full_name)
        logger.info(
            f"Set knowledge repo for user {telegram_user_id}: {repo_full_name}"
        )

    async def disconnect(self, telegram_user_id: int):
        """Отключает пользователя от GitHub."""
        if telegram_user_id in self._cache:
            del self._cache[telegram_user_id]

        from db.queries.github import delete_github_connection

        await delete_github_connection(telegram_user_id)
        logger.info(f"Disconnected user {telegram_user_id} from GitHub")


# Singleton instance
github_oauth = GitHubOAuthClient()
