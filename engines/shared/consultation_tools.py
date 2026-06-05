"""
Инструменты для Claude-консультанта (tool_use).

Определяет tools для Anthropic API и executor для их вызова.
Используется в handle_question_with_tools() для T2+ пользователей.

Tools:
- search_knowledge: поиск в базе знаний (Pack, guides, DS)
- search_guides: поиск по гайдам
- read_digital_twin: чтение данных ЦД пользователя
- search_personal: поиск по личным знаниям (L4) — NEW via Gateway

Архитектурное решение: DP.ARCH.002 (Тиры обслуживания).
WP-209 Ф0: переключение на Gateway MCP.
"""

import json
from typing import Any, Dict, List, Optional

from config import get_logger
from clients.gateway_mcp import gateway_mcp

logger = get_logger(__name__)


# =============================================================================
# TOOL DEFINITIONS (Anthropic API format)
# =============================================================================

TOOL_SEARCH_KNOWLEDGE = {
    "name": "search_knowledge",
    "description": (
        "Семантический поиск по базе знаний Aisystant. "
        "Включает Pack-источники (системное мышление, личное развитие, "
        "цифровая платформа), руководства и документацию. "
        "Используй для ответа на предметные вопросы пользователя. "
        "ВАЖНО: перед вызовом этого инструмента система уже выполняет pre-search "
        "и включает результаты в контекст. НЕ вызывай search_knowledge для того же "
        "запроса — используй только для уточняющего поиска с ИЗМЕНЁННЫМ запросом, "
        "если pre-search результатов недостаточно."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Поисковый запрос на естественном языке"
            },
            "limit": {
                "type": "integer",
                "description": "Максимум результатов (1-10)",
                "default": 5
            },
            "source_type": {
                "type": "string",
                "enum": ["pack", "guides", "ds"],
                "description": "Фильтр по типу источника: pack (знания), guides (гайды), ds (процессы)"
            }
        },
        "required": ["query"]
    }
}

TOOL_SEARCH_GUIDES = {
    "name": "search_guides",
    "description": (
        "Поиск по образовательным гайдам Aisystant. "
        "Гайды содержат пошаговые инструкции, примеры и практики. "
        "Используй когда нужен практический совет или пошаговая инструкция."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Поисковый запрос"
            },
            "limit": {
                "type": "integer",
                "description": "Максимум результатов (1-10)",
                "default": 5
            }
        },
        "required": ["query"]
    }
}

TOOL_READ_DIGITAL_TWIN = {
    "name": "read_digital_twin",
    "description": (
        "Чтение данных Цифрового Двойника (ЦД) пользователя. "
        "ЦД содержит: профиль (имя, занятие), цели обучения, "
        "самооценку по компетенциям, текущие проблемы, контекст. "
        "Используй для персонализации ответа, когда нужно знать "
        "цели, проблемы или контекст пользователя."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Путь к данным. Примеры: "
                    "'1_declarative' (весь декларативный раздел), "
                    "'1_declarative/1_2_goals' (цели), "
                    "'1_declarative/1_3_selfeval' (самооценка), "
                    "'1_declarative/1_4_context' (контекст). "
                    "Пустая строка = весь профиль."
                )
            }
        },
        "required": ["path"]
    }
}

TOOL_GET_COGNITIVE_BRIEF = {
    "name": "get_cognitive_brief",
    "description": (
        "Чтение cognitive brief пользователя — агрегированного среза его текущего состояния. "
        "Возвращает 4 поля: orchestrator_brief (режим дня), tailor_recommendation (занятие), "
        "stuck_analysis (поведенческие сигналы), cognitive_profile (cp.wld/cp.agt/bh.awr — только при consent). "
        "Навигатор ДОЛЖЕН вызывать этот tool ПЕРЕД каждым ответом. "
        "Если cognitive_profile = null — значит нет text_analysis consent, отвечай без него."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": []
    }
}


TOOL_GET_BOT_INFO = {
    "name": "get_bot_info",
    "description": (
        "Информация о боте AIST: команды, FAQ, устранение проблем. "
        "Используй ТОЛЬКО когда пользователь КОНКРЕТНО спрашивает о командах бота, "
        "его функциях или как пользоваться ботом. "
        "НЕ используй для предметных вопросов (системное мышление, IWE, установка, архитектура, "
        "философия, обучение) — для них используй search_knowledge или search_guides."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": []
    }
}


