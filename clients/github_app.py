"""GitHub App клиент (WP-301 Ф7).

Аутентификация GitHub App для записи assignments в репозитории пилотов.

Архитектура (DP.SC.020 v3):
- Платформа регистрирует один App «Aisystant Personal Guide»
- Пилот устанавливает App на свой `<user>/DS-personal-guide` репо (1 клик)
- App получает installation_id, write permission на contents, push event subscription
- Платформа пишет `assignments/YYYY-MM-DD.md` через installation token
- Пилот пишет `workbook/YYYY-MM-DD.md` сам → push → webhook → платформа

Env-vars:
- GITHUB_APP_ID: numeric ID (е.g. "1234567")
- GITHUB_APP_PRIVATE_KEY: PEM-encoded RSA private key (multiline)
- GITHUB_APP_SLUG: для install URL (е.g. "aisystant-personal-guide")
- GITHUB_APP_WEBHOOK_SECRET: HMAC для App webhook (отличается от per-repo GITHUB_WORKBOOK_WEBHOOK_SECRET)

JWT auth: RS256 подпись App-private-key, exp ≤10 мин (GitHub требование).
Installation token: exchange JWT → short-lived token (~1h), не нужно хранить долго.
"""

import base64
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp
import jwt

from config import get_logger

logger = get_logger(__name__)

GITHUB_API = "https://api.github.com"
JWT_EXPIRY_SECONDS = 540  # 9 min, < 10 min GitHub limit
TOKEN_CACHE: dict[int, tuple[str, float]] = {}  # installation_id → (token, expires_at_unix)

# WP-406: известный чужой App ID (DS-MCP/github-integration-service,
# aisystant-knowledge), по ошибке взятый при регистрации РП-301 —
# report.md сессии 2026-09-10-19-wp406-fix-github-app-slug. Переопределяемо
# через env на случай новых находок; не единственная линия защиты — см.
# verify_app_identity() ниже (сетевая проверка identity через сам GitHub API).
_FORBIDDEN_APP_IDS_DEFAULT = "3261992"

# Three-state: None = ещё не проверялось при старте, True/False = результат
# последней verify_app_identity(). Читается гейтами трёх входных точек как
# вторая линия защиты поверх GITHUB_APP_ENABLED.
_app_identity_verified: Optional[bool] = None


