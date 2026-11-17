"""The gateway error code belongs to the call's own task, not to the shared client (WP-417 Ф7).

One GatewayMCPClient serves every user. With the code kept in an instance field, a concurrent call
of another user (the optional /progress reads) overwrote the code /consent and /diagnose read right
after their own call. Neither scenario touches the network: "not_authorized" comes from a user with
no Ory token, "circuit_open" from the open circuit breaker.
"""

import asyncio
import time

from clients.gateway_mcp import GatewayMCPClient

USER_WITHOUT_TOKEN = 101


def make_client() -> GatewayMCPClient:
    client = GatewayMCPClient("http://gateway.invalid/mcp")
    # Negative token cache: the call is refused as not_authorized without a database lookup.
    client._no_token_users[USER_WITHOUT_TOKEN] = time.time()
    return client


def open_circuit(client: GatewayMCPClient) -> None:
    client._circuit_open = True
    client._last_failure = time.time()


def test_concurrent_call_of_another_user_does_not_overwrite_the_code():
    async def scenario():
        client = make_client()
        first_call_done = asyncio.Event()
        second_call_done = asyncio.Event()

        async def consent_user():
            result = await client._call("grant_consent", {}, telegram_user_id=USER_WITHOUT_TOKEN)
            assert result is None
            first_call_done.set()
            await second_call_done.wait()
            return client.last_call_error

        async def progress_user():
            await first_call_done.wait()
            open_circuit(client)
            await client._call("dt_read_activity", {}, telegram_user_id=None, best_effort=True)
            code = client.last_call_error
            second_call_done.set()
            return code

        return await asyncio.gather(consent_user(), progress_user())

    consent_code, progress_code = asyncio.run(scenario())
    assert consent_code == "not_authorized"
    assert progress_code == "circuit_open"


def test_code_is_reset_by_the_next_call_of_the_same_task():
    async def scenario():
        client = make_client()
        await client._call("grant_consent", {}, telegram_user_id=USER_WITHOUT_TOKEN)
        after_refusal = client.last_call_error
        open_circuit(client)
        await client._call("grant_consent", {}, telegram_user_id=None)
        return after_refusal, client.last_call_error

    after_refusal, after_circuit = asyncio.run(scenario())
    assert after_refusal == "not_authorized"
    assert after_circuit == "circuit_open"


def test_new_task_starts_without_an_inherited_failure():
    async def scenario():
        client = make_client()

        async def failing_call():
            await client._call("grant_consent", {}, telegram_user_id=USER_WITHOUT_TOKEN)
            return client.last_call_error

        inside = await asyncio.create_task(failing_call())
        return inside, client.last_call_error

    inside, outside = asyncio.run(scenario())
    assert inside == "not_authorized"
    assert outside == "ok"
