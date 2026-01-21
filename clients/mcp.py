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
    """Универсальный клиент для работы с MCP серверами Aisystant"""

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

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _call(self, tool_name: str, arguments: dict) -> Optional[dict]:
        """Вызов инструмента MCP через JSON-RPC

        Args:
            tool_name: имя инструмента
            arguments: аргументы вызова

        Returns:
            Результат вызова или None при ошибке
        """
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

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.base_url,
                    headers={"Content-Type": "application/json"},
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if "result" in data:
                            result = data["result"]
                            # Логируем структуру ответа для отладки
                            if result and "content" in result:
                                content_items = result.get("content", [])
                                logger.debug(f"{self.name}: ответ содержит {len(content_items)} content items")
                            return result
                        if "error" in data:
                            logger.error(f"{self.name} JSON-RPC error: {data['error']}")
                            return None
                        # Нет ни result, ни error
                        logger.warning(f"{self.name}: неожиданный ответ (нет result/error): {list(data.keys())}")
                        return None
                    else:
                        error = await resp.text()
                        logger.error(f"{self.name} HTTP error {resp.status}: {error[:500]}")
                        return None
        except asyncio.TimeoutError:
            logger.error(f"{self.name} request timeout (30s)")
            return None
        except Exception as e:
            logger.error(f"{self.name} exception: {e}", exc_info=True)
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
