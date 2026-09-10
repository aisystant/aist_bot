from __future__ import annotations

"""
Единая точка выбора источника авторизации для записи в GitHub (WP-406 Ф22).

Проблема (WP-458 ВЫ-13): /github шёл через OAuth scope "repo" — доступ ко всем
репозиториям аккаунта. Решение ArchGate — GitHub App с
repository_selection=selected. Переходный период: часть пользователей ещё на
OAuth (льготный период), часть уже на App.

Правило (peer-сессия 2026-09-10-08, раунд 2): один запрос — один источник
авторизации, выбранный ДО начала операции. Внутри операции источник не
переключается — сбой installation token не откатывается на OAuth молча (это
обошло бы repository_selection=selected).
"""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from config import get_logger

logger = get_logger(__name__)


class OperationClass(Enum):
    """Классифицирует вызывающий use case, не HTTP-метод (peer-сессия раунд 2)."""

    READ = "read"
    WRITE = "write"


class GitHubAuthUnavailable(Exception):
    """Нет валидного источника авторизации для этой операции.

    Причины: нет ни App-установки, ни OAuth; либо OAuth есть, но льготный
    период для WRITE истёк. Caller обязан показать пользователю понятную
    инструкцию, не тихий сбой.
    """


@dataclass(frozen=True)
class AuthContext:
    """Immutable на время одной операции. Не пересобирать внутри операции."""

    source: str  # "app" | "oauth"
    token: str
    installation_id: Optional[int] = None

    @property
    def auth_header(self) -> str:
        """Формат заголовка, уже проверенный в проде для каждого источника."""
        if self.source == "app":
            return f"token {self.token}"
        return f"Bearer {self.token}"


def _grace_deadline() -> Optional[datetime]:
    """Дедлайн льготного периода для OAuth WRITE (14 дней, решение пилота 09.08).

    Задаётся явной календарной датой при деплое (env, формат YYYY-MM-DD), не
    per-user таймером — не требует нового столбца/нуджа для корректности
    самого гейта. Пустое значение = грейс-период отключён (WRITE через OAuth
    разрешён всегда, поведение до Ф22).

    Дата включительна: `GITHUB_APP_OAUTH_GRACE_UNTIL=2026-09-24` разрешает
    WRITE весь день 24 сентября (UTC), запрет наступает с 00:00 UTC 25
    сентября — иначе `datetime.fromisoformat("2026-09-24")` даёт полночь ТОГО
    ЖЕ дня, и запрет наступал бы на ~сутки раньше, чем ожидает читающий дату
    пилот (код-ревью peer-сессии 2026-09-10-08, Medium).
    """
    raw = os.getenv("GITHUB_APP_OAUTH_GRACE_UNTIL", "").strip()
    if not raw:
        return None
    try:
        # Только календарная дата (см. docstring) — время суток в значении
        # игнорируется, иначе "включительно до конца дня" ниже съезжает на
        # часы вперёд/назад в зависимости от того, что кто-то дописал.
        deadline_day = datetime.fromisoformat(raw).date()
    except ValueError:
        logger.error("[GitHubAuth] GITHUB_APP_OAUTH_GRACE_UNTIL некорректна: %r", raw)
        return None
    midnight = datetime.combine(deadline_day, datetime.min.time(), tzinfo=timezone.utc)
    return midnight + timedelta(days=1)


async def resolve_auth_context(
    telegram_user_id: int, operation: OperationClass
) -> AuthContext:
    """Выбрать ОДИН источник авторизации для всей операции.

    Приоритет: активная (не suspended) App-установка побеждает всегда.
    OAuth — только если App-установки нет, и для WRITE — только внутри
    льготного периода.

    Raises:
        GitHubAuthUnavailable: нет валидного источника для operation.
    """
    from clients.github_app import get_installation_token
    from clients.github_oauth import github_oauth
    from db.queries.github_app import get_app_installation

    installation = await get_app_installation(telegram_user_id)
    if installation and not installation.get("app_suspended"):
        installation_id = installation["app_installation_id"]
        token = await get_installation_token(installation_id)
        if not token:
            # Установка активна, но токен не выдан (transient GitHub-сбой) —
            # НЕ откатываемся на OAuth: это обошло бы repository_selection.
            logger.error(
                "[GitHubAuth] installation token недоступен: chat_id=%d, installation_id=%d",
                telegram_user_id, installation_id,
            )
            raise GitHubAuthUnavailable(
                "installation_token_unavailable"
            )
        return AuthContext(source="app", token=token, installation_id=installation_id)

    oauth_token = await github_oauth.get_access_token(telegram_user_id)
    if not oauth_token:
        raise GitHubAuthUnavailable("no_connection")

    if operation is OperationClass.READ:
        return AuthContext(source="oauth", token=oauth_token)

    deadline = _grace_deadline()
    if deadline and datetime.now(timezone.utc) >= deadline:
        logger.info(
            "[GitHubAuth] OAuth WRITE запрещена после grace-периода: chat_id=%d, deadline=%s",
            telegram_user_id, deadline.date().isoformat(),
        )
        raise GitHubAuthUnavailable("oauth_grace_expired")

    return AuthContext(source="oauth", token=oauth_token)


async def get_repo_default_branch(telegram_user_id: int, repo_full_name: str) -> str:
    """default_branch репозитория через любой доступный источник (READ).

    App-only пользователи (нет OAuth-токена) не могут пройти через
    GitHubOAuthClient.api_request() — та молча падает без OAuth-токена и
    отдаёт "main". Здесь тот же READ-приоритет, что и в resolve_auth_context,
    поэтому App-only пользователь с нестандартной default-веткой не ловит
    404 при первой же записи заметки.
    """
    import aiohttp

    try:
        auth_ctx = await resolve_auth_context(telegram_user_id, OperationClass.READ)
    except GitHubAuthUnavailable:
        return "main"

    url = f"https://api.github.com/repos/{repo_full_name}"
    headers = {
        "Authorization": auth_ctx.auth_header,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("default_branch", "main")
    except Exception as e:
        logger.warning("[GitHubAuth] get_repo_default_branch(%s) failed: %s", repo_full_name, e)
    return "main"
