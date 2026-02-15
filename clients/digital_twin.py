"""
Клиент Digital Twin MCP Server с OAuth2 авторизацией.

Digital Twin хранит состояние пользователя по метамодели Aisystant:
- Степени (degrees): Student, Specialist, Master, etc.
- Ступени (stages): Preparing, Practicing, etc.
- Индикаторы (indicators): цели, роли, время, прогресс

Endpoint: https://digital-twin-mcp.aisystant.workers.dev/mcp
Протокол: JSON-RPC 2.0 + OAuth2 Authorization Code (PKCE)

Использование:
    from clients.digital_twin import digital_twin

    # OAuth: получить URL для авторизации
    auth_url, state = digital_twin.get_authorization_url(telegram_user_id=123456)

    # OAuth: обменять code на токены (после callback)
    tokens = await digital_twin.exchange_code(code, code_verifier, telegram_user_id)

    # Читать данные (требует авторизации)
    data = await digital_twin.read("indicators.IND.1.PREF.objective", telegram_user_id=123456)
"""

import asyncio
import base64
import hashlib
import json
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import aiohttp

from config import DIGITAL_TWIN_MCP_URL, get_logger

logger = get_logger(__name__)

# OAuth2 endpoints (relative to base)
DT_BASE = DIGITAL_TWIN_MCP_URL.rstrip("/mcp")  # https://digital-twin-mcp.aisystant.workers.dev
DT_AUTHORIZE_URL = f"{DT_BASE}/authorize"
DT_TOKEN_URL = f"{DT_BASE}/token"

# Зарегистрированный OAuth client_id
DT_CLIENT_ID = "8b2b906a0de7eee6b00db44cd076c2fc"
DT_REDIRECT_URI = "https://aistmebot-production.up.railway.app/auth/twin/callback"


