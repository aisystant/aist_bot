"""
Клиент Digital Twin MCP Server.

Digital Twin хранит состояние пользователя по метамодели Aisystant:
- Степени (degrees): Student, Specialist, Master, etc.
- Ступени (stages): Preparing, Practicing, etc.
- Индикаторы (indicators): цели, роли, время, прогресс

Endpoint: https://digital-twin-mcp.aisystant.workers.dev/mcp
Протокол: JSON-RPC 2.0

Использование:
    from clients.digital_twin import digital_twin

    # Читать данные пользователя
    data = await digital_twin.read("indicators.IND.1.PREF.objective", user_id="123")

    # Записать данные
    await digital_twin.write("indicators.IND.1.PREF.role_set", ["developer"], user_id="123")

    # Получить всего twin
    all_data = await digital_twin.read("", user_id="123")
"""

import asyncio
import json
from typing import Any, Dict, List, Optional

import aiohttp

from config import get_logger

logger = get_logger(__name__)

# URL Digital Twin MCP сервера
DIGITAL_TWIN_MCP_URL = "https://digital-twin-mcp.aisystant.workers.dev/mcp"


class DigitalTwinClient:
    """Клиент для работы с Digital Twin MCP Server

    Включает circuit breaker как в основном MCP клиенте.
    При недоступности сервера — graceful fallback.
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

        if url not in DigitalTwinClient._circuit_state:
            DigitalTwinClient._circuit_state[url] = {
                "failures": 0,
                "last_failure": 0,
                "open": False
            }

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _is_circuit_open(self) -> bool:
        """Проверяет, открыт ли circuit breaker"""
        import time
        state = DigitalTwinClient._circuit_state[self.base_url]
        if not state["open"]:
            return False
        if time.time() - state["last_failure"] > self.RECOVERY_TIME:
            logger.info(f"{self.name}: circuit breaker half-open, trying to recover")
            return False
        return True

    def _record_failure(self):
        """Записывает ошибку"""
        import time
        state = DigitalTwinClient._circuit_state[self.base_url]
        state["failures"] += 1
        state["last_failure"] = time.time()
        if state["failures"] >= self.FAILURE_THRESHOLD:
            state["open"] = True
            logger.warning(f"{self.name}: circuit breaker OPEN")

    def _record_success(self):
        """Сбрасывает circuit breaker"""
        state = DigitalTwinClient._circuit_state[self.base_url]
        if state["failures"] > 0 or state["open"]:
            logger.info(f"{self.name}: circuit breaker CLOSED")
        state["failures"] = 0
        state["open"] = False

    async def _call(self, tool: str, args: Dict[str, Any]) -> Optional[Any]:
        """Вызов инструмента Digital Twin MCP

        Args:
            tool: имя инструмента
            args: аргументы

        Returns:
            Результат или None при ошибке
        """
        if self._is_circuit_open():
            logger.debug(f"{self.name}: circuit breaker open, skipping")
            return None

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
                        headers={"Content-Type": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=self.DEFAULT_TIMEOUT)
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if "error" in data:
                                error_msg = data["error"].get("message", str(data["error"]))
                                logger.error(f"{self.name} error: {error_msg}")
                                self._record_failure()
                                return None
                            if "result" in data:
                                self._record_success()
                                # Извлекаем text из content
                                content = data["result"].get("content", [])
                                if content and len(content) > 0:
                                    text = content[0].get("text", "")
                                    # Пробуем распарсить JSON
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
    # МЕТАМОДЕЛЬ (справочники)
    # =========================================================================

    async def get_degrees(self) -> Optional[List[Dict]]:
        """Получить все степени квалификации (Student, Specialist, Master, etc.)"""
        return await self._call("get_degrees", {})

    async def get_stages(self, degree: str = "Student") -> Optional[List[Dict]]:
        """Получить ступени внутри степени

        Args:
            degree: код степени (по умолчанию Student)
        """
        return await self._call("get_stages", {"degree": degree})

    async def get_indicator_groups(self) -> Optional[List[Dict]]:
        """Получить группы индикаторов"""
        return await self._call("get_indicator_groups", {})

    async def get_indicators(
        self,
        group: Optional[str] = None,
        for_prompts: Optional[bool] = None
    ) -> Optional[List[Dict]]:
        """Получить индикаторы метамодели

        Args:
            group: фильтр по группе (IND.1, IND.2, etc.)
            for_prompts: только индикаторы для промптов
        """
        args = {}
        if group:
            args["group"] = group
        if for_prompts is not None:
            args["for_prompts"] = for_prompts
        return await self._call("get_indicators", args)

    async def get_indicator(self, code: str) -> Optional[Dict]:
        """Получить один индикатор по коду

        Args:
            code: код индикатора (IND.1.PREF.objective)
        """
        return await self._call("get_indicator", {"code": code})

    async def get_stage_thresholds(self, indicator_code: str) -> Optional[List[Dict]]:
        """Получить пороги ступеней для индикатора

        Args:
            indicator_code: код индикатора
        """
        return await self._call("get_stage_thresholds", {"indicator_code": indicator_code})

    async def validate_value(self, indicator_code: str, value: Any) -> Optional[Dict]:
        """Валидация значения индикатора

        Args:
            indicator_code: код индикатора
            value: значение для валидации
        """
        return await self._call("validate_value", {
            "indicator_code": indicator_code,
            "value": value
        })

    # =========================================================================
    # ДАННЫЕ ПОЛЬЗОВАТЕЛЯ
    # =========================================================================

    async def read(self, path: str, user_id: str) -> Optional[Any]:
        """Читать данные Digital Twin по пути

        Args:
            path: путь к данным (пустая строка = весь twin)
                  Примеры:
                  - "" — весь twin
                  - "degree" — текущая степень
                  - "stage" — текущая ступень
                  - "indicators.IND.1.PREF.objective" — цель обучения
            user_id: ID пользователя (telegram_id как строка)

        Returns:
            Данные или None
        """
        result = await self._call("read_digital_twin", {
            "path": path,
            "user_id": user_id
        })
        logger.debug(f"{self.name}: read({path}, {user_id}) = {result}")
        return result

    async def write(self, path: str, data: Any, user_id: str) -> Optional[Dict]:
        """Записать данные в Digital Twin

        Args:
            path: путь к данным
            data: данные для записи
            user_id: ID пользователя

        Returns:
            Результат операции или None
        """
        result = await self._call("write_digital_twin", {
            "path": path,
            "data": data,
            "user_id": user_id
        })
        logger.info(f"{self.name}: write({path}, {user_id}) = {result}")
        return result

    async def list_users(self) -> Optional[List[str]]:
        """Получить список всех пользователей"""
        return await self._call("list_users", {})

    # =========================================================================
    # УДОБНЫЕ МЕТОДЫ ДЛЯ БОТА
    # =========================================================================

    async def get_user_profile(self, user_id: str) -> Optional[Dict]:
        """Получить полный профиль пользователя

        Args:
            user_id: telegram_id как строка

        Returns:
            Словарь с данными профиля или None
        """
        return await self.read("", user_id)

    async def get_learning_objective(self, user_id: str) -> Optional[str]:
        """Получить цель обучения пользователя"""
        return await self.read("indicators.IND.1.PREF.objective", user_id)

    async def set_learning_objective(self, user_id: str, objective: str) -> Optional[Dict]:
        """Установить цель обучения"""
        return await self.write("indicators.IND.1.PREF.objective", objective, user_id)

    async def get_roles(self, user_id: str) -> Optional[List[str]]:
        """Получить роли пользователя"""
        return await self.read("indicators.IND.1.PREF.role_set", user_id)

    async def set_roles(self, user_id: str, roles: List[str]) -> Optional[Dict]:
        """Установить роли пользователя"""
        return await self.write("indicators.IND.1.PREF.role_set", roles, user_id)

    async def get_weekly_time_budget(self, user_id: str) -> Optional[float]:
        """Получить бюджет времени на обучение (часов/неделю)"""
        return await self.read("indicators.IND.1.PREF.weekly_time_budget", user_id)

    async def set_weekly_time_budget(self, user_id: str, hours: float) -> Optional[Dict]:
        """Установить бюджет времени на обучение"""
        return await self.write("indicators.IND.1.PREF.weekly_time_budget", hours, user_id)

    async def get_current_degree(self, user_id: str) -> Optional[str]:
        """Получить текущую степень пользователя"""
        return await self.read("degree", user_id)

    async def get_current_stage(self, user_id: str) -> Optional[str]:
        """Получить текущую ступень пользователя"""
        return await self.read("stage", user_id)


# Singleton instance
digital_twin = DigitalTwinClient()
