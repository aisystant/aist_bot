"""
Тесты: бот помечает каждый вызов llm-proxy как «aist-bot» (WP-568 Ф12, 09.11.2026).

Без отметки вызовы без X-User-ID (пре-генерация, дайджесты) шлюз считал
«внутренней автоматикой» — дневной потолок для автоматики ударил бы по читателям.
"""

from clients.claude import (
    PROXY_CONSUMER,
    PROXY_CONSUMER_HEADER,
    ClaudeClient,
    resolve_proxy_endpoint,
)


def test_client_marks_proxied_call_without_user():
    client = ClaudeClient()
    client._proxy_secret = "shared-secret"

    headers = client._request_headers(account_id=None)

    assert headers[PROXY_CONSUMER_HEADER] == "aist-bot"


def test_client_marks_proxied_call_with_user():
    client = ClaudeClient()
    client._proxy_secret = "shared-secret"

    headers = client._request_headers(account_id="ory-uuid-123")

    assert headers[PROXY_CONSUMER_HEADER] == PROXY_CONSUMER
    assert headers["X-User-ID"] == "ory-uuid-123"


def test_client_direct_call_has_no_mark():
    client = ClaudeClient()
    client._proxy_secret = ""

    assert PROXY_CONSUMER_HEADER not in client._request_headers(account_id=None)


def test_one_off_endpoint_marks_proxied_call(monkeypatch):
    monkeypatch.setenv("PROXY_SHARED_SECRET", "shared-secret")

    _, headers = resolve_proxy_endpoint("api-key")

    assert headers[PROXY_CONSUMER_HEADER] == "aist-bot"


def test_one_off_endpoint_direct_call_has_no_mark(monkeypatch):
    monkeypatch.delenv("PROXY_SHARED_SECRET", raising=False)

    _, headers = resolve_proxy_endpoint("api-key")

    assert PROXY_CONSUMER_HEADER not in headers
