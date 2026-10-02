"""Regression test for the search_knowledge("...") leak into user-visible answers.

Peer-session 2026-10-02 (WP-7, Kimi+Codex) traced a real pilot conversation
where the bot showed the user a raw, unfinished tool-call preamble instead of
its answer. Root cause: generate_with_tools() can exhaust max_tool_rounds
without ever hitting return/break inside the loop — the only way to reach
that point is for the last round's stop_reason to be "tool_use", meaning the
model's text in that round was written before it saw the tool's result. The
old fallback returned that unfinished text directly instead of going through
the force-text fallback that already existed for this exact case.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clients.claude import ClaudeClient


def _tool_use_round(query: str):
    """A response that asks for another tool call, with a leaked text preamble
    ahead of it — the shape seen in the real pilot conversation."""
    return {
        "stop_reason": "tool_use",
        "content": [
            {"type": "text", "text": f'search_knowledge("{query}")'},
            {"type": "tool_use", "id": f"call-{query}", "name": "search_knowledge",
             "input": {"query": query}},
        ],
    }


async def test_round_exhaustion_does_not_return_unfinished_text():
    client = ClaudeClient()
    calls = []

    async def fake_api_call(payload, inactivity_timeout=15):
        calls.append(payload)
        if len(calls) <= 2:
            # Both allowed rounds ask for another tool call — natural
            # exhaustion of max_tool_rounds=2, no return/break inside the loop.
            return _tool_use_round("системное мышление определение")
        # Force-text fallback: a real synthesized answer.
        return {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "Настоящий синтезированный ответ."}],
        }

    client._api_call_streaming_full = fake_api_call

    async def tool_executor(tool_name, tool_input):
        return "результаты поиска"

    answer = await client.generate_with_tools(
        system_prompt="system",
        messages=[{"role": "user", "content": "Что такое системное мышление?"}],
        tools=[{"name": "search_knowledge", "description": "...", "input_schema": {}}],
        tool_executor=tool_executor,
        max_tool_rounds=2,
    )

    assert answer == "Настоящий синтезированный ответ."
    assert "search_knowledge(" not in answer

    # Three calls total: the 2 exhausted rounds + the force-text fallback.
    assert len(calls) == 3

    force_text_payload = calls[2]
    assert "tools" not in force_text_payload

    # The force-text fallback's own precondition: conversation ends on a
    # valid, non-empty user turn (the last round's tool_results).
    last_message = force_text_payload["messages"][-1]
    assert last_message["role"] == "user"
    assert last_message["content"]
