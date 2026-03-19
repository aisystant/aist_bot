"""
Google Calendar OAuth клиент — интеграция для просмотра событий.

Использует OAuth 2.0 Authorization Code Flow с offline access (refresh_token).
Scope calendar.readonly — только чтение событий.

Токены хранятся в PostgreSQL (таблица google_calendar_connections).
In-memory кеш ускоряет повторные обращения.

Использование:
    from clients.google_calendar_oauth import google_calendar_oauth

    # Получить URL для авторизации
    auth_url, state = google_calendar_oauth.get_authorization_url(telegram_user_id=123456)

    # После callback обменять code на токены
    tokens = await google_calendar_oauth.exchange_code(code, state)

    # Получить события на сегодня
    events = await google_calendar_oauth.get_today_events(telegram_user_id=123456)
"""

import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, quote

import aiohttp

from config import (
    GOOGLE_CALENDAR_CLIENT_ID,
    GOOGLE_CALENDAR_CLIENT_SECRET,
    GOOGLE_CALENDAR_REDIRECT_URI,
    MOSCOW_TZ,
    get_logger,
)

logger = get_logger(__name__)

# Google OAuth endpoints
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"

# Только чтение — минимальный scope
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


class GoogleCalendarOAuthClient:
    """OAuth клиент для Google Calendar API.

    Реализует Authorization Code Flow с refresh_token:
    1. Генерация URL авторизации (access_type=offline)
    2. Обмен code на access_token + refresh_token
    3. Автоматическое обновление access_token через refresh_token
    4. Вызовы Google Calendar API (REST)
    """

    def __init__(self):
        self.client_id = GOOGLE_CALENDAR_CLIENT_ID
        self.client_secret = GOOGLE_CALENDAR_CLIENT_SECRET
        self.redirect_uri = GOOGLE_CALENDAR_REDIRECT_URI

        # state -> {telegram_user_id, created_at} (TTL 10 мин)
        self._pending_states: Dict[str, Dict[str, Any]] = {}

        # telegram_user_id -> cached token data (in-memory)
        self._cache: Dict[int, Dict[str, Any]] = {}

    async def _load_from_db(self, telegram_user_id: int) -> Optional[Dict[str, Any]]:
        """Загружает подключение из БД в кеш."""
        from db.queries.google_calendar import get_calendar_connection

        row = await get_calendar_connection(telegram_user_id)
        if row:
            self._cache[telegram_user_id] = {
                "access_token": row["access_token"],
                "refresh_token": row["refresh_token"],
                "expires_at": row["expires_at"].timestamp() if row.get("expires_at") else 0,
                "email": row.get("email"),
            }
            return self._cache[telegram_user_id]
        return None

    async def _get_cached(self, telegram_user_id: int) -> Optional[Dict[str, Any]]:
        """Возвращает данные из кеша или загружает из БД."""
        if telegram_user_id in self._cache:
            return self._cache[telegram_user_id]
        return await self._load_from_db(telegram_user_id)

    def get_authorization_url(self, telegram_user_id: int) -> Tuple[str, str]:
        """Генерирует URL для OAuth авторизации Google Calendar."""
        if not self.client_id:
            raise ValueError("GOOGLE_CALENDAR_CLIENT_ID not configured")

        state = secrets.token_urlsafe(32)

        self._pending_states[state] = {
            "telegram_user_id": telegram_user_id,
            "created_at": time.time(),
        }

        self._cleanup_old_states()

        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(GOOGLE_SCOPES),
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }

        auth_url = f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"
        logger.info(f"Generated Google Calendar auth URL for user {telegram_user_id}")

        return auth_url, state

    def _cleanup_old_states(self):
        """Удаляет просроченные states (>10 мин)."""
        now = time.time()
        expired = [
            state
            for state, data in self._pending_states.items()
            if now - data["created_at"] > 600
        ]
        for state in expired:
            del self._pending_states[state]

    def validate_state(self, state: str) -> Optional[int]:
        """Проверяет state и возвращает telegram_user_id."""
        data = self._pending_states.get(state)
        if not data:
            logger.warning(f"Invalid or expired Google Calendar state: {state[:10]}...")
            return None

        if time.time() - data["created_at"] > 600:
            del self._pending_states[state]
            logger.warning(f"Expired Google Calendar state: {state[:10]}...")
            return None

        return data["telegram_user_id"]

    async def exchange_code(self, code: str, state: str) -> Optional[Dict[str, Any]]:
        """Обменивает authorization code на access_token + refresh_token."""
        telegram_user_id = self.validate_state(state)
        if not telegram_user_id:
            return None

        del self._pending_states[state]

        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    GOOGLE_TOKEN_URL,
                    data=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        tokens = await resp.json()

                        if "error" in tokens:
                            logger.error(f"Google token error: {tokens['error']}")
                            return None

                        access_token = tokens.get("access_token")
                        refresh_token = tokens.get("refresh_token")
                        expires_in = tokens.get("expires_in", 3600)
                        expires_at = time.time() + expires_in

                        # Получаем email пользователя через calendar API
                        email = await self._get_user_email(access_token)

                        # Кешируем
                        self._cache[telegram_user_id] = {
                            "access_token": access_token,
                            "refresh_token": refresh_token,
                            "expires_at": expires_at,
                            "email": email,
                        }

                        # Сохраняем в БД
                        from db.queries.google_calendar import save_calendar_connection

                        await save_calendar_connection(
                            chat_id=telegram_user_id,
                            access_token=access_token,
                            refresh_token=refresh_token,
                            expires_at=datetime.utcnow() + timedelta(seconds=expires_in),
                            email=email,
                        )

                        logger.info(
                            f"Successfully exchanged Google Calendar code for user {telegram_user_id} ({email})"
                        )
                        return self._cache[telegram_user_id]
                    else:
                        error = await resp.text()
                        logger.error(
                            f"Google token exchange failed: {resp.status} - {error}"
                        )
                        return None

        except Exception as e:
            logger.error(f"Google token exchange exception: {e}")
            return None

    async def _get_user_email(self, access_token: str) -> Optional[str]:
        """Получает email пользователя из Google Calendar settings."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{GOOGLE_CALENDAR_API}/calendars/primary",
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("id")  # primary calendar id = email
        except Exception:
            pass
        return None

    async def get_access_token(self, telegram_user_id: int) -> Optional[str]:
        """Возвращает access_token, автоматически обновляя при необходимости."""
        data = await self._get_cached(telegram_user_id)
        if not data:
            return None

        # Обновляем за 60 сек до истечения
        if data.get("expires_at", 0) < time.time() + 60:
            refreshed = await self._refresh_access_token(
                telegram_user_id, data.get("refresh_token")
            )
            if not refreshed:
                return None
            data = self._cache.get(telegram_user_id)

        return data.get("access_token") if data else None

    async def _refresh_access_token(
        self, telegram_user_id: int, refresh_token: str
    ) -> bool:
        """Обновляет access_token через refresh_token."""
        if not refresh_token:
            logger.warning(f"No refresh token for user {telegram_user_id}")
            return False

        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    GOOGLE_TOKEN_URL,
                    data=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        tokens = await resp.json()
                        new_access = tokens.get("access_token")
                        expires_in = tokens.get("expires_in", 3600)
                        expires_at = time.time() + expires_in

                        self._cache[telegram_user_id] = {
                            "access_token": new_access,
                            "refresh_token": tokens.get("refresh_token", refresh_token),
                            "expires_at": expires_at,
                            "email": self._cache.get(telegram_user_id, {}).get("email"),
                        }

                        from db.queries.google_calendar import update_calendar_tokens

                        await update_calendar_tokens(
                            chat_id=telegram_user_id,
                            access_token=new_access,
                            refresh_token=tokens.get("refresh_token", refresh_token),
                            expires_at=datetime.utcnow() + timedelta(seconds=expires_in),
                        )

                        logger.info(f"Refreshed Google Calendar token for user {telegram_user_id}")
                        return True
                    else:
                        error = await resp.text()
                        logger.error(
                            f"Google Calendar token refresh failed for {telegram_user_id}: {resp.status} - {error}"
                        )
                        return False

        except Exception as e:
            logger.error(f"Google Calendar token refresh exception: {e}")
            return False

    async def is_connected(self, telegram_user_id: int) -> bool:
        """Проверяет, подключён ли пользователь к Google Calendar."""
        data = await self._get_cached(telegram_user_id)
        return data is not None

    async def get_today_events(self, telegram_user_id: int) -> Optional[List[Dict[str, Any]]]:
        """Получает события на сегодня (МСК)."""
        now_msk = datetime.now(MOSCOW_TZ)
        start = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)

        return await self.get_events(
            telegram_user_id,
            time_min=start.isoformat(),
            time_max=end.isoformat(),
        )

    async def get_tomorrow_events(self, telegram_user_id: int) -> Optional[List[Dict[str, Any]]]:
        """Получает события на завтра (МСК)."""
        now_msk = datetime.now(MOSCOW_TZ)
        start = (now_msk + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)

        return await self.get_events(
            telegram_user_id,
            time_min=start.isoformat(),
            time_max=end.isoformat(),
        )

    async def get_events(
        self,
        telegram_user_id: int,
        time_min: str,
        time_max: str,
        max_results: int = 20,
    ) -> Optional[List[Dict[str, Any]]]:
        """Получает события из primary календаря."""
        access_token = await self.get_access_token(telegram_user_id)
        if not access_token:
            return None

        params = {
            "timeMin": time_min,
            "timeMax": time_max,
            "maxResults": max_results,
            "singleEvents": "true",
            "orderBy": "startTime",
        }

        url = f"{GOOGLE_CALENDAR_API}/calendars/primary/events?{urlencode(params)}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("items", [])
                    else:
                        error = await resp.text()
                        logger.error(
                            f"Google Calendar API failed for {telegram_user_id}: {resp.status} - {error}"
                        )
                        return None

        except Exception as e:
            logger.error(f"Google Calendar API exception: {e}")
            return None

    async def disconnect(self, telegram_user_id: int):
        """Отключает пользователя от Google Calendar."""
        # Отзываем токен у Google
        data = await self._get_cached(telegram_user_id)
        if data and data.get("access_token"):
            try:
                async with aiohttp.ClientSession() as session:
                    await session.post(
                        f"https://oauth2.googleapis.com/revoke?token={data['access_token']}",
                        timeout=aiohttp.ClientTimeout(total=5),
                    )
            except Exception as e:
                logger.warning(f"Failed to revoke Google token: {e}")

        if telegram_user_id in self._cache:
            del self._cache[telegram_user_id]

        from db.queries.google_calendar import delete_calendar_connection

        await delete_calendar_connection(telegram_user_id)
        logger.info(f"Disconnected user {telegram_user_id} from Google Calendar")


def format_event_time(event: Dict[str, Any]) -> str:
    """Форматирует время события для отображения в Telegram."""
    start = event.get("start", {})
    end = event.get("end", {})

    # Полнодневное событие
    if "date" in start:
        return "Весь день"

    start_dt = start.get("dateTime", "")
    end_dt = end.get("dateTime", "")

    if start_dt and end_dt:
        try:
            s = datetime.fromisoformat(start_dt)
            e = datetime.fromisoformat(end_dt)
            # Переводим в МСК
            s_msk = s.astimezone(MOSCOW_TZ)
            e_msk = e.astimezone(MOSCOW_TZ)
            duration = e - s
            mins = int(duration.total_seconds() / 60)
            if mins >= 60:
                h, m = divmod(mins, 60)
                dur_str = f"{h}ч" if m == 0 else f"{h}ч {m}мин"
            else:
                dur_str = f"{mins}мин"
            return f"{s_msk.strftime('%H:%M')}–{e_msk.strftime('%H:%M')} ({dur_str})"
        except (ValueError, TypeError):
            pass

    return ""


def format_events_message(events: List[Dict[str, Any]], title: str = "Сегодня") -> str:
    """Форматирует список событий в HTML для Telegram."""
    if not events:
        return f"<b>{title}</b>\n\nСобытий нет."

    lines = [f"<b>{title}</b>\n"]
    for ev in events:
        time_str = format_event_time(ev)
        summary = ev.get("summary", "Без названия")
        lines.append(f"• {time_str} — {summary}")

    return "\n".join(lines)


# Singleton
google_calendar_oauth = GoogleCalendarOAuthClient()