class DigitalTwinClient:
    """Клиент для работы с Digital Twin MCP Server.

    Совмещает OAuth2 PKCE авторизацию и JSON-RPC вызовы.
    Включает circuit breaker для graceful degradation.
    """

    # Таймауты
    DEFAULT_TIMEOUT = 10  # секунд
    MAX_RETRIES = 1

    # Circuit breaker
    FAILURE_THRESHOLD = 2
    RECOVERY_TIME = 60

    _circuit_state: Dict[str, Any] = {}

    def __init__(self, url: str = DIGITAL_TWIN_MCP_URL):
        self.base_url = url
        self.name = "DigitalTwin"
        self._request_id = 0

        # OAuth: state -> {telegram_user_id, code_verifier, created_at}
        self._pending_states: Dict[str, Dict[str, Any]] = {}

        # OAuth: telegram_user_id -> {access_token, refresh_token, expires_at}
        self._tokens: Dict[int, Dict[str, Any]] = {}

        if url not in DigitalTwinClient._circuit_state:
            DigitalTwinClient._circuit_state[url] = {
                "failures": 0,
                "last_failure": 0,
                "open": False
            }

    # =========================================================================
    # OAuth2 PKCE
    # =========================================================================

    @staticmethod
    def _generate_pkce() -> Tuple[str, str]:
        """Генерирует code_verifier и code_challenge (S256)."""
        code_verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return code_verifier, code_challenge

    def get_authorization_url(self, telegram_user_id: int) -> Tuple[str, str]:
        """Генерирует URL для OAuth авторизации с PKCE.

        Args:
            telegram_user_id: ID пользователя Telegram

        Returns:
            Tuple[auth_url, state]
        """
        state = secrets.token_urlsafe(32)
        code_verifier, code_challenge = self._generate_pkce()

        self._pending_states[state] = {
            "telegram_user_id": telegram_user_id,
            "code_verifier": code_verifier,
            "created_at": time.time()
        }
        self._cleanup_old_states()

        params = {
            "client_id": DT_CLIENT_ID,
            "redirect_uri": DT_REDIRECT_URI,
            "response_type": "code",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "scope": "openid offline_access",
        }

        auth_url = f"{DT_AUTHORIZE_URL}?{urlencode(params)}"
        logger.info(f"Generated DT auth URL for user {telegram_user_id}")
        return auth_url, state

    def _cleanup_old_states(self):
        """Удаляет просроченные states (10 минут)."""
        now = time.time()
        expired = [s for s, d in self._pending_states.items() if now - d["created_at"] > 600]
        for s in expired:
            del self._pending_states[s]

    def validate_state(self, state: str) -> Optional[int]:
        """Проверяет state и возвращает telegram_user_id."""
        data = self._pending_states.get(state)
        if not data:
            return None
        if time.time() - data["created_at"] > 600:
            del self._pending_states[state]
            return None
        return data["telegram_user_id"]

    def get_code_verifier(self, state: str) -> Optional[str]:
        """Возвращает code_verifier для state."""
        data = self._pending_states.get(state)
        return data["code_verifier"] if data else None

    async def exchange_code(self, code: str, state: str) -> Optional[Dict[str, Any]]:
        """Обменивает authorization code на access_token (с PKCE).

        Args:
            code: Authorization code из callback
            state: State для верификации

        Returns:
            Токены или None при ошибке
        """
        data = self._pending_states.get(state)
        if not data:
            logger.warning("Invalid or expired state for DT OAuth")
            return None

        telegram_user_id = data["telegram_user_id"]
        code_verifier = data["code_verifier"]

        # Удаляем использованный state
        del self._pending_states[state]

        payload = {
            "grant_type": "authorization_code",
            "client_id": DT_CLIENT_ID,
            "redirect_uri": DT_REDIRECT_URI,
            "code": code,
            "code_verifier": code_verifier,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    DT_TOKEN_URL,
                    data=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        tokens = await resp.json()
                        self._tokens[telegram_user_id] = {
                            "access_token": tokens.get("access_token"),
                            "refresh_token": tokens.get("refresh_token"),
                            "expires_at": time.time() + tokens.get("expires_in", 3600),
                            "created_at": time.time(),
                        }
                        logger.info(f"DT OAuth: user {telegram_user_id} connected")
                        return self._tokens[telegram_user_id]
                    else:
                        error = await resp.text()
                        logger.error(f"DT token exchange failed: {resp.status} - {error}")
                        return None
        except Exception as e:
            logger.error(f"DT token exchange exception: {e}")
            return None

    async def _refresh_token(self, telegram_user_id: int) -> bool:
        """Обновляет access_token через refresh_token."""
        token_data = self._tokens.get(telegram_user_id)
        if not token_data or not token_data.get("refresh_token"):
            return False

        payload = {
            "grant_type": "refresh_token",
            "client_id": DT_CLIENT_ID,
            "refresh_token": token_data["refresh_token"],
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    DT_TOKEN_URL,
                    data=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        tokens = await resp.json()
                        self._tokens[telegram_user_id] = {
                            "access_token": tokens.get("access_token"),
                            "refresh_token": tokens.get("refresh_token", token_data["refresh_token"]),
                            "expires_at": time.time() + tokens.get("expires_in", 3600),
                            "created_at": time.time(),
                        }
                        logger.info(f"DT OAuth: refreshed token for user {telegram_user_id}")
                        return True
                    else:
                        logger.error(f"DT token refresh failed: {resp.status}")
                        return False
        except Exception as e:
            logger.error(f"DT token refresh exception: {e}")
            return False

    def get_access_token(self, telegram_user_id: int) -> Optional[str]:
        """Возвращает access_token для пользователя."""
        token_data = self._tokens.get(telegram_user_id)
        if token_data:
            return token_data.get("access_token")
        return None

    def is_connected(self, telegram_user_id: int) -> bool:
        """Проверяет, авторизован ли пользователь в Digital Twin."""
        return telegram_user_id in self._tokens

    def disconnect(self, telegram_user_id: int):
        """Отключает пользователя от Digital Twin."""
        if telegram_user_id in self._tokens:
            del self._tokens[telegram_user_id]
            logger.info(f"DT: disconnected user {telegram_user_id}")

    # =========================================================================
    # Circuit breaker
    # =========================================================================

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _is_circuit_open(self) -> bool:
        state = DigitalTwinClient._circuit_state[self.base_url]
        if not state["open"]:
            return False
        if time.time() - state["last_failure"] > self.RECOVERY_TIME:
            logger.info(f"{self.name}: circuit breaker half-open, trying to recover")
            return False
        return True

    def _record_failure(self):
        state = DigitalTwinClient._circuit_state[self.base_url]
        state["failures"] += 1
        state["last_failure"] = time.time()
        if state["failures"] >= self.FAILURE_THRESHOLD:
            state["open"] = True
            logger.warning(f"{self.name}: circuit breaker OPEN")

    def _record_success(self):
        state = DigitalTwinClient._circuit_state[self.base_url]
        if state["failures"] > 0 or state["open"]:
            logger.info(f"{self.name}: circuit breaker CLOSED")
        state["failures"] = 0
        state["open"] = False

    # =========================================================================
    # JSON-RPC вызовы (с Bearer token)
    # =========================================================================

    async def _call(self, tool: str, args: Dict[str, Any], telegram_user_id: Optional[int] = None) -> Optional[Any]:
        """Вызов инструмента Digital Twin MCP с Bearer авторизацией.

        Args:
            tool: имя инструмента
            args: аргументы
            telegram_user_id: ID пользователя для Bearer token

        Returns:
            Результат или None при ошибке
        """
        if self._is_circuit_open():
            logger.debug(f"{self.name}: circuit breaker open, skipping")
            return None

        # Собираем заголовки
        headers = {"Content-Type": "application/json"}
        if telegram_user_id is not None:
            token = self.get_access_token(telegram_user_id)
            if token:
                headers["Authorization"] = f"Bearer {token}"

        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": args
            }
        }

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        self.base_url,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=self.DEFAULT_TIMEOUT)
                    ) as resp:
                        # Token expired — try refresh
                        if resp.status == 401 and telegram_user_id and attempt == 0:
                            refreshed = await self._refresh_token(telegram_user_id)
                            if refreshed:
                                token = self.get_access_token(telegram_user_id)
                                if token:
                                    headers["Authorization"] = f"Bearer {token}"
                                continue
                            else:
                                self.disconnect(telegram_user_id)
                                logger.warning(f"{self.name}: token expired, user disconnected")
                                return None

                        if resp.status == 200:
                            data = await resp.json()
                            if "error" in data:
                                error_msg = data["error"].get("message", str(data["error"]))
                                logger.error(f"{self.name} error: {error_msg}")
                                self._record_failure()
                                return None
                            if "result" in data:
                                self._record_success()
                                content = data["result"].get("content", [])
                                if content and len(content) > 0:
                                    text = content[0].get("text", "")
                                    try:
                                        return json.loads(text)
                                    except json.JSONDecodeError:
                                        return text
                                return data["result"]
                        else:
                            logger.error(f"{self.name} HTTP {resp.status}")
                            self._record_failure()
            except asyncio.TimeoutError:
                if attempt < self.MAX_RETRIES:
                    logger.warning(f"{self.name} timeout, retry {attempt + 1}/{self.MAX_RETRIES}")
                    await asyncio.sleep(1)
                    continue
                self._record_failure()
            except Exception as e:
                logger.error(f"{self.name} exception: {e}")
                self._record_failure()
                return None

        logger.warning(f"{self.name}: unavailable, continuing without Digital Twin")
        return None

    # =========================================================================
    # МЕТАМОДЕЛЬ (справочники) — не требуют user auth
    # =========================================================================

    async def get_degrees(self, telegram_user_id: Optional[int] = None) -> Optional[List[Dict]]:
        """Получить все степени квалификации."""
        return await self._call("get_degrees", {}, telegram_user_id)

    async def get_stages(self, degree: str = "Student", telegram_user_id: Optional[int] = None) -> Optional[List[Dict]]:
        """Получить ступени внутри степени."""
        return await self._call("get_stages", {"degree": degree}, telegram_user_id)

    async def get_indicator_groups(self, telegram_user_id: Optional[int] = None) -> Optional[List[Dict]]:
        """Получить группы индикаторов."""
        return await self._call("get_indicator_groups", {}, telegram_user_id)

    async def get_indicators(self, group: Optional[str] = None, for_prompts: Optional[bool] = None, telegram_user_id: Optional[int] = None) -> Optional[List[Dict]]:
        """Получить индикаторы метамодели."""
        args: Dict[str, Any] = {}
        if group:
            args["group"] = group
        if for_prompts is not None:
            args["for_prompts"] = for_prompts
        return await self._call("get_indicators", args, telegram_user_id)

    async def get_indicator(self, code: str, telegram_user_id: Optional[int] = None) -> Optional[Dict]:
        """Получить один индикатор по коду."""
        return await self._call("get_indicator", {"code": code}, telegram_user_id)

    async def get_stage_thresholds(self, indicator_code: str, telegram_user_id: Optional[int] = None) -> Optional[List[Dict]]:
        """Получить пороги ступеней для индикатора."""
        return await self._call("get_stage_thresholds", {"indicator_code": indicator_code}, telegram_user_id)

    async def validate_value(self, indicator_code: str, value: Any, telegram_user_id: Optional[int] = None) -> Optional[Dict]:
        """Валидация значения индикатора."""
        return await self._call("validate_value", {"indicator_code": indicator_code, "value": value}, telegram_user_id)

    # =========================================================================
    # ДАННЫЕ ПОЛЬЗОВАТЕЛЯ (требуют auth)
    # =========================================================================

    async def read(self, path: str, telegram_user_id: int) -> Optional[Any]:
        """Читать данные Digital Twin по пути.

        Args:
            path: путь к данным (пустая строка = весь twin)
            telegram_user_id: ID пользователя Telegram (int)
        """
        result = await self._call("read_digital_twin", {"path": path}, telegram_user_id)
        logger.debug(f"{self.name}: read({path}, {telegram_user_id}) = {result}")
        return result

    async def write(self, path: str, data: Any, telegram_user_id: int) -> Optional[Dict]:
        """Записать данные в Digital Twin."""
        result = await self._call("write_digital_twin", {"path": path, "data": data}, telegram_user_id)
        logger.info(f"{self.name}: write({path}, {telegram_user_id}) = {result}")
        return result

    async def list_users(self, telegram_user_id: Optional[int] = None) -> Optional[List[str]]:
        """Получить список всех пользователей."""
        return await self._call("list_users", {}, telegram_user_id)

    # =========================================================================
    # УДОБНЫЕ МЕТОДЫ ДЛЯ БОТА
    # =========================================================================

    async def get_user_profile(self, telegram_user_id: int) -> Optional[Dict]:
        """Получить полный профиль пользователя."""
        return await self.read("", telegram_user_id)

    async def get_learning_objective(self, telegram_user_id: int) -> Optional[str]:
        """Получить цель обучения пользователя."""
        return await self.read("indicators.IND.1.PREF.objective", telegram_user_id)

    async def set_learning_objective(self, telegram_user_id: int, objective: str) -> Optional[Dict]:
        """Установить цель обучения."""
        return await self.write("indicators.IND.1.PREF.objective", objective, telegram_user_id)

    async def get_roles(self, telegram_user_id: int) -> Optional[List[str]]:
        """Получить роли пользователя."""
        return await self.read("indicators.IND.1.PREF.role_set", telegram_user_id)

    async def set_roles(self, telegram_user_id: int, roles: List[str]) -> Optional[Dict]:
        """Установить роли пользователя."""
        return await self.write("indicators.IND.1.PREF.role_set", roles, telegram_user_id)

    async def get_weekly_time_budget(self, telegram_user_id: int) -> Optional[float]:
        """Получить бюджет времени на обучение (часов/неделю)."""
        return await self.read("indicators.IND.1.PREF.weekly_time_budget", telegram_user_id)

    async def set_weekly_time_budget(self, telegram_user_id: int, hours: float) -> Optional[Dict]:
        """Установить бюджет времени на обучение."""
        return await self.write("indicators.IND.1.PREF.weekly_time_budget", hours, telegram_user_id)

    async def get_current_degree(self, telegram_user_id: int) -> Optional[str]:
        """Получить текущую степень пользователя."""
        return await self.read("degree", telegram_user_id)

    async def get_current_stage(self, telegram_user_id: int) -> Optional[str]:
        """Получить текущую ступень пользователя."""
        return await self.read("stage", telegram_user_id)

    # =========================================================================
    # СИНХРОНИЗАЦИЯ ПРОФИЛЯ БОТ → ЦД
    # =========================================================================

    # Маппинг: поле бота → путь в ЦД (source-of-truth: DP.AISYS.014 § 4.5.1)
    PROFILE_DT_MAPPING = {
        'name': '1_declarative/1_1_profile/02_Имя',
        'occupation': '1_declarative/1_1_profile/01_Занятие',
        'interests': '1_declarative/1_2_goals/01_Интересы',
        'goals': '1_declarative/1_2_goals/09_Цели обучения',
        'role': '1_declarative/1_3_selfeval/06_Роли',
        'study_duration': '1_declarative/1_3_selfeval/11_Срок обучения',
        'current_problems': '1_declarative/1_4_context/01_Текущие проблемы',
        'desires': '1_declarative/1_4_context/02_Желания',
        'schedule_time': '1_declarative/1_4_context/05_Режим обучения',
        'feed_schedule_time': '1_declarative/1_4_context/04_Удобное время',
    }

    @staticmethod
    def _convert_value(field: str, value: Any) -> Any:
        """Конвертация значения бота в формат ЦД."""
        if value is None:
            return ""
        if field == 'interests':
            # JSON array → comma-separated string
            if isinstance(value, list):
                return ", ".join(value)
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, list):
                        return ", ".join(parsed)
                except (json.JSONDecodeError, TypeError):
                    pass
            return str(value)
        if field == 'study_duration':
            return str(value)
        return str(value) if not isinstance(value, str) else value

    async def sync_profile(self, telegram_user_id: int, intern_data: dict) -> int:
        """Полный перелив профиля бота → ЦД. Возвращает кол-во записанных полей."""
        if not self.is_connected(telegram_user_id):
            return 0

        synced = 0
        for field, dt_path in self.PROFILE_DT_MAPPING.items():
            value = intern_data.get(field)
            if value is None or value == '' or value == '[]':
                continue
            converted = self._convert_value(field, value)
            if not converted:
                continue
            try:
                result = await self.write(dt_path, converted, telegram_user_id)
                if result is not None:
                    synced += 1
            except Exception as e:
                logger.error(f"DT sync field {field} failed: {e}")

        logger.info(f"DT sync: user {telegram_user_id}, {synced}/{len(self.PROFILE_DT_MAPPING)} fields")
        return synced

    async def sync_fields(self, telegram_user_id: int, fields: dict) -> int:
        """Инкрементальный sync: только указанные поля. Возвращает кол-во записанных."""
        if not self.is_connected(telegram_user_id):
            return 0

        synced = 0
        for field, value in fields.items():
            dt_path = self.PROFILE_DT_MAPPING.get(field)
            if not dt_path:
                continue
            converted = self._convert_value(field, value)
            try:
                result = await self.write(dt_path, converted, telegram_user_id)
                if result is not None:
                    synced += 1
            except Exception as e:
                logger.error(f"DT sync field {field} failed: {e}")

        if synced:
            logger.info(f"DT incremental sync: user {telegram_user_id}, {synced} fields")
        return synced

    def get_connected_user_ids(self) -> list:
        """Список ID подключённых пользователей."""
        return list(self._tokens.keys())


# Singleton instance
digital_twin = DigitalTwinClient()