TOOL_SEARCH_PERSONAL = {
    "name": "search_personal",
    "description": (
        "Поиск по личным знаниям пользователя (L4). "
        "Включает личные репозитории GitHub: стратегию, заметки, экзокортекс. "
        "Используй для вопросов, связанных с личным контекстом пользователя, "
        "его заметками, стратегией или персональными документами."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Поисковый запрос"
            },
            "limit": {
                "type": "integer",
                "description": "Максимум результатов (1-10)",
                "default": 5
            }
        },
        "required": ["query"]
    }
}


# Tools with custom executors — их descriptors захардкожены, executor имеет
# специальную логику (401-fallback, compact output, DT paths).
# Discovered tools с этими именами игнорируются (hybrid, DP.ROLE.038 §4).
_HARDCODED_TOOL_NAMES = frozenset({
    "search_knowledge", "search_guides", "read_digital_twin",
    "get_cognitive_brief", "get_bot_info", "search_personal",
})


def get_tools_for_tier(has_digital_twin: bool) -> List[Dict[str, Any]]:
    """Возвращает набор tools для Claude API.

    Базовые (hardcoded): search_knowledge, search_guides, get_bot_info
    T2+ (с ЦД): + read_digital_twin, search_personal
    Discovered (DP.SC.129): новые tool из tools/list, не покрытые hardcoded.
    """
    import asyncio

    tools: List[Dict[str, Any]] = [TOOL_SEARCH_KNOWLEDGE, TOOL_SEARCH_GUIDES, TOOL_GET_BOT_INFO]
    if has_digital_twin:
        tools.append(TOOL_READ_DIGITAL_TWIN)
        tools.append(TOOL_GET_COGNITIVE_BRIEF)
        tools.append(TOOL_SEARCH_PERSONAL)

    # Append discovered tools whose names are not already in the hardcoded set.
    # admin_* tools are skipped (require elevated permissions).
    hardcoded_names = {t["name"] for t in tools}
    for disc in gateway_mcp.get_discovered_tools():
        name = disc.get("name", "")
        if name.startswith("admin_") or name in hardcoded_names:
            continue
        tools.append(disc)

    # Background refresh if cache is stale (fire-and-forget, does not block response)
    if not gateway_mcp.is_tools_cache_fresh():
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(_refresh_tools_cache())
        except Exception:
            pass

    return tools


async def _refresh_tools_cache() -> None:
    """Fire-and-forget background refresh of tools discovery cache."""
    try:
        await gateway_mcp.list_tools()
    except Exception as e:
        logger.warning(f"Tools discovery background refresh failed: {e}")


# =============================================================================
# TOOL EXECUTOR
# =============================================================================

async def execute_tool(
    tool_name: str,
    tool_input: Dict[str, Any],
    telegram_user_id: Optional[int] = None,
) -> str:
    """Исполняет tool call, проксируя к существующим MCP-клиентам.

    Для hardcoded tools — кастомные обёртки с fallback-логикой.
    Для discovered tools (DP.SC.129) — generic proxy через gateway_mcp._call().
    """
    if tool_name == "search_knowledge":
        return await _exec_search_knowledge(tool_input, telegram_user_id)
    elif tool_name == "search_guides":
        return await _exec_search_guides(tool_input, telegram_user_id)
    elif tool_name == "read_digital_twin":
        return await _exec_read_digital_twin(tool_input, telegram_user_id)
    elif tool_name == "get_cognitive_brief":
        return await _exec_get_cognitive_brief(tool_input, telegram_user_id)
    elif tool_name == "search_personal":
        return await _exec_search_personal(tool_input, telegram_user_id)
    elif tool_name == "get_bot_info":
        return _exec_get_bot_info()
    else:
        return await _exec_generic_tool(tool_name, tool_input, telegram_user_id)


