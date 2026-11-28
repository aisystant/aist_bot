from __future__ import annotations

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
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from uuid import uuid4

from clients.github_auth import (
    AuthContext,
    GitHubAuthUnavailable,
    OperationClass,
    resolve_auth_context,
)
from clients.github_oauth import github_oauth
from config import get_logger

logger = get_logger(__name__)

MOSCOW_TZ = timezone(timedelta(hours=3))

MONTHS_RU = {
    1: "янв",
    2: "фев",
    3: "мар",
    4: "апр",
    5: "май",
    6: "июн",
    7: "июл",
    8: "авг",
    9: "сен",
    10: "окт",
    11: "ноя",
    12: "дек",
}


class RemoveStatus(Enum):
    REMOVED = "removed"
    NOT_FOUND = "not_found"
    FAILED = "failed"


def note_block_hash(block: list[str]) -> str:
    """Short fingerprint of a note block (the exact lines written to the file)."""
    return hashlib.sha256("\n".join(block).encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class NoteTarget:
    repo: str
    path: str
    branch: str

    def fingerprint(self) -> str:
        encoded = json.dumps(
            [self.repo, self.path, self.branch], ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:12]


@dataclass(frozen=True)
class NoteRef:
    operation_id: str
    block_hash: str
    target_hash: str

    def encode(self) -> str:
        return f"2:{self.operation_id}:{self.block_hash}:{self.target_hash}"


def parse_note_ref(value: str) -> NoteRef | None:
    match = re.fullmatch(r"2:([0-9a-f]{32}):([0-9a-f]{12}):([0-9a-f]{12})", value)
    return NoteRef(*match.groups()) if match else None


def operation_marker(operation_id: str, edge: str) -> str:
    return f"<!-- iwe-note:v2:{operation_id}:{edge} -->"


def find_note_operation(lines: list[str], operation_id: str) -> tuple[int, int] | None:
    """Unique start/end markers; edited, partial or ambiguous identities are never guessed."""
    starts = [
        i
        for i, line in enumerate(lines)
        if line == operation_marker(operation_id, "start")
    ]
    ends = [
        i
        for i, line in enumerate(lines)
        if line == operation_marker(operation_id, "end")
    ]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        return None
    return starts[0], ends[0] + 1


@dataclass(frozen=True)
class NoteRequest:
    user_id: int
    text: str
    source_label: str | None
    operation_id: str
    target: NoteTarget
    created_at: datetime
    bind_block_hash: Callable[[str], Awaitable[str]] | None = None


AfterAttempt = Callable[[dict | None, float], Awaitable[None]]
CanRetry = Callable[[], Awaitable[bool]]


@dataclass
class PendingNote:
    request: NoteRequest
    attempt: int
    after_attempt: AfterAttempt | None
    can_retry: CanRetry | None


class GitHubNotesClient:
    """Клиент для записи заметок в GitHub."""

    _MAX_RETRIES = 3

    def __init__(self):
        self._pending: list[PendingNote] = []

    @staticmethod
    def _extract_title(text: str, max_words: int = 7) -> tuple[str, str]:
        """Извлекает заголовок из первого предложения или первых 5 слов.

        Returns:
            (title, body) — body пустой, если текст короткий.
        """
        # Убираем начальную пунктуацию (. ! ? и пробелы)
        clean = text.lstrip(".!? ")
        if not clean:
            return text.strip() or "заметка", ""

        # Первое предложение (до . ! ? с пробелом или концом строки)
        match = re.search(r"[.!?](?:\s|$)", clean)
        if match:
            title = clean[: match.start() + 1].strip()
            if len(title.split()) <= max_words:
                body = clean[match.end() :].strip()
                return title, body

        # Fallback: первые 5 слов
        words = clean.split()
        if len(words) <= 5:
            return clean.strip(), ""
        title = " ".join(words[:5]) + "…"
        return title, clean.strip()

    @staticmethod
    def _format_note_lines(
        text: str, now: datetime, source_label: str | None = None
    ) -> list[str]:
        """Форматирует заметку как markdown-блок.

        Формат: ---\\n\\n**Title**\\n<sub>date[ · source]</sub>\\n\\nBody
        source_label (например «голос», «Siri») помечает заметку, надиктованную, а не набранную.
        """
        title, body = GitHubNotesClient._extract_title(text)

        day = now.day
        month = MONTHS_RU[now.month]
        time_str = now.strftime("%H:%M")
        date_str = f"{day} {month}, {time_str}"
        if source_label:
            date_str = f"{date_str} · {source_label}"

        lines = ["---", "", f"**{title}**", f"<sub>{date_str}</sub>"]
        if body:
            lines.extend(["", body])
        lines.append("")
        return "\n".join(lines).split("\n")

    @staticmethod
    def _find_insert_position(lines: list[str]) -> int:
        """Находит позицию для вставки новой заметки.

        Стратегия:
        1. После blockquote-описания (> ...), перед первым ---
        2. Fallback: после заголовка # + пустых строк
        """
        # Пропускаем YAML frontmatter
        start = 0
        if lines and lines[0].strip() == "---":
            for i in range(1, len(lines)):
                if lines[i].strip() == "---":
                    start = i + 1
                    break

        # Ищем заголовок #
        header_pos = None
        for i in range(start, len(lines)):
            if lines[i].startswith("# "):
                header_pos = i
                break

        if header_pos is None:
            return len(lines)

        # Пропускаем blockquote-описание после заголовка
        i = header_pos + 1
        found_blockquote = False
        while i < len(lines):
            stripped = lines[i].strip()
            if stripped.startswith(">"):
                found_blockquote = True
                i += 1
                continue
            if not stripped:
                i += 1
                continue
            # Непустая строка, не blockquote
            if found_blockquote and stripped == "---":
                return i  # вставляем перед этим ---
            if found_blockquote:
                return i
            break

        return min(len(lines), i)

    async def resolve_target(self, user_id: int) -> NoteTarget | None:
        repo = await github_oauth.get_target_repo(user_id)
        if not repo:
            return None
        return NoteTarget(
            repo,
            await github_oauth.get_notes_path(user_id),
            await github_oauth.get_default_branch(user_id),
        )

    async def append_note(
        self,
        telegram_user_id: int,
        text: str,
        source_label: str | None = None,
        *,
        operation_id: str | None = None,
        target: NoteTarget | None = None,
        created_at: datetime | None = None,
        bind_block_hash: Callable[[str], Awaitable[str]] | None = None,
        after_attempt: AfterAttempt | None = None,
        can_retry: CanRetry | None = None,
        queue_retry: bool = True,
    ) -> dict | None:
        """Capture the destination and identity once; all retries preserve them."""
        target = target or await self.resolve_target(telegram_user_id)
        if target is None:
            logger.warning("No target repo for user %s", telegram_user_id)
            return None
        operation_id = operation_id or uuid4().hex
        if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise ValueError("Invalid note operation id")
        if created_at is not None and created_at.utcoffset() is None:
            raise ValueError("Note timestamp must include its timezone")
        request = NoteRequest(
            telegram_user_id,
            text,
            source_label,
            operation_id,
            target,
            created_at.astimezone(MOSCOW_TZ)
            if created_at is not None
            else datetime.now(MOSCOW_TZ),
            bind_block_hash,
        )
        result = await self._write_note(request)
        if result is None and queue_retry:
            if not any(
                job.request.operation_id == operation_id for job in self._pending
            ):
                self._pending.append(PendingNote(request, 1, after_attempt, can_retry))
            logger.warning("Note queued for retry: %d chars", len(text))
        return result

    async def _write_note(self, request: NoteRequest) -> dict | None:
        """One attempt; no queue mutation and no destination re-resolution."""
        auth_ctx = await resolve_auth_context(request.user_id, OperationClass.WRITE)
        note_lines = [
            operation_marker(request.operation_id, "start"),
            *self._format_note_lines(
                request.text, request.created_at, request.source_label
            ),
            operation_marker(request.operation_id, "end"),
            "",
        ]
        target = request.target
        candidate_hash = note_block_hash(note_lines[:-1])
        expected_hash = (
            await request.bind_block_hash(candidate_hash)
            if request.bind_block_hash is not None
            else candidate_hash
        )
        if not re.fullmatch(r"[0-9a-f]{12}", expected_hash):
            raise ValueError("Invalid expected note block fingerprint")
        result = await self._append_to_file(
            auth_ctx=auth_ctx,
            repo=target.repo,
            path=target.path,
            note_lines=note_lines,
            commit_message="note: fleeting note saved",
            branch=target.branch,
            operation_id=request.operation_id,
            expected_block_hash=expected_hash,
        )
        if result is None:
            return None
        return {
            **result,
            "operation_id": request.operation_id,
            "target_hash": target.fingerprint(),
            "block_hash": expected_hash,
            "block_len": len(note_lines),
            "content_matches": result.get("stored_block_hash", candidate_hash)
            == expected_hash,
            "input_matches": candidate_hash == expected_hash,
        }

    async def _append_to_file(
        self,
        auth_ctx: AuthContext,
        repo: str,
        path: str,
        note_lines: list[str],
        commit_message: str,
        branch: str = "main",
        max_retries: int = 3,
        operation_id: str | None = None,
        expected_block_hash: str | None = None,
    ) -> dict | None:
        """Добавляет заметку в файл через Contents API с retry на 409.

        auth_ctx выбран ДО вызова (resolve_auth_context) и не меняется внутри
        операции — сбой этого токена НЕ откатывается на другой источник
        (WP-406 Ф22, peer-сессия раунд 2).
        """
        import aiohttp

        url = f"https://api.github.com/repos/{repo}/contents/{path}"
        headers = {
            "Authorization": auth_ctx.auth_header,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        line_start: int | None = None  # 1-based line number of first inserted note line

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
                            if operation_id is not None:
                                bounds = find_note_operation(lines, operation_id)
                                if bounds is not None:
                                    return {
                                        "repo": repo,
                                        "path": path,
                                        "branch": branch,
                                        "sha": current_sha,
                                        "line_start": bounds[0] + 1,
                                        "stored_block_hash": note_block_hash(
                                            lines[bounds[0] : bounds[1]]
                                        ),
                                    }
                                if any(
                                    operation_marker(operation_id, edge) in lines
                                    for edge in ("start", "end")
                                ):
                                    logger.error(
                                        "Note operation markers are partial or ambiguous; write refused"
                                    )
                                    return None
                            insert_pos = self._find_insert_position(lines)

                            line_start = (
                                insert_pos + 1
                            )  # 1-based line number of first note line

                            for j, note_line in enumerate(note_lines):
                                lines.insert(insert_pos + j, note_line)

                            updated_content = "\n".join(lines)
                        elif resp.status == 404:
                            current_sha = None
                            now_str = datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d")
                            header_lines = [
                                "---",
                                "type: inbox",
                                "status: active",
                                f"updated: {now_str}",
                                "---",
                                "",
                                "# Fleeting Notes",
                                "",
                            ]
                            header_str = "\n".join(header_lines) + "\n"
                            updated_content = header_str + "\n".join(note_lines)
                            line_start = header_str.count("\n") + 1  # 1-based
                        elif (
                            resp.status in (500, 502, 503, 504)
                            and attempt < max_retries - 1
                        ):
                            logger.warning(
                                f"GitHub GET {path}: {resp.status} transient, "
                                f"retry {attempt + 1}/{max_retries}"
                            )
                            import asyncio

                            await asyncio.sleep(1.0 * (attempt + 1))
                            continue
                        else:
                            error = await resp.text()
                            logger.error(
                                f"GitHub GET {path}: {resp.status} - {error[:200]}"
                            )
                            return None

                    if (
                        expected_block_hash is not None
                        and expected_block_hash != note_block_hash(note_lines[:-1])
                    ):
                        logger.warning(
                            "Changed retry input cannot create a new note block"
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
                                "branch": branch,
                                "sha": result.get("content", {}).get("sha", ""),
                                "line_start": line_start,
                            }
                        elif resp.status == 409 and attempt < max_retries - 1:
                            logger.warning(
                                f"SHA conflict on {path}, "
                                f"retry {attempt + 1}/{max_retries}"
                            )
                            continue
                        elif (
                            resp.status in (500, 502, 503, 504)
                            and attempt < max_retries - 1
                        ):
                            logger.warning(
                                f"GitHub PUT {path}: {resp.status} transient, "
                                f"retry {attempt + 1}/{max_retries}"
                            )
                            import asyncio

                            await asyncio.sleep(1.0 * (attempt + 1))
                            continue
                        else:
                            error = await resp.text()
                            logger.error(
                                f"GitHub PUT {path}: {resp.status} - {error[:200]}"
                            )
                            return None

            except Exception as e:  # noqa: BLE001 — network boundary returns a retriable result
                logger.error(
                    f"GitHub append_note exception "
                    f"(attempt {attempt + 1}): {type(e).__name__}"
                )
                if attempt < max_retries - 1:
                    continue
                return None

        return None

    async def remove_note_block(
        self, telegram_user_id: int, ref: NoteRef
    ) -> RemoveStatus:
        """Delete an unchanged operation only in the destination bound to its button.

        Legacy content-only references are deliberately not accepted: after an identical
        note disappears they cannot prove which remaining note the old button meant.
        """
        target = await self.resolve_target(telegram_user_id)
        if target is None:
            return RemoveStatus.FAILED
        if not isinstance(ref, NoteRef) or target.fingerprint() != ref.target_hash:
            return RemoveStatus.NOT_FOUND
        auth_ctx = await resolve_auth_context(telegram_user_id, OperationClass.WRITE)
        for _attempt in range(self._MAX_RETRIES):
            fetched = await self._fetch_file(
                auth_ctx, target.repo, target.path, target.branch
            )
            if fetched is None:
                return RemoveStatus.FAILED
            content, sha = fetched
            lines = content.split("\n")
            bounds = find_note_operation(lines, ref.operation_id)
            if bounds is None:
                return RemoveStatus.NOT_FOUND
            start, end = bounds
            if note_block_hash(lines[start:end]) != ref.block_hash:
                return RemoveStatus.NOT_FOUND
            if end < len(lines) and lines[end] == "":
                end += 1
            del lines[start:end]
            outcome = await self._put_file(
                auth_ctx,
                target.repo,
                target.path,
                target.branch,
                "\n".join(lines),
                sha,
                "undo: note removed",
            )
            if outcome == "ok":
                return RemoveStatus.REMOVED
            if outcome == "error":
                return RemoveStatus.FAILED
        return RemoveStatus.FAILED

    @staticmethod
    def _api_headers(auth_ctx: AuthContext) -> dict:
        return {
            "Authorization": auth_ctx.auth_header,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _fetch_file(
        self, auth_ctx: AuthContext, repo: str, path: str, branch: str
    ) -> tuple[str, str] | None:
        """(content, sha) of a file through the Contents API, or None on any failure."""
        import aiohttp

        url = f"https://api.github.com/repos/{repo}/contents/{path}"
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url,
                    headers=self._api_headers(auth_ctx),
                    params={"ref": branch},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp,
            ):
                if resp.status != 200:
                    logger.error(f"GitHub GET {path}: {resp.status}")
                    return None
                data = await resp.json()
            return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]
        except Exception as exc:  # noqa: BLE001 — Contents API failures map to an explicit failure
            logger.error(f"GitHub GET {path} failed: {type(exc).__name__}")
            return None

    async def _put_file(
        self,
        auth_ctx: AuthContext,
        repo: str,
        path: str,
        branch: str,
        content: str,
        sha: str,
        message: str,
    ) -> str:
        """Write a file through the Contents API: "ok", "conflict" (SHA moved) or "error"."""
        import aiohttp

        url = f"https://api.github.com/repos/{repo}/contents/{path}"
        body = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
            "sha": sha,
        }
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.put(
                    url,
                    headers=self._api_headers(auth_ctx),
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp,
            ):
                if resp.status in (200, 201):
                    return "ok"
                if resp.status == 409:
                    return "conflict"
                logger.error(f"GitHub PUT {path}: {resp.status}")
                return "error"
        except Exception as exc:  # noqa: BLE001 — Contents API failures map to an explicit failure
            logger.error(f"GitHub PUT {path} failed: {type(exc).__name__}")
            return "error"

    async def retry_pending(self):
        """Retry each captured operation once, using the caller's common completion."""
        import time

        pending, self._pending = self._pending, []
        for job in pending:
            started = time.monotonic()
            try:
                if job.can_retry is not None and not await job.can_retry():
                    logger.info(
                        "Note retry dropped: notes access is no longer available"
                    )
                    continue
                result = await self._write_note(job.request)
            except GitHubAuthUnavailable:
                logger.warning("Note retry dropped: authorization unavailable")
                continue
            except Exception as exc:  # noqa: BLE001 — one failed job must not discard the remaining queue
                logger.warning("Note retry failed: %s", type(exc).__name__)
                result = None
            if job.after_attempt is not None:
                try:
                    await job.after_attempt(result, started)
                except Exception as exc:  # noqa: BLE001 — completion is best-effort after the write
                    logger.warning(
                        "Note completion callback failed: %s", type(exc).__name__
                    )
            if result is not None:
                logger.info("Note retry succeeded")
            elif job.attempt < self._MAX_RETRIES:
                job.attempt += 1
                self._pending.append(job)
            else:
                logger.error("Note dropped after %d retries", self._MAX_RETRIES)

    async def clear_notes(self, telegram_user_id: int) -> bool:
        """Очищает файл заметок (сохраняет шапку с описанием).

        Raises:
            GitHubAuthUnavailable: нет валидного источника авторизации для
            записи (WP-406 Ф22) — caller обязан показать понятную инструкцию.
        """
        import aiohttp

        repo = await github_oauth.get_target_repo(telegram_user_id)
        if not repo:
            return False

        path = await github_oauth.get_notes_path(telegram_user_id)
        auth_ctx = await resolve_auth_context(telegram_user_id, OperationClass.WRITE)
        branch = await github_oauth.get_default_branch(telegram_user_id)

        url = f"https://api.github.com/repos/{repo}/contents/{path}"
        headers = {
            "Authorization": auth_ctx.auth_header,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    params={"ref": branch},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return False
                    file_data = await resp.json()
                    current_sha = file_data["sha"]
                    current_content = base64.b64decode(file_data["content"]).decode(
                        "utf-8"
                    )

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
                        "branch": branch,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status in (200, 201):
                        logger.info(f"Notes cleared: {repo}/{path}")
                        return True
                    return False

        except Exception as e:  # noqa: BLE001 — network boundary maps failures to False
            logger.error("GitHub clear_notes exception: %s", type(e).__name__)
            return False


# Singleton instance
github_notes = GitHubNotesClient()
