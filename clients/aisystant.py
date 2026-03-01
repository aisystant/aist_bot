"""
Aisystant LMS API клиент — расписание, оплата, подписки, привязка аккаунта.

Портирован из SystemsSchool_bot. Используется для:
- Привязка Telegram ↔ Aisystant аккаунта
- Проверка подписки «Бесконечное развитие» (определяет T2)
- Каталог потоков (курсы, семинары, резидентуры)
- Оплата курсов и подписки через Aisystant Payment Gateway
- Расписание занятий пользователя

Использование:
    from clients.aisystant import aisystant

    # Привязка
    aisystant_id = await aisystant.find_user_by_tg(tg_user_id)
    link = await aisystant.get_link_url(tg_user_id, tg_username)

    # Подписка
    is_active = await aisystant.has_active_subscription(aisystant_id)

    # Каталог
    courses = await aisystant.get_available_courses()

    # Оплата
    payment = await aisystant.create_internship_payment(aisystant_id, code, amount)
"""

import hashlib
import json
import re
import uuid
from datetime import date, datetime
from typing import Any

import aiohttp
from config import get_logger

logger = get_logger(__name__)

# Классификация потоков по программам (WP-79 UX)
PROGRAM_RULES: list[tuple[str, Any]] = [
    ("seminars", lambda p: "семинар" in p.get("courseName", "").lower()),
    ("reviews", lambda p: "разбор" in p.get("courseName", "").lower()),
    ("professional", lambda p: bool(re.match(r"^R\d", p.get("code", "")))),
    ("personal", lambda p: True),  # default — всё остальное
]

EXCLUDE_CODES = ["Urspectr"]