async def _exec_generic_tool(
    tool_name: str,
    tool_input: Dict[str, Any],
    telegram_user_id: Optional[int] = None,
) -> str:
    """Generic proxy для discovered tools (DP.SC.129).

    Вызывает gateway_mcp._call(tool_name, tool_input) без кастомной обёртки.
    Применяется для knowledge_concept_* и любых будущих tool без специальной логики.
    """
    try:
        # pylint: disable=protected-access
        result = await gateway_mcp._call(tool_name, tool_input, telegram_user_id)
        if result is None:
            return json.dumps({"error": f"Tool {tool_name} unavailable"}, ensure_ascii=False)
        data = gateway_mcp._parse_text_content(result)
        if data is None:
            return json.dumps({"result": None}, ensure_ascii=False)
        if isinstance(data, (dict, list)):
            return json.dumps(data, ensure_ascii=False)
        return str(data)
    except Exception as e:
        logger.error(f"Generic tool executor error for {tool_name}: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _exec_get_bot_info() -> str:
    """Возвращает компактную справку о боте (идентичность + FAQ, без полного списка сценариев).

    Полный self-knowledge слишком большой (~4000 chars) и содержит список всех
    сценариев с командами. Claude, получив его, склонен цитировать команды
    вместо ответа по существу → off-topic_response (WP-7 urgent fix).
    """
    from core.self_knowledge import get_self_knowledge
    knowledge = get_self_knowledge('ru')
    # Обрезаем до секции FAQ (убираем полный список сценариев)
    # Структура: identity → scenarios → platform → FAQ → troubleshooting → integrations → programs
    # Нужны: identity + FAQ + troubleshooting (без scenarios и platform)
    lines = knowledge.split('\n')
    compact_parts = []
    skip = False
    for line in lines:
        # Пропускаем секцию сценариев (громоздкий список команд)
        if line.strip().startswith('## Сценарии') or line.strip().startswith('## Scenarios'):
            skip = True
            compact_parts.append("## Основные команды: /learn, /feed, /test, /me, /mode, /settings, /profile, /help, /connect, ?вопрос, .заметка")
            compact_parts.append("## /connect — подключение IWE к AI-клиентам (claude.ai, Cursor, ChatGPT, Claude Code). Показывает пошаговые инструкции для каждого клиента.")
            continue
        # Возобновляем после секции сценариев
        if skip and line.strip().startswith('## '):
            skip = False
        if not skip:
            compact_parts.append(line)
    compact = '\n'.join(compact_parts)
    return json.dumps({"bot_info": compact[:3000]}, ensure_ascii=False)


async def _exec_search_knowledge(input: Dict[str, Any],
                                 telegram_user_id: Optional[int] = None) -> str:
    """Proxy к gateway_mcp.knowledge_search() (WP-209: через Gateway)."""
    query = input.get("query", "")
    limit = min(input.get("limit", 5), 10)
    source_type = input.get("source_type")

    try:
        results = await gateway_mcp.knowledge_search(
            query=query,
            limit=limit,
            source_type=source_type,
            telegram_user_id=telegram_user_id,
        )

        if not results:
            return json.dumps({"results": [], "message": "No results found"}, ensure_ascii=False)

        # Форматируем результаты компактно для Claude
        formatted = []
        for item in results:
            if isinstance(item, dict):
                entry = {
                    "text": (item.get("text", item.get("content", "")))[:2000],
                    "source": item.get("source", ""),
                    "source_type": item.get("source_type", "pack"),
                    "score": item.get("score", 0),
                }
                github_url = item.get("github_url")
                if github_url:
                    entry["github_url"] = github_url
                formatted.append(entry)
            elif isinstance(item, str):
                formatted.append({"text": item[:2000]})

        logger.info(f"search_knowledge: {len(formatted)} results for '{query[:50]}'")
        return json.dumps({"results": formatted}, ensure_ascii=False)

    except Exception as e:
        logger.error(f"search_knowledge error: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _exec_search_guides(input: Dict[str, Any],
                              telegram_user_id: Optional[int] = None) -> str:
    """Proxy к gateway_mcp.knowledge_search(source_type='guides') (WP-209: через Gateway)."""
    query = input.get("query", "")
    limit = min(input.get("limit", 5), 10)

    try:
        results = await gateway_mcp.knowledge_search(
            query=query,
            limit=limit,
            source_type="guides",
            telegram_user_id=telegram_user_id,
        )

        if not results:
            return json.dumps({"results": [], "message": "No guides found"}, ensure_ascii=False)

        formatted = []
        for item in results:
            if isinstance(item, dict):
                formatted.append({
                    "text": (item.get("text", item.get("content", "")))[:2000],
                    "source": item.get("source", ""),
                    "score": item.get("score", 0),
                })
            elif isinstance(item, str):
                formatted.append({"text": item[:2000]})

        logger.info(f"search_guides: {len(formatted)} results for '{query[:50]}'")
        return json.dumps({"results": formatted}, ensure_ascii=False)

    except Exception as e:
        logger.error(f"search_guides error: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _exec_read_digital_twin(
    input: Dict[str, Any],
    telegram_user_id: Optional[int] = None,
) -> str:
    """Proxy к gateway_mcp.dt_read() (WP-209: через Gateway)."""
    path = input.get("path", "")

    if not telegram_user_id:
        return json.dumps({"error": "User not connected to Digital Twin"}, ensure_ascii=False)

    if not gateway_mcp.is_connected(telegram_user_id):
        return json.dumps({"error": "User not authorized — requires Ory registration"}, ensure_ascii=False)

    try:
        data = await gateway_mcp.dt_read(path, telegram_user_id)

        if data is None:
            return json.dumps({"data": None, "message": f"No data at path '{path}'"}, ensure_ascii=False)

        logger.info(f"read_digital_twin: path='{path}', user={telegram_user_id}")
        return json.dumps({"data": data}, ensure_ascii=False, default=str)

    except Exception as e:
        logger.error(f"read_digital_twin error: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _exec_get_cognitive_brief(
    input: Dict[str, Any],
    telegram_user_id: Optional[int] = None,
) -> str:
    """Proxy к gateway_mcp.get_cognitive_brief() (DP.SC.147)."""
    if not telegram_user_id:
        return json.dumps({"error": "User not identified"}, ensure_ascii=False)

    if not gateway_mcp.is_connected(telegram_user_id):
        return json.dumps({"error": "User not authorized — requires Ory registration"}, ensure_ascii=False)

    try:
        # get_cognitive_brief — discovered tool из gateway-mcp
        result = await gateway_mcp._call("get_cognitive_brief", {}, telegram_user_id)
        if result is None:
            return json.dumps({"error": "Cognitive brief unavailable"}, ensure_ascii=False)
        data = gateway_mcp._parse_text_content(result)
        if data is None:
            return json.dumps({"result": None}, ensure_ascii=False)
        if isinstance(data, (dict, list)):
            return json.dumps(data, ensure_ascii=False)
        return str(data)
    except Exception as e:
        logger.error(f"get_cognitive_brief error: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _exec_search_personal(
    input: Dict[str, Any],
    telegram_user_id: Optional[int] = None,
) -> str:
    """Proxy к gateway_mcp.personal_search() (WP-209: L4 через Gateway)."""
    query = input.get("query", "")
    limit = min(input.get("limit", 5), 10)

    if not telegram_user_id:
        return json.dumps({"error": "User not identified"}, ensure_ascii=False)

    if not gateway_mcp.is_connected(telegram_user_id):
        return json.dumps({"error": "User not authorized — requires Ory registration"}, ensure_ascii=False)

    try:
        results = await gateway_mcp.personal_search(
            query=query,
            telegram_user_id=telegram_user_id,
            limit=limit,
        )

        if not results:
            return json.dumps({"results": [], "message": "No personal knowledge found"}, ensure_ascii=False)

        formatted = []
        for item in results:
            if isinstance(item, dict):
                formatted.append({
                    "text": (item.get("text", item.get("content", "")))[:2000],
                    "source": item.get("source", ""),
                    "score": item.get("score", 0),
                })
            elif isinstance(item, str):
                formatted.append({"text": item[:2000]})

        logger.info(f"search_personal: {len(formatted)} results for '{query[:50]}'")
        return json.dumps({"results": formatted}, ensure_ascii=False)

    except Exception as e:
        logger.error(f"search_personal error: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# =============================================================================
# STANDARD CLAUDE.MD LOADER
# =============================================================================

_standard_claude_md_cache: Optional[str] = None


def get_standard_claude_md() -> str:
    """Загружает standard CLAUDE.md для T2+ system prompt.

    Содержит ключевые методологические принципы (FPF, различения, протоколы),
    доступные всем пользователям с ЦД (T2+).

    Путь: config/standard_claude.md (хранится в боте, обновляется template-sync).
    """
    global _standard_claude_md_cache

    if _standard_claude_md_cache is not None:
        return _standard_claude_md_cache

    from pathlib import Path
    path = Path(__file__).parent.parent.parent / "config" / "standard_claude.md"

    if path.exists():
        try:
            _standard_claude_md_cache = path.read_text(encoding="utf-8")
            logger.info(f"Standard CLAUDE.md loaded: {len(_standard_claude_md_cache)} chars")
            return _standard_claude_md_cache
        except Exception as e:
            logger.warning(f"Failed to read standard CLAUDE.md: {e}")

    # Fallback: пустая строка (T1 поведение)
    _standard_claude_md_cache = ""
    logger.info("Standard CLAUDE.md: not found, T1 mode")
    return _standard_claude_md_cache


def invalidate_standard_claude_cache():
    """Сброс кеша standard CLAUDE.md (для hot reload)."""
    global _standard_claude_md_cache
    _standard_claude_md_cache = None


# =============================================================================
# PERSONAL CLAUDE.MD LOADER (T3)
# =============================================================================

# In-memory кеш: telegram_user_id -> (content, timestamp)
_personal_claude_cache: Dict[int, tuple] = {}
_PERSONAL_CACHE_TTL = 300  # 5 минут


async def get_personal_claude_md(telegram_user_id: int) -> str:
    """Загружает personal CLAUDE.md из GitHub (T3 Со-мыслитель).

    Читает exocortex/CLAUDE.md из strategy_repo пользователя.
    Содержит персональную методологию, правила и контекст.

    Кешируется на 5 минут (GitHub API rate limits).

    Returns:
        Содержимое personal CLAUDE.md или пустая строка.
    """
    import time

    # Проверяем кеш
    cached = _personal_claude_cache.get(telegram_user_id)
    if cached:
        content, ts = cached
        if time.time() - ts < _PERSONAL_CACHE_TTL:
            return content

    from clients.github_oauth import github_oauth
    from clients.github_strategy import github_strategy

    strategy_repo = await github_oauth.get_strategy_repo(telegram_user_id)
    if not strategy_repo:
        logger.info(f"Personal CLAUDE.md: no strategy_repo for user {telegram_user_id}")
        _personal_claude_cache[telegram_user_id] = ("", time.time())
        return ""

    # Читаем exocortex/CLAUDE.md
    claude_md = await github_strategy.read_file(
        telegram_user_id, strategy_repo, "exocortex/CLAUDE.md"
    )

    if not claude_md:
        logger.info(f"Personal CLAUDE.md: not found in {strategy_repo}/exocortex/")
        _personal_claude_cache[telegram_user_id] = ("", time.time())
        return ""

    # Ограничиваем размер (system prompt budget)
    MAX_PERSONAL_CHARS = 6000
    if len(claude_md) > MAX_PERSONAL_CHARS:
        claude_md = claude_md[:MAX_PERSONAL_CHARS] + "\n\n[...усечено...]"

    logger.info(
        f"Personal CLAUDE.md loaded: {len(claude_md)} chars "
        f"from {strategy_repo} for user {telegram_user_id}"
    )
    _personal_claude_cache[telegram_user_id] = (claude_md, time.time())
    return claude_md


def invalidate_personal_claude_cache(telegram_user_id: Optional[int] = None):
    """Сброс кеша personal CLAUDE.md."""
    if telegram_user_id:
        _personal_claude_cache.pop(telegram_user_id, None)
    else:
        _personal_claude_cache.clear()


# =============================================================================
# TIER PROMPT LOADER (DP.ARCH.002)
# =============================================================================

_tier_prompt_cache: Dict[int, str] = {}

_TIER_FILES = {
    1: "t1_expert.md",
    2: "t2_mentor.md",
    3: "t3_cothinker.md",
    4: "t3_cothinker.md",  # T4 uses T3 prompt until dedicated tools exist
}


def load_tier_prompt(tier: int) -> str:
    """Загружает шаблон system prompt для указанного тира.

    Промпты хранятся в config/prompts/t{N}_{role}.md.
    Содержат {placeholders} для подстановки через fill_tier_prompt().

    Args:
        tier: номер тира (1-4)

    Returns:
        Шаблон промпта (строка с {placeholders}).
    """
    if tier in _tier_prompt_cache:
        return _tier_prompt_cache[tier]

    from pathlib import Path
    filename = _TIER_FILES.get(tier, _TIER_FILES[1])
    path = Path(__file__).parent.parent.parent / "config" / "prompts" / filename

    if path.exists():
        try:
            content = path.read_text(encoding="utf-8")
            _tier_prompt_cache[tier] = content
            logger.info(f"Tier prompt loaded: T{tier} ({filename}, {len(content)} chars)")
            return content
        except Exception as e:
            logger.warning(f"Failed to read tier prompt {filename}: {e}")

    # Fallback: базовый промпт T1
    fallback = (
        "Ты — дружелюбный эксперт по системному мышлению.\n"
        "Отвечаешь на вопросы пользователя {name}.\n\n"
        "{lang_instruction}\n\n"
        "Используй tools для поиска информации.\n"
        "НЕ выдумывай факты.\n\n"
        "{knowledge_section}\n"
        "{lang_reminder}"
    )
    logger.warning(f"Tier prompt T{tier} not found at {path}, using fallback")
    _tier_prompt_cache[tier] = fallback
    return fallback


def fill_tier_prompt(template: str, **kwargs) -> str:
    """Безопасная подстановка {placeholders} в шаблон промпта.

    В отличие от str.format(), не падает на неизвестных {braces}.
    """
    result = template
    for key, value in kwargs.items():
        result = result.replace(f"{{{key}}}", str(value))
    return result


def invalidate_tier_prompt_cache():
    """Сброс кеша tier prompts (для hot reload)."""
    _tier_prompt_cache.clear()


# =============================================================================
# ROLE PROMPT LOADER (DP.D.044 — Role Attribution)
# =============================================================================

_role_prompt_cache: Dict[str, str] = {}

_ROLE_FILES = {
    "navigator": "navigator.md",
    "diagnostician": "diagnostician.md",
}

# Role display: emoji + name for footer (L1 Role Attribution)
ROLE_ATTRIBUTION = {
    "navigator": {"ru": "🧭 Навигатор", "en": "🧭 Navigator"},
    "diagnostician": {"ru": "🎯 Диагност", "en": "🎯 Diagnostician"},
}

# WP-156: Hint for continuing with the same role via prefix
ROLE_CONTINUE_HINT = {
    "navigator": {
        "ru": "Чтобы продолжить с Навигатором, начни вопрос со слова «Навигатор» и далее ваш вопрос.",
        "en": "To continue with Navigator, start with 'Navigator' followed by your question.",
    },
    "diagnostician": {
        "ru": "Чтобы продолжить с Диагностом, начни вопрос со слова «Диагност» и далее ваш вопрос.",
        "en": "To continue with Diagnostician, start with 'Diagnostician' followed by your question.",
    },
}

# Role transition messages (L2 Role Attribution)
ROLE_TRANSITION = {
    "navigator": {
        "ru": "🧭 _Подключаю Навигатора — он поможет с траекторией развития._",
        "en": "🧭 _Connecting Navigator — they'll help with your development path._",
    },
    "diagnostician": {
        "ru": "🎯 _Подключаю Диагноста — он определит вашу ступень._",
        "en": "🎯 _Connecting Diagnostician — they'll assess your level._",
    },
}

ROLE_RETURN_MESSAGE = {
    "ru": "↩️ _Возвращаю Консультанта._",
    "en": "↩️ _Returning to Consultant._",
}


def load_role_prompt(role: str) -> Optional[str]:
    """Загружает промпт для специализированной роли (Навигатор, Диагност).

    Args:
        role: идентификатор роли ("navigator", "diagnostician")

    Returns:
        Шаблон промпта или None если роль не найдена.
    """
    if role in _role_prompt_cache:
        return _role_prompt_cache[role]

    filename = _ROLE_FILES.get(role)
    if not filename:
        return None

    from pathlib import Path
    path = Path(__file__).parent.parent.parent / "config" / "prompts" / filename

    if path.exists():
        try:
            content = path.read_text(encoding="utf-8")
            _role_prompt_cache[role] = content
            logger.info(f"Role prompt loaded: {role} ({filename}, {len(content)} chars)")
            return content
        except Exception as e:
            logger.warning(f"Failed to read role prompt {filename}: {e}")

    logger.warning(f"Role prompt {role} not found at {path}")
    return None


def get_role_footer(role: str, lang: str) -> str:
    """Возвращает footer с подписью роли (L1 Role Attribution) + hint для продолжения.

    Добавляется ПОСЛЕ генерации, не в промпте.
    """
    attr = ROLE_ATTRIBUTION.get(role, {})
    footer = attr.get(lang, attr.get("ru", ""))
    hint = ROLE_CONTINUE_HINT.get(role, {})
    hint_text = hint.get(lang, hint.get("ru", ""))
    if footer and hint_text:
        return f"{footer}\n{hint_text}"
    return footer


def invalidate_role_prompt_cache():
    """Сброс кеша role prompts."""
    _role_prompt_cache.clear()
