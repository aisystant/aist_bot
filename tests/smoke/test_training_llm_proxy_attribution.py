"""
Тесты: атрибуция стоимости в llm-proxy для training (WP-262 В1, Ф2).

AR.218 — единый LLM-ingress уже используется (IWE_LLM_PROXY_URL на боте),
Ф2 добавляет только X-User-ID для per-account cost visibility в дашборде
прокси. Не отдельный сервис — training остаётся в боте, planner.py уже
проксируется через существующий ClaudeClient.

Два уровня:
1. ClaudeClient._request_headers — X-User-ID появляется только при
   заданном proxy_secret И переданном account_id.
2. planner.generate_assignment_text/generate_child_assignment_text —
   account_id реально доходит до claude.generate().
"""

import pytest
from unittest.mock import AsyncMock, patch

from clients.claude import ClaudeClient
from engines.training import planner


# ─── 1. _request_headers: X-User-ID только с proxy_secret + account_id ───

def test_request_headers_includes_user_id_when_proxied():
    client = ClaudeClient()
    client._proxy_secret = "shared-secret"

    headers = client._request_headers(account_id="ory-uuid-123")

    assert headers["X-User-ID"] == "ory-uuid-123"
    assert headers["x-iwe-internal-secret"] == "shared-secret"


def test_request_headers_omits_user_id_without_account_id():
    client = ClaudeClient()
    client._proxy_secret = "shared-secret"

    headers = client._request_headers(account_id=None)

    assert "X-User-ID" not in headers


def test_request_headers_omits_user_id_without_proxy():
    """Без прокси (прямой вызов Anthropic) — X-User-ID бессмыслен, не добавлять."""
    client = ClaudeClient()
    client._proxy_secret = ""

    headers = client._request_headers(account_id="ory-uuid-123")

    assert "X-User-ID" not in headers


# ─── 2. planner-функции передают account_id из intern в claude.generate() ───

@pytest.mark.asyncio
async def test_generate_assignment_text_passes_account_id_from_intern():
    cell_data = {"forms": {"postformal": "form"}, "transfer_test": "tt", "can_do": [], "domains": []}
    intern = {"language": "ru", "name": "Тест", "dt_user_id": "ory-uuid-123"}

    with patch("engines.training.planner.claude.generate", new_callable=AsyncMock) as mock_generate:
        mock_generate.return_value = "задание"

        await planner.generate_assignment_text(cell_data, "postformal", intern, "Аксиоматичность", 2)

        assert mock_generate.await_args.kwargs["account_id"] == "ory-uuid-123"


@pytest.mark.asyncio
async def test_generate_assignment_text_account_id_none_without_intern():
    cell_data = {"forms": {}, "transfer_test": "tt", "can_do": [], "domains": []}

    with patch("engines.training.planner.claude.generate", new_callable=AsyncMock) as mock_generate:
        mock_generate.return_value = "задание"

        await planner.generate_assignment_text(cell_data, "postformal", None, "Аксиоматичность", 2)

        assert mock_generate.await_args.kwargs["account_id"] is None


@pytest.mark.asyncio
async def test_generate_child_assignment_text_passes_explicit_account_id():
    """account_id ребёнка = ory_id взрослого (передаётся явно, не из intern — child-профиль не intern)."""
    cell_data = {"forms": {}, "transfer_test": "tt", "can_do": [], "domains": []}

    with patch("engines.training.planner.claude.generate", new_callable=AsyncMock) as mock_generate:
        mock_generate.return_value = "карточка"

        await planner.generate_child_assignment_text(
            cell_data, "concrete_operational", "Ваня", "Структура", 1,
            lang="ru", account_id="ory-uuid-456",
        )

        assert mock_generate.await_args.kwargs["account_id"] == "ory-uuid-456"
