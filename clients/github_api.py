"""
GitHub Contents API — запись файлов в репозитории.

Использует GitHub REST API для создания/обновления файлов.
Работает через OAuth-токен из github_oauth.

Использование:
    from clients.github_api import github_notes

    result = await github_notes.append_note(
        telegram_user_id=123456,
        text="купить книгу по системному мышлению"
    )
"""

import base64
from datetime import datetime, timezone, timedelta

from config import get_logger
from clients.github_oauth import github_oauth

logger = get_logger(__name__)

MOSCOW_TZ = timezone(timedelta(hours=3))


class GitHubNotesClient:
    """Клиент для записи исчезающих заметок в GitHub."""

    async def append_note(
        self, telegram_user_id: int, text: str
    ) -> dict | None:
        """Добавляет заметку в файл fleeting-notes.md.

        Returns:
            {"repo": "owner/repo", "path": "inbox/...", "sha": "..."} или None
        """
        repo = github_oauth.get_target_repo(telegram_user_id)
        if not repo:
            logger.warning(f"No target repo for user {telegram_user_id}")
            return None

        path = github_oauth.get_notes_path(telegram_user_id)
        access_token = github_oauth.get_access_token(telegram_user_id)
        if not access_token:
            return None

        now = datetime.now(MOSCOW_TZ)
        timestamp = now.strftime("%Y-%m-%d %H:%M")
        new_line = f"- {timestamp} — {text}\n"

        return await self._append_to_file(
            access_token=access_token,
            repo=repo,
            path=path,
            new_line=new_line,
            commit_message=f"note: {text[:50]}",
        )

    async def _append_to_file(
        self,
        access_token: str,
        repo: str,
        path: str,
        new_line: str,
        commit_message: str,
        branch: str = "main",
    ) -> dict | None:
        """Добавляет строку в конец файла через Contents API."""
        import aiohttp

        url = f"https://api.github.com/repos/{repo}/contents/{path}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            async with aiohttp.ClientSession() as session:
                # 1. Получаем текущий файл
                async with session.get(
                    url,
                    headers=headers,
                    params={"ref": branch},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        file_data = await resp.json()
                        current_sha = file_data["sha"]
                        current_content = base64.b64decode(
                            file_data["content"]
                        ).decode("utf-8")
                        updated_content = current_content.rstrip("\n") + "\n" + new_line
                    elif resp.status == 404:
                        # Файл не существует — создаём
                        current_sha = None
                        updated_content = (
                            "# Исчезающие заметки\n\n" + new_line
                        )
                    else:
                        error = await resp.text()
                        logger.error(f"GitHub GET {path}: {resp.status} - {error}")
                        return None

                # 2. Записываем обновлённый файл
                put_data = {
                    "message": commit_message,
                    "content": base64.b64encode(
                        updated_content.encode("utf-8")
                    ).decode("ascii"),
                    "branch": branch,
                }
                if current_sha:
                    put_data["sha"] = current_sha

                async with session.put(
                    url,
                    headers=headers,
                    json=put_data,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status in (200, 201):
                        result = await resp.json()
                        logger.info(f"Note written to {repo}/{path}")
                        return {
                            "repo": repo,
                            "path": path,
                            "sha": result.get("content", {}).get("sha", ""),
                        }
                    else:
                        error = await resp.text()
                        logger.error(f"GitHub PUT {path}: {resp.status} - {error}")
                        return None

        except Exception as e:
            logger.error(f"GitHub append_note exception: {e}")
            return None


# Singleton instance
github_notes = GitHubNotesClient()