class AisystantClient:
    """HTTP-клиент для Aisystant LMS REST API."""

    _session: aiohttp.ClientSession | None = None

    def __init__(self, base_url: str, tech_password: str):
        self.base_url = base_url.rstrip("/")
        self._auth = aiohttp.BasicAuth("tech@aisystant.com", tech_password)
        self._subscription_cache: dict[str, tuple[bool, float]] = {}  # aisystant_id -> (result, expires_at)
        self._cache_ttl = 300  # 5 минут

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    async def _get(self, path: str, params: dict | None = None) -> dict | list | None:
        """GET-запрос к Aisystant API."""
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        try:
            async with session.get(url, auth=self._auth, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"Aisystant GET {path} error {resp.status}: {text[:200]}")
                    return None
                text = await resp.text()
                return json.loads(text) if text else None
        except Exception as e:
            logger.error(f"Aisystant GET {path} exception: {e}")
            return None

    async def _post(self, path: str, params: dict | None = None,
                    body: dict | None = None) -> dict | list | None:
        """POST-запрос к Aisystant API."""
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        try:
            async with session.post(url, auth=self._auth, params=params, json=body) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"Aisystant POST {path} error {resp.status}: {text[:200]}")
                    return None
                text = await resp.text()
                return json.loads(text) if text else None
        except Exception as e:
            logger.error(f"Aisystant POST {path} exception: {e}")
            return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Привязка аккаунта ──────────────────────────────────

    async def find_user_by_tg(self, tg_user_id: int) -> str | None:
        """Найти aisystant_id по Telegram ID. Возвращает ID или None."""
        year = datetime.now().year
        msg = f"{tg_user_id}+{year}"
        signature = hashlib.sha256(msg.encode("utf-8")).hexdigest()
        result = await self._get(
            "/api/profile/find-by-tg",
            params={"tg-id": str(tg_user_id), "s": signature},
        )
        if result and isinstance(result, dict) and result.get("id"):
            return str(result["id"])
        return None

    async def get_link_url(self, tg_user_id: int, tg_username: str | None = None) -> str:
        """Получить URL для привязки Telegram к Aisystant."""
        params: dict[str, str] = {"tg-id": str(tg_user_id)}
        if tg_username:
            params["tg-username"] = tg_username
        result = await self._post("/api/profile/new-tg-link", params=params)
        if result and isinstance(result, dict) and result.get("path"):
            path = result["path"]
            return f"{self.base_url}/lk/#/auth/tgattach?path={path}"
        return f"{self.base_url}/lk/#/auth/tgattach"

    # ── Подписка ───────────────────────────────────────────

    async def has_active_subscription(self, aisystant_id: str) -> bool:
        """Проверить активность подписки «Бесконечное развитие». Кэш 5 мин."""
        import time
        now = time.time()

        # Проверяем кэш
        cached = self._subscription_cache.get(aisystant_id)
        if cached and cached[1] > now:
            return cached[0]

        result = await self._get(
            "/api/subscriptions/active-subscription",
            params={"user-id": aisystant_id},
        )
        is_active = result is not None and bool(result)

        # Сохраняем в кэш
        self._subscription_cache[aisystant_id] = (is_active, now + self._cache_ttl)
        return is_active

    def invalidate_subscription_cache(self, aisystant_id: str):
        """Сбросить кэш подписки (после покупки)."""
        self._subscription_cache.pop(aisystant_id, None)

    async def get_subscription_tariffs(self, aisystant_id: str,
                                        purpose: str = "") -> list[dict]:
        """Получить доступные тарифы подписки."""
        params: dict[str, str] = {"user-id": aisystant_id}
        if purpose:
            params["purpose"] = purpose
        result = await self._get("/api/subscriptions/active-tariffs", params=params)
        return result if isinstance(result, list) else []

    async def get_subscription_status(self, aisystant_id: str) -> dict | None:
        """Получить текущий статус подписки (тариф, дата окончания)."""
        return await self._get(
            "/api/subscriptions/active-subscription",
            params={"user-id": aisystant_id},
        )

    async def get_subscription_status_by_purpose(
        self, aisystant_id: str, purpose: str,
    ) -> dict | None:
        """Получить статус подписки по purpose (WORKSHOP, INTERNSHIP и т.д.)."""
        return await self._get(
            "/api/subscriptions/active-subscription",
            params={"user-id": aisystant_id, "purpose": purpose},
        )

    # ── Каталог потоков ────────────────────────────────────

    async def get_all_potoki(self) -> list[dict]:
        """Получить все потоки из CRM."""
        result = await self._get("/api/crm/potok")
        return result if isinstance(result, list) else []

    async def get_available_courses(self) -> list[dict]:
        """Получить потоки со статусом 'Продается'/'Onboarding', отфильтрованные.

        Возвращает отсортированный список с добавленным полем 'program'.
        """
        all_potoki = await self.get_all_potoki()
        today = date.today()
        courses = []

        for potok in all_potoki:
            # Фильтр по статусу
            if potok.get("status") not in ("Onboarding", "Продается"):
                continue
            # Исключения по коду
            code = potok.get("code", "")
            if any(exc in code for exc in EXCLUDE_CODES):
                continue
            # Фильтр по дате старта
            started = potok.get("started", "")
            if started:
                try:
                    start_date = datetime.strptime(started, "%Y-%m-%d").date()
                    if start_date < today:
                        continue
                except ValueError:
                    pass

            # Классификация по программе
            program = "personal"
            for prog_name, rule in PROGRAM_RULES:
                if rule(potok):
                    program = prog_name
                    break

            potok["program"] = program
            courses.append(potok)

        courses.sort(key=lambda p: p.get("started", ""))
        return courses

    async def get_available_internships(self, aisystant_id: str) -> list[dict]:
        """Получить курсы, доступные для покупки (с ценами)."""
        result = await self._get(
            "/api/subscriptions/active-internships",
            params={"user-id": aisystant_id},
        )
        return result if isinstance(result, list) else []

    # ── Курсы пользователя ─────────────────────────────────

    async def get_user_courses(self, aisystant_id: str) -> list[dict]:
        """Получить курсы, на которые записан пользователь.

        Фильтрует закрытые/отменённые.
        """
        result = await self._get(
            "/api/crm/crm-course-passings",
            params={"user-id": aisystant_id},
        )
        if not isinstance(result, list):
            return []

        excluded_statuses = {"Закрыт", "Отменен", "Завершил обучение"}
        return [
            p for p in result
            if p.get("potok", {}).get("status") not in excluded_statuses
        ]

    async def get_user_lessons(self, aisystant_id: str) -> list[dict]:
        """Получить ближайшие занятия пользователя."""
        result = await self._get(
            "/api/crm/potok-lessons",
            params={"user-id": aisystant_id},
        )
        if not isinstance(result, list):
            return []

        today = date.today()
        excluded_statuses = {"Закрыт", "Отменен", "Завершил обучение"}
        lessons = []

        for lesson in result:
            potok_status = lesson.get("potok", {}).get("status", "")
            if potok_status in excluded_statuses:
                continue
            lesson_dt = lesson.get("lesson", {}).get("datetime", "")
            if lesson_dt:
                try:
                    lesson_date = datetime.fromisoformat(lesson_dt).date()
                    if lesson_date < today:
                        continue
                except ValueError:
                    pass
            lessons.append(lesson)

        lessons.sort(key=lambda l: l.get("lesson", {}).get("datetime", ""))
        return lessons

    # ── Оплата ─────────────────────────────────────────────

    async def create_internship_payment(
        self, aisystant_id: str, code: str, amount: float,
        payment_index: int | None = None,
    ) -> dict | None:
        """Создать платёж за курс/семинар.

        Возвращает {'confirmationUrl': ..., 'id': ...} или None.
        """
        request_id = str(uuid.uuid4())
        body = {
            "code": code,
            "amount": amount,
            "autopay": False,
            "paymentIndex": payment_index,
        }
        return await self._post(
            "/api/subscriptions/create-internship-payment",
            params={"request-id": request_id, "user-id": aisystant_id},
            body=body,
        )

    async def create_subscription_payment(
        self, aisystant_id: str, code: str, amount: float,
        purpose: str = "",
    ) -> dict | None:
        """Создать платёж за подписку.

        Возвращает {'confirmationUrl': ..., 'id': ...} или None.
        """
        request_id = str(uuid.uuid4())
        body = {
            "code": code,
            "amount": amount,
            "autopay": True,
        }
        params: dict[str, str] = {
            "request-id": request_id,
            "user-id": aisystant_id,
        }
        if purpose:
            params["purpose"] = purpose
        return await self._post(
            "/api/subscriptions/create-subscription-payment",
            params=params,
            body=body,
        )

    async def check_payment(self, payment_id: str, aisystant_id: str) -> dict | None:
        """Проверить статус оплаты."""
        return await self._post(
            "/api/subscriptions/check-payment",
            params={"id": payment_id, "user-id": aisystant_id},
        )

    # ── Телеграм-группы ────────────────────────────────────

    async def approve_chat_join(self, body: dict) -> bool:
        """Одобрить вступление в группу Telegram."""
        result = await self._post("/tg/approve-chat-join-request", body=body)
        return result is not None

    async def log_action(self, body: dict):
        """Записать действие в лог Aisystant."""
        await self._post("/tg/log", body=body)


# ── Синглтон ───────────────────────────────────────────

import os

_base_url = os.getenv("AISYSTANT_BASE_URL", "https://aisystant.system-school.ru")
_tech_password = os.getenv("AISYSTANT_TECH_PASSWORD", "")

aisystant = AisystantClient(_base_url, _tech_password)
