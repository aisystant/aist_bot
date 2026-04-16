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
    tokens = await google_calendar_oauth.exchange_code(code, telegram_user_id)

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

    async def get_authorization_url(self, telegram_user_id: int) -> Tuple[str, str]:
        """Генерирует URL для OAuth авторизации Google Calendar."""
        if not self.client_id:
            raise ValueError("GOOGLE_CALENDAR_CLIENT_ID not configured")

        state = secrets.token_urlsafe(32)

        from db.queries.oauth_states import save_oauth_state
        await save_oauth_state(state, "google_calendar", telegram_user_id)

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

    async def validate_state(self, state: str) -> Optional[int]:
        """Проверяет state и возвращает telegram_user_id."""
        from db.queries.oauth_states import validate_oauth_state
        telegram_user_id = await validate_oauth_state(state)
        if not telegram_user_id:
            logger.warning(f"Invalid or expired Google Calendar state: {state[:10]}...")
        return telegram_user_id

    async def exchange_code(self, code: str, telegram_user_id: int) -> Optional[Dict[str, Any]]:
        """Обменивает authorization code на access_token + refresh_token.

        Args:
            code: Authorization code из OAuth callback.
            telegram_user_id: ID пользователя (уже проверен через validate_state в callback handler).
        """

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

    async def get_week_events(self, telegram_user_id: int) -> Optional[List[Dict[str, Any]]]:
        """Получает события на 7 дней вперёд (МСК)."""
        now_msk = datetime.now(MOSCOW_TZ)
        start = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)

        return await self.get_events(
            telegram_user_id,
            time_min=start.isoformat(),
            time_max=end.isoformat(),
        )

    async def get_calendar_list(self, telegram_user_id: int) -> Optional[List[Dict[str, Any]]]:
        """Получает список всех календарей пользователя."""
        access_token = await self.get_access_token(telegram_user_id)
        if not access_token:
            return None

        url = f"{GOOGLE_CALENDAR_API}/users/me/calendarList"

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
                        logger.error(f"Google Calendar list failed: {resp.status} - {error}")
                        return None
        except Exception as e:
            logger.error(f"Google Calendar list exception: {e}")
            return None

    async def _get_events_from_calendar(
        self,
        access_token: str,
        calendar_id: str,
        time_min: str,
        time_max: str,
        max_results: int = 20,
    ) -> List[Dict[str, Any]]:
        """Получает события из одного календаря."""
        params = {
            "timeMin": time_min,
            "timeMax": time_max,
            "maxResults": max_results,
            "singleEvents": "true",
            "orderBy": "startTime",
        }

        encoded_id = quote(calendar_id, safe='')
        url = f"{GOOGLE_CALENDAR_API}/calendars/{encoded_id}/events?{urlencode(params)}"

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
                        logger.warning(f"Calendar {calendar_id}: {resp.status}")
                        return []
        except Exception as e:
            logger.warning(f"Calendar {calendar_id} exception: {e}")
            return []

    async def get_events(
        self,
        telegram_user_id: int,
        time_min: str,
        time_max: str,
        max_results: int = 50,
    ) -> Optional[List[Dict[str, Any]]]:
        """Получает события из ВСЕХ календарей пользователя."""
        access_token = await self.get_access_token(telegram_user_id)
        if not access_token:
            return None

        # Получаем список календарей
        calendars = await self.get_calendar_list(telegram_user_id)
        if calendars is None:
            return None

        # Собираем события из всех календарей
        all_events = []
        for cal in calendars:
            cal_id = cal.get("id")
            if not cal_id:
                continue
            # Пропускаем праздники и дни рождения (шум)
            if cal.get("accessRole") == "reader" and "#holiday" in cal_id:
                continue
            if "birthdaysCalendar" in (cal.get("id") or ""):
                continue

            events = await self._get_events_from_calendar(
                access_token, cal_id, time_min, time_max, max_results=max_results,
            )
            for ev in events:
                ev["_calendar_summary"] = cal.get("summary", "")
            all_events.extend(events)

        # Сортируем по времени начала
        def sort_key(ev):
            start = ev.get("start", {})
            return start.get("dateTime", start.get("date", ""))

        all_events.sort(key=sort_key)
        return all_events

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
        cal_name = ev.get("_calendar_summary", "")
        cal_suffix = f" <i>({cal_name})</i>" if cal_name else ""
        lines.append(f"• {time_str} — {summary}{cal_suffix}")

    lines.append(f"\n<i>Всего: {len(events)}</i>")
    return "\n".join(lines)


DAY_NAMES_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTH_NAMES_RU = ["", "янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def format_week_events_message(events: List[Dict[str, Any]]) -> str:
    """Форматирует события за 7 дней, группируя по дням."""
    if not events:
        return "<b>7 дней</b>\n\nСобытий нет."

    # Группируем по дате
    days: Dict[str, List[Dict[str, Any]]] = {}
    for ev in events:
        start = ev.get("start", {})
        dt_str = start.get("dateTime", start.get("date", ""))
        if not dt_str:
            continue
        try:
            dt = datetime.fromisoformat(dt_str)
            date_key = dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        days.setdefault(date_key, []).append(ev)

    lines = ["<b>7 дней</b>\n"]
    for date_key in sorted(days.keys()):
        dt = datetime.strptime(date_key, "%Y-%m-%d")
        day_name = DAY_NAMES_RU[dt.weekday()]
        month_name = MONTH_NAMES_RU[dt.month]
        lines.append(f"\n<b>{dt.day} {month_name} ({day_name})</b>")
        for ev in days[date_key]:
            time_str = format_event_time(ev)
            summary = ev.get("summary", "Без названия")
            cal_name = ev.get("_calendar_summary", "")
            cal_suffix = f" <i>({cal_name})</i>" if cal_name else ""
            lines.append(f"  • {time_str} — {summary}{cal_suffix}")

    lines.append(f"\n<i>Всего: {len(events)}</i>")
    return "\n".join(lines)


# Singleton
google_calendar_oauth = GoogleCalendarOAuthClient()
