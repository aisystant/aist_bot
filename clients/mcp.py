"""
Клиент для работы с MCP серверами Aisystant.

MCPClient - универсальный клиент для JSON-RPC взаимодействия с MCP серверами.
Поддерживает:
- MCP-Guides (руководства): semantic_search, get_guides_list, get_guide_sections
- MCP-Knowledge (база знаний): search
"""

import json
import asyncio
from typing import Optional, List

import aiohttp

from config import get_logger, MCP_URL, KNOWLEDGE_MCP_URL

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

    def __init__(self, url: str, name: str = "MCP", search_tool: str = "semantic_search"):
        """
        Args:
            url: URL MCP сервера
            name: имя клиента для логов
            search_tool: инструмент поиска ("semantic_search" для guides, "search" для knowledge)
        """
        self.base_url = url
        self.name = name
        self.search_tool = search_tool
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
        # Circuit breaker: fail-fast если сервер недоступен
        if self._is_circuit_open():
            logger.debug(f"{self.name}: circuit breaker open, skipping request")
            return None

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

    async def get_guides_list(self, lang: str = "ru", category: str = None) -> List[dict]:
        """Получить список всех руководств

        Args:
            lang: язык (ru/en)
            category: категория для фильтрации

        Returns:
            Список руководств
        """
        args = {"lang": lang}
        if category:
            args["category"] = category

        result = await self._call("get_guides_list", args)
        if result and "content" in result:
            # Парсим JSON из content
            for item in result.get("content", []):
                if item.get("type") == "text":
                    try:
                        return json.loads(item.get("text", "[]"))
                    except json.JSONDecodeError:
                        pass
        return []

    async def get_guide_sections(self, guide_slug: str, lang: str = "ru") -> List[dict]:
        """Получить разделы конкретного руководства

        Args:
            guide_slug: slug руководства
            lang: язык (ru/en)

        Returns:
            Список разделов
        """
        result = await self._call("get_guide_sections", {
            "guide_slug": guide_slug,
            "lang": lang
        })
        if result and "content" in result:
            for item in result.get("content", []):
                if item.get("type") == "text":
                    try:
                        return json.loads(item.get("text", "[]"))
                    except json.JSONDecodeError:
                        pass
        return []

    async def get_section_content(self, guide_slug: str, section_slug: str, lang: str = "ru") -> str:
        """Получить содержимое раздела

        Args:
            guide_slug: slug руководства
            section_slug: slug раздела
            lang: язык (ru/en)

        Returns:
            Текст раздела
        """
        result = await self._call("get_section_content", {
            "guide_slug": guide_slug,
            "section_slug": section_slug,
            "lang": lang
        })
        if result and "content" in result:
            for item in result.get("content", []):
                if item.get("type") == "text":
                    return item.get("text", "")
        return ""

    async def semantic_search(self, query: str, lang: str = "ru", limit: int = 5, sort_by: str = None) -> List[dict]:
        """Семантический поиск по руководствам или базе знаний

        Args:
            query: поисковый запрос
            lang: язык (ru/en) — только для MCP-Guides
            limit: максимальное количество результатов
            sort_by: сортировка (например, "created_at:desc" для свежих постов)

        Returns:
            Список результатов поиска
        """
        args = {
            "query": query,
            "limit": limit
        }
        # Параметр lang только для semantic_search (MCP-Guides)
        if self.search_tool == "semantic_search":
            args["lang"] = lang
        if sort_by:
            args["sort"] = sort_by

        result = await self._call(self.search_tool, args)
        if result and "content" in result:
            for item in result.get("content", []):
                if item.get("type") == "text":
                    try:
                        data = json.loads(item.get("text", "[]"))
                        # Если sort_by указан и данные содержат дату, сортируем на клиенте
                        if sort_by and "desc" in sort_by and isinstance(data, list):
                            data.sort(key=lambda x: x.get('created_at', x.get('date', '')), reverse=True)
                        return data
                    except json.JSONDecodeError:
                        # Если не JSON, возвращаем как текст
                        return [{"text": item.get("text", "")}]
        return []

    async def search(self, query: str, limit: int = 5) -> List[dict]:
        """Поиск по базе знаний (knowledge MCP)

        Args:
            query: поисковый запрос
            limit: максимальное количество результатов

        Returns:
            Список результатов поиска
        """
        args = {
            "query": query,
            "limit": limit
        }

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
                        # Если не JSON, возвращаем как текст
                        return [{"text": raw_text}]
        logger.debug(f"{self.name} search: no content in result")
        return []


# Создаём клиенты для двух MCP серверов
mcp_guides = MCPClient(MCP_URL, "MCP-Guides")
mcp_knowledge = MCPClient(KNOWLEDGE_MCP_URL, "MCP-Knowledge", search_tool="search")

# Для обратной совместимости
mcp = mcp_guides
