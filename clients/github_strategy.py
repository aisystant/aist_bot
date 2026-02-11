"""
GitHub Strategy Client — чтение файлов стратега из GitHub.

Использует GitHub Contents API для чтения DayPlan, WeekPlan, WeekReport
из репозитория DS-my-strategy через OAuth-токен.

Использование:
    from clients.github_strategy import github_strategy

    day_plan = await github_strategy.get_day_plan(telegram_user_id=123456)
    week_plan = await github_strategy.get_week_plan(telegram_user_id=123456)
"""

import base64
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import aiohttp

from config import get_logger
from clients.github_oauth import github_oauth

logger = get_logger(__name__)

MOSCOW_TZ = timezone(timedelta(hours=3))
GITHUB_API_URL = "https://api.github.com"


class GitHubStrategyClient:
    """Клиент для чтения файлов стратега из GitHub."""

    async def _get_auth(self, telegram_user_id: int) -> tuple[Optional[str], Optional[str]]:
        """Возвращает (access_token, strategy_repo) или (None, None)."""
        strategy_repo = await github_oauth.get_strategy_repo(telegram_user_id)
        if not strategy_repo:
            return None, None
        access_token = await github_oauth.get_access_token(telegram_user_id)
        if not access_token:
            return None, None
        return access_token, strategy_repo

    async def read_file(
        self, telegram_user_id: int, repo: str, path: str
    ) -> Optional[str]:
        """Читает файл из GitHub через Contents API."""
        access_token = await github_oauth.get_access_token(telegram_user_id)
        if not access_token:
            return None

        url = f"{GITHUB_API_URL}/repos/{repo}/contents/{path}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        content = base64.b64decode(data["content"]).decode("utf-8")
                        return content
                    elif resp.status == 404:
                        logger.info(f"File not found: {repo}/{path}")
                        return None
                    else:
                        error = await resp.text()
                        logger.error(f"GitHub GET {path}: {resp.status} - {error}")
                        return None
        except Exception as e:
            logger.error(f"GitHub read_file exception: {e}")
            return None

    async def list_directory(
        self, telegram_user_id: int, repo: str, path: str
    ) -> Optional[List[dict]]:
        """Получает список файлов в директории GitHub."""
        access_token = await github_oauth.get_access_token(telegram_user_id)
        if not access_token:
            return None

        url = f"{GITHUB_API_URL}/repos/{repo}/contents/{path}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return None
        except Exception as e:
            logger.error(f"GitHub list_directory exception: {e}")
            return None

    async def get_day_plan(self, telegram_user_id: int) -> Optional[str]:
        """Получает DayPlan на сегодня."""
        access_token, repo = await self._get_auth(telegram_user_id)
        if not access_token:
            return None

        today = datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d")
        path = f"current/DayPlan {today}.md"

        return await self.read_file(telegram_user_id, repo, path)

    async def get_week_plan(self, telegram_user_id: int) -> Optional[str]:
        """Получает последний WeekPlan."""
        access_token, repo = await self._get_auth(telegram_user_id)
        if not access_token:
            return None

        files = await self.list_directory(telegram_user_id, repo, "current")
        if not files:
            return None

        # Ищем WeekPlan W*.md (последний по имени)
        week_plans = sorted(
            [f for f in files if f["name"].startswith("WeekPlan W") and f["name"].endswith(".md")],
            key=lambda f: f["name"],
            reverse=True,
        )

        if not week_plans:
            return None

        return await self.read_file(telegram_user_id, repo, week_plans[0]["path"])

    async def get_week_report(self, telegram_user_id: int) -> Optional[str]:
        """Получает последний WeekReport."""
        access_token, repo = await self._get_auth(telegram_user_id)
        if not access_token:
            return None

        files = await self.list_directory(telegram_user_id, repo, "current")
        if not files:
            return None

        week_reports = sorted(
            [f for f in files if f["name"].startswith("WeekReport W") and f["name"].endswith(".md")],
            key=lambda f: f["name"],
            reverse=True,
        )

        if not week_reports:
            return None

        return await self.read_file(telegram_user_id, repo, week_reports[0]["path"])

    async def get_strategy_repo_url(self, telegram_user_id: int) -> Optional[str]:
        """Возвращает URL репо стратега на GitHub."""
        repo = await github_oauth.get_strategy_repo(telegram_user_id)
        if repo:
            return f"https://github.com/{repo}"
        return None


# Singleton instance
github_strategy = GitHubStrategyClient()