def is_app_enabled() -> bool:
    """GITHUB_APP_ENABLED — общий флаг фичи (WP-406). Default: выключено."""
    return os.getenv("GITHUB_APP_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def app_identity_status() -> Optional[bool]:
    """Результат последней verify_app_identity() при старте (три состояния)."""
    return _app_identity_verified


def _forbidden_app_ids() -> set[str]:
    raw = os.getenv("GITHUB_APP_FORBIDDEN_IDS", _FORBIDDEN_APP_IDS_DEFAULT)
    return {x.strip() for x in raw.split(",") if x.strip()}


def _load_app_credentials() -> tuple[str, str]:
    """Прочитать APP_ID + PRIVATE_KEY из env. Кидает RuntimeError если нет
    или если APP_ID — известный чужой App (WP-406 fail-fast tripwire)."""
    app_id = os.getenv("GITHUB_APP_ID", "").strip()
    private_key = os.getenv("GITHUB_APP_PRIVATE_KEY", "").strip()
    if not app_id or not private_key:
        raise RuntimeError(
            "GITHUB_APP_ID и/или GITHUB_APP_PRIVATE_KEY не установлены. "
            "См. WP-301 Ф7 инструкцию по регистрации App."
        )
    if app_id in _forbidden_app_ids():
        logger.warning(
            "[GitHubApp] gate=forbidden_id app_id=%s — настроен известный чужой App, отказ",
            app_id,
        )
        raise RuntimeError(
            f"GITHUB_APP_ID={app_id} входит в список запрещённых (GITHUB_APP_FORBIDDEN_IDS) — "
            "это ID чужого приложения, не платформы. См. WP-406 (report.md сессии "
            "2026-09-10-19-wp406-fix-github-app-slug)."
        )
    # PEM может прийти из env с экранированными \n или с raw newlines
    if "\\n" in private_key and "\n" not in private_key:
        private_key = private_key.replace("\\n", "\n")
    return app_id, private_key


async def verify_app_identity(timeout: int = 5) -> bool:
    """Best-effort сверка настроенного App с реальным GitHub API (WP-406 п.3).

    GET /app с App JWT, сверяет id (и slug, если задан в env) с ответом.
    Не бросает исключений — любая ошибка (сеть, таймаут, mismatch) → False.
    Обновляет module-level `_app_identity_verified` для трёх входных точек.
    """
    global _app_identity_verified
    try:
        app_id, _ = _load_app_credentials()
    except RuntimeError as e:
        logger.warning("[GitHubApp] gate=identity_check_skipped reason=%s", e)
        _app_identity_verified = False
        return False

    try:
        app_jwt = generate_app_jwt()
        headers = {
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{GITHUB_API}/app", headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "[GitHubApp] gate=identity_check_failed app_id=%s status=%d body=%s",
                        app_id, resp.status, body[:200],
                    )
                    _app_identity_verified = False
                    return False
                data = await resp.json()
    except Exception as e:
        logger.warning(
            "[GitHubApp] gate=identity_check_failed app_id=%s reason=%s: %s",
            app_id, type(e).__name__, e,
        )
        _app_identity_verified = False
        return False

    remote_id = str(data.get("id", "")).strip()
    remote_slug = str(data.get("slug", "")).strip()
    expected_slug = os.getenv("GITHUB_APP_SLUG", "").strip()

    if remote_id != app_id:
        logger.warning(
            "[GitHubApp] gate=identity_mismatch expected_id=%s remote_id=%s remote_slug=%s",
            app_id, remote_id, remote_slug,
        )
        _app_identity_verified = False
        return False
    if expected_slug and remote_slug != expected_slug:
        logger.warning(
            "[GitHubApp] gate=identity_mismatch app_id=%s expected_slug=%s remote_slug=%s",
            app_id, expected_slug, remote_slug,
        )
        _app_identity_verified = False
        return False

    logger.info("[GitHubApp] identity verified app_id=%s slug=%s", app_id, remote_slug)
    _app_identity_verified = True
    return True


async def verify_installation_belongs_to_app(installation_id: int, timeout: int = 5) -> bool:
    """Проверяет, что installation_id принадлежит настроенному в env App (WP-406 п.4).

    GET /app/installations/{id} с App JWT. Fail-closed: любая ошибка (сеть,
    таймаут, не-200, mismatch) → False — вызывающий код обязан отказать в
    сохранении установки, не считать отсутствие ответа успехом.
    """
    try:
        app_id, _ = _load_app_credentials()
    except RuntimeError as e:
        logger.warning(
            "[GitHubApp] gate=ownership_check_skipped installation_id=%d reason=%s",
            installation_id, e,
        )
        return False

    try:
        app_jwt = generate_app_jwt()
        headers = {
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"{GITHUB_API}/app/installations/{installation_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "[GitHubApp] gate=ownership_check_failed installation_id=%d app_id=%s "
                        "status=%d body=%s",
                        installation_id, app_id, resp.status, body[:200],
                    )
                    return False
                data = await resp.json()
    except Exception as e:
        logger.warning(
            "[GitHubApp] gate=ownership_check_failed installation_id=%d app_id=%s reason=%s: %s",
            installation_id, app_id, type(e).__name__, e,
        )
        return False

    remote_app_id = str(data.get("app_id", "")).strip()
    if remote_app_id != app_id:
        logger.warning(
            "[GitHubApp] gate=ownership_mismatch installation_id=%d expected_app_id=%s "
            "remote_app_id=%s",
            installation_id, app_id, remote_app_id,
        )
        return False
    return True


def generate_app_jwt() -> str:
    """JWT для аутентификации App (НЕ installation).

    Используется для вызова /app/installations/{id}/access_tokens
    и /app/installations (list).
    """
    app_id, private_key = _load_app_credentials()
    now = int(time.time())
    payload = {
        "iat": now - 30,           # backdate 30s для clock skew
        "exp": now + JWT_EXPIRY_SECONDS,
        "iss": app_id,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


async def get_installation_token(installation_id: int) -> Optional[str]:
    """Получить short-lived installation access token (~1h TTL).

    Кэшируется в TOKEN_CACHE до истечения (минус 60s safety margin).
    Возвращает None при сбое (логирует ошибку).
    """
    cached = TOKEN_CACHE.get(installation_id)
    if cached:
        token, exp = cached
        if exp - 60 > time.time():
            return token

    try:
        app_jwt = generate_app_jwt()
    except RuntimeError as e:
        logger.error("[GitHubApp] generate_app_jwt failed: %s", e)
        return None

    url = f"{GITHUB_API}/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, timeout=15) as resp:
                if resp.status != 201:
                    body = await resp.text()
                    logger.error(
                        "[GitHubApp] installation_token failed %d: %s (installation_id=%d)",
                        resp.status, body[:200], installation_id,
                    )
                    return None
                data = await resp.json()
                token = data["token"]
                # expires_at: ISO8601 UTC, например "2026-05-11T17:00:00Z"
                # Простой парсинг — берём now + 3540s (59 мин) safety, не парсим строку
                exp_unix = time.time() + 3540
                TOKEN_CACHE[installation_id] = (token, exp_unix)
                return token
    except Exception as e:
        logger.error("[GitHubApp] installation_token exception: %s", e)
        return None


@dataclass
class WriteResult:
    success: bool
    sha: Optional[str] = None
    error: Optional[str] = None


async def write_file(
    installation_id: int,
    repo_full_name: str,
    path: str,
    content: str,
    message: str,
    branch: str = "main",
) -> WriteResult:
    """Записать (создать/обновить) файл в репозитории через installation token.

    Args:
        installation_id: GitHub installation ID
        repo_full_name: "owner/repo"
        path: путь файла в репо ("assignments/2026-05-12.md")
        content: содержимое (UTF-8 текст)
        message: commit message
        branch: ветка (default: "main")

    Returns:
        WriteResult с sha коммита или ошибкой.
    """
    token = await get_installation_token(installation_id)
    if not token:
        return WriteResult(success=False, error="no installation token")

    url = f"{GITHUB_API}/repos/{repo_full_name}/contents/{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Сначала пробуем GET — если файл существует, нужен sha для update
    existing_sha = None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params={"ref": branch}, timeout=10) as r:
                if r.status == 200:
                    existing_sha = (await r.json()).get("sha")
                elif r.status not in (404,):
                    logger.warning("[GitHubApp] GET file unexpected status %d", r.status)
    except Exception as e:
        logger.warning("[GitHubApp] GET file exception: %s", e)

    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if existing_sha:
        body["sha"] = existing_sha

    try:
        async with aiohttp.ClientSession() as session:
            async with session.put(url, headers=headers, json=body, timeout=20) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    logger.error(
                        "[GitHubApp] PUT %s failed %d: %s",
                        path, resp.status, text[:200],
                    )
                    return WriteResult(success=False, error=f"http {resp.status}")
                data = await resp.json()
                sha = data.get("commit", {}).get("sha")
                logger.info("[GitHubApp] wrote %s/%s sha=%s", repo_full_name, path, sha)
                return WriteResult(success=True, sha=sha)
    except Exception as e:
        logger.error("[GitHubApp] PUT exception: %s", e)
        return WriteResult(success=False, error=str(e))


async def get_installation_repos(installation_id: int) -> list[dict]:
    """Список репо, к которым у App есть доступ через эту установку.

    Используется для определения, что пилот установил App на правильный репо.
    """
    token = await get_installation_token(installation_id)
    if not token:
        return []
    url = f"{GITHUB_API}/installation/repositories"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("repositories", []) or []
    except Exception as e:
        logger.warning("[GitHubApp] list repos failed: %s", e)
        return []


async def set_repo_private(installation_id: int, repo_full_name: str) -> bool:
    """Переключает репозиторий на приватный через installation token.

    Требует permission "Administration: write" у App — без него GitHub
    вернёт 403 и функция вернёт False. Вызывающий код обязан явно
    предупредить пользователя при False, не считать успех по умолчанию
    (WP-527: 6 из 11 проверенных `personal-guide`-репо оказались публичными
    после ручного создания пользователем через install-flow GitHub App).
    """
    token = await get_installation_token(installation_id)
    if not token:
        return False
    url = f"{GITHUB_API}/repos/{repo_full_name}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.patch(url, headers=headers, json={"private": True}, timeout=10) as resp:
                if resp.status == 200:
                    return True
                body = await resp.text()
                logger.warning(
                    "[GitHubApp] set_repo_private failed for %s: status=%d body=%s",
                    repo_full_name, resp.status, body[:300],
                )
                return False
    except Exception as e:
        logger.warning("[GitHubApp] set_repo_private failed for %s: %s", repo_full_name, e)
        return False
