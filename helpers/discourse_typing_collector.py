"""
WP-327 Phase 3б: server-side Discourse typing collector.

Подход: вместо JS-инъекции в браузер — опрашиваем Discourse API ежедневно,
читаем raw-текст постов, эмитим user_typing_tracked события.

Идемпотентность: UNIQUE(source, external_id) в domain_event.
external_id = "typing-discourse-{post_id}" — глобально уникален.

Lookback: LOOKBACK_DAYS последних дней (повторная обработка безопасна,
дубли отброшены gateway).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import aiohttp

logger = logging.getLogger(__name__)

CHANNEL_WEIGHT_DISCOURSE = 1.0  # обдуманный текст → полный вес
LOOKBACK_DAYS = 3               # окно опроса; дубли = idempotent no-op
MIN_CHARS = 20                  # тот же порог, что в TG-трекере
_DISCOURSE_FILTERS = (4, 5)     # 4=новые топики, 5=реплики


async def _fetch_post_ids_since(
    session: aiohttp.ClientSession,
    base_url: str,
    api_key: str,
    username: str,
    since: datetime,
) -> dict[int, datetime]:
    """Вернуть {post_id: created_at} для постов username после since."""
    headers = {"Api-Key": api_key, "Api-Username": "system"}
    posts: dict[int, datetime] = {}

    for f in _DISCOURSE_FILTERS:
        offset = 0
        while True:
            url = f"{base_url}/user_actions.json?username={username}&filter={f}&offset={offset}"
            try:
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        break
                    data = await resp.json()
            except Exception as exc:
                logger.warning("[discourse_collector] actions fetch error: %s", exc)
                break

            actions = data.get("user_actions", [])
            if not actions:
                break

            stop = False
            for a in actions:
                raw_ts = a.get("created_at", "")
                try:
                    ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue
                if ts < since:
                    stop = True
                    break
                post_id = a.get("post_id")
                if post_id:
                    posts.setdefault(post_id, ts)

            if stop or len(actions) < 30:
                break

            offset += 30
            await asyncio.sleep(0.1)

    return posts


async def _fetch_raw(
    session: aiohttp.ClientSession,
    base_url: str,
    api_key: str,
    post_id: int,
) -> str | None:
    """Получить raw markdown текст поста по ID."""
    url = f"{base_url}/posts/{post_id}.json"
    try:
        async with session.get(
            url,
            headers={"Api-Key": api_key, "Api-Username": "system"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return data.get("raw")
    except Exception as exc:
        logger.warning("[discourse_collector] post %d fetch error: %s", post_id, exc)
        return None


async def collect_discourse_typing() -> None:
    """Основная точка входа — вызывается из scheduler ежедневно."""
    import os
    from db.queries.discourse import get_all_discourse_accounts
    from helpers.dual_write import post_event, resolve_ory_id_from_chat
    from db.queries.consent import is_typing_tracking_disabled

    base_url = os.getenv("DISCOURSE_API_URL", "").rstrip("/")
    api_key = os.getenv("DISCOURSE_API_KEY", "")

    if not base_url or not api_key:
        logger.info("[discourse_collector] env not set, skip")
        return

    since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    accounts = await get_all_discourse_accounts()
    if not accounts:
        logger.info("[discourse_collector] no linked accounts")
        return

    logger.info("[discourse_collector] start: %d accounts, lookback=%dd",
                len(accounts), LOOKBACK_DAYS)
    emitted = 0

    async with aiohttp.ClientSession() as session:
        for acc in accounts:
            chat_id: int = acc["chat_id"]
            username: str | None = acc.get("discourse_username")
            if not username:
                continue

            account_id = await resolve_ory_id_from_chat(chat_id)
            if not account_id:
                continue

            try:
                if await is_typing_tracking_disabled(account_id):
                    continue
            except Exception:
                pass  # fail-open

            post_map = await _fetch_post_ids_since(
                session, base_url, api_key, username, since
            )

            for post_id, created_at in post_map.items():
                raw = await _fetch_raw(session, base_url, api_key, post_id)
                if not raw or len(raw) < MIN_CHARS:
                    continue

                char_count = len(raw)
                weighted = round(char_count * CHANNEL_WEIGHT_DISCOURSE, 2)

                await post_event(
                    source="aist-bot",
                    external_id=f"typing-discourse-{post_id}",
                    event_type="user_typing_tracked",
                    schema_version="v1",
                    occurred_at=created_at.replace(tzinfo=None),  # naive UTC
                    account_id=account_id,
                    payload={
                        "interface": "discourse",
                        "date": created_at.date().isoformat(),
                        "char_count": char_count,
                        "weighted_chars": weighted,
                    },
                )
                emitted += 1
                await asyncio.sleep(0.05)  # ~20 req/s к Discourse

            await asyncio.sleep(0.3)  # пауза между пользователями

    logger.info("[discourse_collector] done: %d events emitted", emitted)
