"""
Round-trip test: EngineRouter → PlatformTailorAdapter → LessonPacket (WP-262 В1.1).

Проверяет:
- TAILOR_ROUTE=platform → EngineRouter возвращает PlatformTailorAdapter
- assemble_lesson() десериализует HTTP-ответ в LessonPacket
- Поля lesson и generated содержат ожидаемые ключи
- Shared secret передаётся в заголовке
- ConnectionError при сетевом сбое

HTTP не поднимается — используется httpx mock transport.
"""

import importlib.util
import json
import sys
import types
import unittest.mock as mock
from pathlib import Path

import httpx
import pytest

# ──────────────────────────────────────────────────────────────────
# 1. Stub aiogram before anything else.
#    engines/__init__.py → mode_selector.py → aiogram (Command etc.)
# ──────────────────────────────────────────────────────────────────
_AIOGRAM_ATTRS = (
    "Bot", "Dispatcher", "Router", "F",
    "Message", "CallbackQuery",
    "InlineKeyboardMarkup", "InlineKeyboardButton",
    "Command",  # aiogram.filters.Command
)
for _submod in (
    "aiogram",
    "aiogram.types",
    "aiogram.filters",
    "aiogram.fsm.context",
    "aiogram.fsm.storage.base",
    "aiogram.fsm.storage.memory",
):
    if _submod not in sys.modules:
        _m = types.ModuleType(_submod)
        for _attr in _AIOGRAM_ATTRS:
            setattr(_m, _attr, mock.MagicMock())
        sys.modules[_submod] = _m

# ──────────────────────────────────────────────────────────────────
# 2. Pre-register package namespaces so that relative imports inside
#    bot_adapter.py / router.py don't trigger engines/__init__.py.
#    Also stub engines.tailor.planner: bot_adapter imports BLOOM_NAMES
#    and DIRECTION_NAMES from there, but those constants live in
#    core/evaluator.py — pre-existing import bug, not our concern here.
# ──────────────────────────────────────────────────────────────────
for _pkg in ("engines", "engines.tailor"):
    if _pkg not in sys.modules:
        sys.modules[_pkg] = types.ModuleType(_pkg)

_planner_stub = types.ModuleType("engines.tailor.planner")
_planner_stub.BLOOM_NAMES = {1: "Различения", 2: "Понимание", 3: "Применение"}
_planner_stub.DIRECTION_NAMES = {0: "", 1: "Вперёд", 2: "Назад"}
sys.modules["engines.tailor.planner"] = _planner_stub

# ──────────────────────────────────────────────────────────────────
# 3. Load real modules by file path, dependency order.
#    Pre-register in sys.modules BEFORE exec so cross-imports resolve.
# ──────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # register before exec to break import cycles
    spec.loader.exec_module(mod)
    return mod


_port_mod = _load_module("engines.tailor.port", "engines/tailor/port.py")
_schemas = _load_module("engines.tailor.schemas", "engines/tailor/schemas.py")
_platform_adapter = _load_module(
    "engines.tailor.platform_adapter", "engines/tailor/platform_adapter.py"
)
_bot_adapter_mod = _load_module(
    "engines.tailor.bot_adapter", "engines/tailor/bot_adapter.py"
)
_router_mod = _load_module("engines.router", "engines/router.py")

LessonRequest = _schemas.LessonRequest
LessonPacket = _schemas.LessonPacket
PlatformTailorAdapter = _platform_adapter.PlatformTailorAdapter
EngineRouter = _router_mod.EngineRouter
BotTailorAdapter = _bot_adapter_mod.BotTailorAdapter

# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────

_MOCK_LESSON = {
    "element_id": "CAT.001.A3",
    "element_type": "worldview",
    "area": 1,
    "area_name": "Знания",
    "target_depth": 1,
    "impact_type": "worldview",
    "stage": 2,
    "decision_log": "stub-f1: mock lesson",
}

_MOCK_GENERATED = {
    "intro": "Добро пожаловать.",
    "core": "Основной материал.",
    "practice": "Практика.",
    "reflection": "Рефлексия.",
    "word_count": 10,
}

_MOCK_RESPONSE = {"lesson": _MOCK_LESSON, "generated": _MOCK_GENERATED, "error": None}


def _make_patched_client(handler):
    """Return an httpx.AsyncClient subclass that uses handler as transport."""

    class _Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(**kwargs)

    return _Client


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        headers={"content-type": "application/json"},
        content=json.dumps(_MOCK_RESPONSE).encode(),
    )


# ──────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_platform_adapter_returns_lesson_packet() -> None:
    """assemble_lesson() возвращает LessonPacket с непустыми lesson и generated."""
    adapter = PlatformTailorAdapter(url="http://tailor-stub", shared_secret="secret")

    with mock.patch.object(httpx, "AsyncClient", _make_patched_client(_ok_handler)):
        packet = await adapter.assemble_lesson(LessonRequest(user_id=42, mode="worldview"))

    assert isinstance(packet, LessonPacket)
    assert packet.error is None
    assert packet.lesson["element_id"] == "CAT.001.A3"
    assert packet.generated["word_count"] == 10
    assert "intro" in packet.generated


@pytest.mark.asyncio
async def test_platform_adapter_sends_shared_secret() -> None:
    """assemble_lesson() передаёт X-Shared-Secret в заголовке."""
    received_headers: dict = {}

    def _capturing_handler(request: httpx.Request) -> httpx.Response:
        received_headers.update(dict(request.headers))
        return _ok_handler(request)

    adapter = PlatformTailorAdapter(url="http://tailor-stub", shared_secret="my-secret-123")

    with mock.patch.object(httpx, "AsyncClient", _make_patched_client(_capturing_handler)):
        await adapter.assemble_lesson(LessonRequest(user_id=42))

    assert received_headers.get("x-shared-secret") == "my-secret-123"


@pytest.mark.asyncio
async def test_platform_adapter_raises_connection_error_on_connect_fail() -> None:
    """assemble_lesson() поднимает ConnectionError при сетевом сбое (не HTTPStatusError)."""

    def _fail_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = PlatformTailorAdapter(url="http://unreachable", shared_secret="s")

    with mock.patch.object(httpx, "AsyncClient", _make_patched_client(_fail_handler)):
        with pytest.raises(ConnectionError):
            await adapter.assemble_lesson(LessonRequest(user_id=1))


def test_engine_router_returns_platform_adapter_when_env_set(monkeypatch) -> None:
    """EngineRouter при TAILOR_ROUTE=platform возвращает PlatformTailorAdapter."""
    monkeypatch.setenv("TAILOR_ROUTE", "platform")
    monkeypatch.setenv("TAILOR_SERVICE_URL", "http://tailor-service.railway.app")
    monkeypatch.setenv("TAILOR_SHARED_SECRET", "secret")

    router = EngineRouter(bot=mock.MagicMock())
    adapter = router.get_tailor_adapter(user_id=99)

    assert isinstance(adapter, PlatformTailorAdapter)


def test_engine_router_falls_back_to_local_when_url_missing(monkeypatch) -> None:
    """EngineRouter делает fallback на BotTailorAdapter если TAILOR_SERVICE_URL не задан."""
    monkeypatch.setenv("TAILOR_ROUTE", "platform")
    monkeypatch.delenv("TAILOR_SERVICE_URL", raising=False)

    router = EngineRouter(bot=mock.MagicMock())
    adapter = router.get_tailor_adapter(user_id=99)

    assert isinstance(adapter, BotTailorAdapter)
