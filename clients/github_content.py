"""
GitHub Content API клиент — доступ к индексу знаний (DS-Knowledge-Index).

Используется Публикатором (R21) для:
- Сканирования постов (status=ready, target=club)
- Чтения контента для публикации
- Обновления frontmatter (status→published) после публикации

Использование:
    from clients.github_content import github_content, parse_frontmatter

    # Список файлов
    files = await github_content.list_files("docs/2026")

    # Прочитать пост
    content, sha = await github_content.read_file("docs/2026/2026-02-14-post.md")
    fm = parse_frontmatter(content)

    # Обновить frontmatter
    new_content = update_frontmatter_field(content, "status", "published")
    await github_content.update_file(path, new_content, sha, "Published to club: Title")
"""

import base64
import re

import aiohttp

from config import get_logger

logger = get_logger(__name__)


class GitHubContentClient:
    """HTTP-клиент для GitHub Contents API."""

    _session: aiohttp.ClientSession | None = None

    def __init__(self, token: str, repo: str):
        self.token = token
        self.repo = repo  # "owner/repo"
        self.base_url = "https://api.github.com"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def list_files(self, path: str) -> list[dict]:
        """Список .md файлов в директории. Возвращает [{name, path, sha, size}, ...]."""
        session = await self._get_session()
        url = f"{self.base_url}/repos/{self.repo}/contents/{path}"
        async with session.get(url, headers=self._headers()) as resp:
            if resp.status >= 400:
                logger.error(f"GitHub list_files {path} error {resp.status}")
                return []
            data = await resp.json()
            if isinstance(data, list):
                return [
                    {"name": f["name"], "path": f["path"], "sha": f["sha"], "size": f["size"]}
                    for f in data if f["type"] == "file" and f["name"].endswith(".md")
                ]
            return []

    async def list_dirs(self, path: str) -> list[str]:
        """Список поддиректорий. Возвращает имена директорий."""
        session = await self._get_session()
        url = f"{self.base_url}/repos/{self.repo}/contents/{path}"
        async with session.get(url, headers=self._headers()) as resp:
            if resp.status >= 400:
                return []
            data = await resp.json()
            if isinstance(data, list):
                return [f["name"] for f in data if f["type"] == "dir"]
            return []

    async def read_file(self, path: str) -> tuple[str, str] | None:
        """Прочитать файл. Возвращает (content, sha) или None."""
        session = await self._get_session()
        url = f"{self.base_url}/repos/{self.repo}/contents/{path}"
        async with session.get(url, headers=self._headers()) as resp:
            if resp.status >= 400:
                logger.error(f"GitHub read_file {path} error {resp.status}")
                return None
            data = await resp.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            return content, data["sha"]

    async def update_file(self, path: str, content: str, sha: str, message: str) -> bool:
        """Обновить файл (git commit). Возвращает True при успехе."""
        session = await self._get_session()
        url = f"{self.base_url}/repos/{self.repo}/contents/{path}"
        payload = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "sha": sha,
        }
        async with session.put(url, json=payload, headers=self._headers()) as resp:
            if resp.status >= 400:
                text = await resp.text()
                logger.error(f"GitHub update_file {path} error {resp.status}: {text[:300]}")
                return False
            logger.info(f"GitHub file updated: {path} ({message})")
            return True

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ── Frontmatter helpers ──────────────────────────────────────


def parse_frontmatter(content: str) -> dict:
    """Извлечь frontmatter из markdown-файла. Возвращает dict полей."""
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    fm_text = match.group(1)
    result = {}
    for line in fm_text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # Handle lists like tags: [tag1, tag2]
            if value.startswith("[") and value.endswith("]"):
                value = [v.strip().strip('"').strip("'") for v in value[1:-1].split(",") if v.strip()]
            result[key] = value
    return result


def update_frontmatter_field(content: str, field: str, new_value: str) -> str:
    """Обновить одно поле в frontmatter. Возвращает обновлённый content."""
    match = re.match(r"^(---\s*\n)(.*?)(\n---)", content, re.DOTALL)
    if not match:
        return content
    prefix, fm_text, suffix = match.group(1), match.group(2), match.group(3)
    rest = content[match.end():]

    lines = fm_text.split("\n")
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{field}:"):
            lines[i] = f"{field}: {new_value}"
            found = True
            break
    if not found:
        lines.append(f"{field}: {new_value}")

    return prefix + "\n".join(lines) + suffix + rest


def strip_frontmatter(content: str) -> str:
    """Удалить frontmatter из markdown. Возвращает чистый контент."""
    match = re.match(r"^---\s*\n.*?\n---\s*\n?", content, re.DOTALL)
    if match:
        return content[match.end():]
    return content


# ── Singleton ──────────────────────────────────────────────

import os

_gh_token = os.getenv("GITHUB_BOT_PAT", "")
_gh_repo = os.getenv("GITHUB_KNOWLEDGE_REPO", "")

github_content = GitHubContentClient(_gh_token, _gh_repo) if _gh_token and _gh_repo else None
