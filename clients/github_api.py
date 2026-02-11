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
import re
from datetime import datetime, timezone, timedelta

from config import get_logger
from clients.github_oauth import github_oauth

logger = get_logger(__name__)

MOSCOW_TZ = timezone(timedelta(hours=3))

MONTHS_RU = {
    1: "янв", 2: "фев", 3: "мар", 4: "апр",
    5: "май", 6: "июн", 7: "июл", 8: "авг",
    9: "сен", 10: "окт", 11: "ноя", 12: "дек",
}


class GitHubNotesClient:
    """Клиент для записи заметок в GitHub."""

    def __init__(self):
        self._pending: list[tuple[int, str]] = []

    @staticmethod
    def _extract_title(text: str, max_words: int = 7) -> tuple[str, str]:
        """Извлекает заголовок из первого предложения или первых 5 слов.

        Returns:
            (title, body) — body пустой, если текст короткий.
        """
        # Убираем начальную пунктуацию (. ! ? и пробелы)
        clean = text.lstrip('.!? ')
        if not clean:
            return text.strip() or "заметка", ""
        offset = len(text) - len(clean)

        # Первое предложение (до . ! ? с пробелом или концом строки)
        match = re.search(r'[.!?](?:\s|$)', clean)
        if match:
            title = clean[:match.start() + 1].strip()
            if len(title.split()) <= max_words:
                body = clean[match.end():].strip()
                return title, body

        # Fallback: первые 5 слов
        words = clean.split()
        if len(words) <= 5:
            return clean.strip(), ""
        title = " ".join(words[:5]) + "…"
        return title, clean.strip()

    @staticmethod
    def _format_note_lines(text: str, now: datetime) -> list[str]:
        """Форматирует заметку как markdown-блок.

        Формат: ---\\n\\n**Title**\\n<sub>date</sub>\\n\\nBody
        """
        title, body = GitHubNotesClient._extract_title(text)

        day = now.day
        month = MONTHS_RU[now.month]
        time_str = now.strftime("%H:%M")
        date_str = f"{day} {month}, {time_str}"

        lines = ["---", "", f"**{title}**", f"<sub>{date_str}</sub>"]
        if body:
            lines.extend(["", body])
        lines.append("")
        return lines

    @staticmethod
    def _find_insert_position(lines: list[str]) -> int:
        """Находит позицию для вставки новой заметки.

        Стратегия:
        1. После blockquote-описания (> ...), перед первым ---
        2. Fallback: после заголовка # + пустых строк
        """
        # Пропускаем YAML frontmatter
        start = 0
        if lines and lines[0].strip() == '---':
            for i in range(1, len(lines)):
                if lines[i].strip() == '---':
                    start = i + 1
                    break

        # Ищем заголовок #
        header_pos = None
        for i in range(start, len(lines)):
            if lines[i].startswith('# '):
                header_pos = i
                break

        if header_pos is None:
            return len(lines)

        # Пропускаем blockquote-описание после заголовка
        i = header_pos + 1
        found_blockquote = False
        while i < len(lines):
            stripped = lines[i].strip()
            if stripped.startswith('>'):
                found_blockquote = True
                i += 1
                continue
            if not stripped:
                i += 1
                continue
            # Непустая строка, не blockquote
            if found_blockquote and stripped == '---':
                return i  # вставляем перед этим ---
            if found_blockquote:
                return i
            break

        return i if i < len(lines) else len(lines)

    async def append_note(
        self, telegram_user_id: int, text: str
    ) -> dict | None:
        """Добавляет заметку в файл fleeting-notes.md.

        Returns:
            {"repo": "owner/repo", "path": "inbox/...", "sha": "..."} или None
        """
        repo = await github_oauth.get_target_repo(telegram_user_id)
        if not repo:
            logger.warning(f"No target repo for user {telegram_user_id}")
            return None

        path = await github_oauth.get_notes_path(telegram_user_id)
        access_token = await github_oauth.get_access_token(telegram_user_id)
        if not access_token:
            return None

        now = datetime.now(MOSCOW_TZ)
        note_lines = self._format_note_lines(text, now)

        result = await self._append_to_file(
            access_token=access_token,
            repo=repo,
            path=path,
            note_lines=note_lines,
            commit_message=f"note: {text[:50]}",
        )

        if not result:
            self._pending.append((telegram_user_id, text))
            logger.warning(f"Note queued for retry: {text[:30]}...")

        return result

    async def _append_to_file(
        self,
        access_token: str,
        repo: str,
        path: str,
        note_lines: list[str],
        commit_message: str,
        branch: str = "main",
        max_retries: int = 3,
    ) -> dict | None:
        """Добавляет заметку в файл через Contents API с retry на 409."""
        import aiohttp

        url = f"https://api.github.com/repos/{repo}/contents/{path}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        for attempt in range(max_retries):
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

                            lines = current_content.split("\n")
                            insert_pos = self._find_insert_position(lines)

                            for j, note_line in enumerate(note_lines):
                                lines.insert(insert_pos + j, note_line)

                            updated_content = "\n".join(lines)
                        elif resp.status == 404:
                            current_sha = None
                            now_str = datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d")
                            header = (
                                f"---\ntype: inbox\nstatus: active\n"
                                f"updated: {now_str}\n---\n\n"
                                f"# Fleeting Notes\n\n"
                            )
                            updated_content = header + "\n".join(note_lines)
                        else:
                            error = await resp.text()
                            logger.error(
                                f"GitHub GET {path}: {resp.status} - {error}"
                            )
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
                                "sha": result.get("content", {}).get(
                                    "sha", ""
                                ),
                            }
                        elif resp.status == 409 and attempt < max_retries - 1:
                            logger.warning(
                                f"SHA conflict on {path}, "
                                f"retry {attempt + 1}/{max_retries}"
                            )
                            continue
                        else:
                            error = await resp.text()
                            logger.error(
                                f"GitHub PUT {path}: {resp.status} - {error}"
                            )
                            return None

            except Exception as e:
                logger.error(
                    f"GitHub append_note exception "
                    f"(attempt {attempt + 1}): {e}"
                )
                if attempt < max_retries - 1:
                    continue
                return None

        return None

    async def retry_pending(self):
        """Повторная отправка неотправленных заметок."""
        if not self._pending:
            return

        pending = self._pending.copy()
        self._pending.clear()

        for user_id, text in pending:
            result = await self.append_note(user_id, text)
            if result:
                logger.info(f"Retry succeeded: {text[:30]}...")

    async def clear_notes(self, telegram_user_id: int) -> bool:
        """Очищает файл заметок (сохраняет шапку с описанием)."""
        import aiohttp

        repo = await github_oauth.get_target_repo(telegram_user_id)
        if not repo:
            return False

        path = await github_oauth.get_notes_path(telegram_user_id)
        access_token = await github_oauth.get_access_token(telegram_user_id)
        if not access_token:
            return False

        url = f"https://api.github.com/repos/{repo}/contents/{path}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return False
                    file_data = await resp.json()
                    current_sha = file_data["sha"]
                    current_content = base64.b64decode(
                        file_data["content"]
                    ).decode("utf-8")

                # Сохраняем шапку до позиции вставки заметок
                lines = current_content.split("\n")
                insert_pos = self._find_insert_position(lines)
                clean_content = "\n".join(lines[:insert_pos]).rstrip() + "\n"

                async with session.put(
                    url,
                    headers=headers,
                    json={
                        "message": "clear fleeting notes",
                        "content": base64.b64encode(
                            clean_content.encode("utf-8")
                        ).decode("ascii"),
                        "sha": current_sha,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status in (200, 201):
                        logger.info(f"Notes cleared: {repo}/{path}")
                        return True
                    return False

        except Exception as e:
            logger.error(f"GitHub clear_notes exception: {e}")
            return False


# Singleton instance
github_notes = GitHubNotesClient()
