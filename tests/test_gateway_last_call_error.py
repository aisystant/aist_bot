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


def test_call_of_another_user_between_call_and_read_does_not_overwrite_the_code():
    # Two tasks of the same client; the events force the order that loses the code with a
    # shared field: user A calls, user B calls, user A reads.
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


def test_code_set_inside_a_child_task_does_not_reach_the_caller():
    # A child task gets a copy of the caller's context: it sees the caller's code, and what it
    # sets stays in the child (the reason to read last_call_error right after awaiting the call).
    async def scenario():
        client = make_client()
        open_circuit(client)
        await client._call("grant_consent", {}, telegram_user_id=None)

        async def child_call():
            inherited = client.last_call_error
            client._circuit_open = False
            await client._call("grant_consent", {}, telegram_user_id=USER_WITHOUT_TOKEN)
            return inherited, client.last_call_error

        inherited, inside = await asyncio.create_task(child_call())
        return inherited, inside, client.last_call_error

    inherited, inside, caller = asyncio.run(scenario())
    assert inherited == "circuit_open"
    assert inside == "not_authorized"
    assert caller == "circuit_open"
