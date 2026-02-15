"""
Клиент для работы с MCP серверами Aisystant.

MCPClient - универсальный клиент для JSON-RPC взаимодействия с MCP серверами.
Поддерживает:
- Knowledge MCP (SYS.017): unified search по Pack + guides + DS
"""

import json
import asyncio
from typing import Optional, List

import aiohttp

from config import get_logger, KNOWLEDGE_MCP_URL

logger = get_logger(__name__)


class MCPClient:
    """Универсальный клиент для работы с MCP серверами Aisystant

    Включает circuit breaker: если сервер недоступен, запросы fail-fast
    без ожидания таймаута. Автоматически восстанавливается через 60 секунд.
    """

    # Настройки таймаутов и retry
    DEFAULT_TIMEOUT = 15  # секунд (первая попытка)
    RETRY_TIMEOUT = 10    # секунд (повторная попытка)
    MAX_RETRIES = 1       # количество повторных попыток

    # Circuit breaker настройки
    FAILURE_THRESHOLD = 2   # после скольких ошибок отключаемся
    RECOVERY_TIME = 60      # секунд до повторной попытки

    # Глобальное состояние circuit breaker для каждого сервера
    _circuit_state: dict = {}  # url -> {"failures": int, "last_failure": timestamp, "open": bool}

    def __init__(self, url: str, name: str = "MCP"):
        """
        Args:
            url: URL MCP сервера
            name: имя клиента для логов
        """
        self.base_url = url
        self.name = name
        self._request_id = 0

        # Инициализируем circuit breaker для этого URL
        if url not in MCPClient._circuit_state:
            MCPClient._circuit_state[url] = {"failures": 0, "last_failure": 0, "open": False}

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _is_circuit_open(self) -> bool:
        """Проверяет, открыт ли circuit breaker (сервер недоступен)"""
        state = MCPClient._circuit_state[self.base_url]
        if not state["open"]:
            return False

        # Проверяем, прошло ли время восстановления
        import time
        if time.time() - state["last_failure"] > self.RECOVERY_TIME:
            logger.info(f"{self.name}: circuit breaker half-open, пробуем восстановить")
            return False  # Даём шанс на восстановление

        return True

    def _record_failure(self):
        """Записывает ошибку в circuit breaker"""
        import time
        state = MCPClient._circuit_state[self.base_url]
        state["failures"] += 1
        state["last_failure"] = time.time()

        if state["failures"] >= self.FAILURE_THRESHOLD:
            state["open"] = True
            logger.warning(f"{self.name}: circuit breaker OPEN — сервер временно отключён")

    def _record_success(self):
        """Сбрасывает circuit breaker при успехе"""
        state = MCPClient._circuit_state[self.base_url]
        if state["failures"] > 0 or state["open"]:
            logger.info(f"{self.name}: circuit breaker CLOSED — сервер восстановлен")
        state["failures"] = 0
        state["open"] = False

    async def _call(self, tool_name: str, arguments: dict) -> Optional[dict]:
        """Вызов инструмента MCP через JSON-RPC с retry и circuit breaker

        Args:
            tool_name: имя инструмента
            arguments: аргументы вызова

        Returns:
            Результат вызова или None при ошибке (graceful fallback)
        """
        from core.tracing import span

        # Circuit breaker: fail-fast если сервер недоступен
        if self._is_circuit_open():
            logger.debug(f"{self.name}: circuit breaker open, skipping request")
            return None

        async with span(f"mcp.{tool_name}", server=self.name):
            return await self._call_inner(tool_name, arguments)

    async def _call_inner(self, tool_name: str, arguments: dict) -> Optional[dict]:
        """Внутренняя реализация вызова MCP (вынесена для трейсинга)."""
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments
            },
            "id": self._next_id()
        }

        logger.debug(f"{self.name}: вызов {tool_name} с аргументами {arguments}")

        last_error = None
        for attempt in range(self.MAX_RETRIES + 1):
            # Используем разные таймауты для первой и повторных попыток
            timeout = self.DEFAULT_TIMEOUT if attempt == 0 else self.RETRY_TIMEOUT

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        self.base_url,
                        headers={"Content-Type": "application/json"},
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=timeout)
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if "result" in data:
                                result = data["result"]
                                self._record_success()  # Сбрасываем circuit breaker
                                # Логируем структуру ответа для отладки
                                if result and "content" in result:
                                    content_items = result.get("content", [])
                                    logger.debug(f"{self.name}: ответ содержит {len(content_items)} content items")
                                return result
                            if "error" in data:
                                error_msg = data['error'].get('message', str(data['error']))
                                # 502 ошибка бэкенда — записываем в circuit breaker
                                if "502" in error_msg:
                                    logger.warning(f"{self.name}: backend error (502), marking as failed")
                                    self._record_failure()
                                else:
                                    logger.error(f"{self.name} JSON-RPC error: {error_msg}")
                                return None
                            # Нет ни result, ни error
                            logger.warning(f"{self.name}: неожиданный ответ (нет result/error): {list(data.keys())}")
                            return None
                        else:
                            error = await resp.text()
                            logger.error(f"{self.name} HTTP error {resp.status}: {error[:200]}")
                            last_error = f"HTTP {resp.status}"
                            self._record_failure()
            except asyncio.TimeoutError:
                last_error = "timeout"
                if attempt < self.MAX_RETRIES:
                    logger.warning(f"{self.name} timeout ({timeout}s), retry {attempt + 1}/{self.MAX_RETRIES}...")
                    await asyncio.sleep(1)  # Короткая пауза перед retry
                    continue
                self._record_failure()
            except Exception as e:
                logger.error(f"{self.name} exception: {e}")
                self._record_failure()
                return None

        # Все попытки исчерпаны
        logger.warning(f"{self.name}: unavailable, continuing without MCP context")
        return None

    async def search(self, query: str, limit: int = 5,
                     source: str = None, source_type: str = None) -> List[dict]:
        """Семантический поиск по unified Knowledge MCP

        Args:
            query: поисковый запрос
            limit: максимальное количество результатов
            source: фильтр по источнику (например, "PACK-digital-platform")
            source_type: фильтр по типу ("pack", "guides", "ds")

        Returns:
            Список результатов поиска [{filename, content, source, source_type, score}]
        """
        args = {
            "query": query,
            "limit": limit
        }
        if source:
            args["source"] = source
        if source_type:
            args["source_type"] = source_type

        result = await self._call("search", args)
        if result and "content" in result:
            for item in result.get("content", []):
                if item.get("type") == "text":
                    raw_text = item.get("text", "[]")
                    logger.debug(f"{self.name} search: raw response length={len(raw_text)}")
                    try:
                        data = json.loads(raw_text)
                        parsed_count = len(data) if isinstance(data, list) else 1
                        logger.debug(f"{self.name} search: parsed {parsed_count} items")
                        return data if isinstance(data, list) else [data]
                    except json.JSONDecodeError as e:
                        logger.warning(f"{self.name} search: JSON parse error: {e}, returning as text")
                        return [{"text": raw_text}]
        logger.debug(f"{self.name} search: no content in result")
        return []

    async def get_document(self, filename: str, source: str = None) -> Optional[dict]:
        """Получить документ по имени файла

        Args:
            filename: имя файла (относительный путь)
            source: фильтр по источнику

        Returns:
            Документ {filename, content, source, source_type} или None
        """
        args = {"filename": filename}
        if source:
            args["source"] = source

        result = await self._call("get_document", args)
        if result and "content" in result:
            for item in result.get("content", []):
                if item.get("type") == "text":
                    try:
                        return json.loads(item.get("text", "null"))
                    except json.JSONDecodeError:
                        pass
        return None

    async def list_sources(self, source_type: str = None) -> List[dict]:
        """Список доступных баз знаний

        Args:
            source_type: фильтр по типу ("pack", "guides", "ds")

        Returns:
            Список источников [{source, source_type, doc_count}]
        """
        args = {}
        if source_type:
            args["source_type"] = source_type

        result = await self._call("list_sources", args)
        if result and "content" in result:
            for item in result.get("content", []):
                if item.get("type") == "text":
                    try:
                        return json.loads(item.get("text", "[]"))
                    except json.JSONDecodeError:
                        pass
        return []

    # Backward-compatible alias for code that still calls semantic_search
    async def semantic_search(self, query: str, lang: str = "ru",
                              limit: int = 5, sort_by: str = None,
                              source_type: str = None) -> List[dict]:
        """Alias for search() — backward compatibility with old guides-mcp API"""
        return await self.search(query, limit=limit, source_type=source_type)


# Unified Knowledge MCP client (SYS.017)
mcp_knowledge = MCPClient(KNOWLEDGE_MCP_URL, "MCP-Knowledge")
