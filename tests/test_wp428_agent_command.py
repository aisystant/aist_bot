"""WP-428 Ф5 — bot-side unit tests for /agent: arg parsing + SESSION executor field.

Pure-function tests (no aiogram message mocks). The live end-to-end path
(Telegram → SESSION file → dispatcher → runtime) needs a running bot + dispatcher
and is covered by the manual smoke in the РП card.
"""
import handlers.external_session as es


def test_parse_agent_args_explicit_executor():
    assert es._parse_agent_args("kimi do X") == ("kimi", "do X")
    assert es._parse_agent_args("claude fix the bug") == ("claude", "fix the bug")
    assert es._parse_agent_args("hermes привет") == ("hermes", "привет")


def test_parse_agent_args_case_insensitive():
    assert es._parse_agent_args("KiMi Do X") == ("kimi", "Do X")


def test_parse_agent_args_default_when_no_executor():
    # First token is not a known executor → whole string is text, default claude.
    assert es._parse_agent_args("just some text") == ("claude", "just some text")
    assert es._parse_agent_args("fix") == ("claude", "fix")


def test_parse_agent_args_executor_without_text():
    assert es._parse_agent_args("kimi") == ("kimi", "")


def test_parse_agent_args_trims_whitespace():
    assert es._parse_agent_args("  some text  ") == ("claude", "some text")


def test_meta_content_writes_executor_field():
    meta = es._meta_content("SESSION-abc", 123, "2026-06-20T00:00:00Z", executor="kimi")
    assert "executor: kimi" in meta


def test_meta_content_executor_defaults_to_claude():
    meta = es._meta_content("SESSION-abc", 123, "2026-06-20T00:00:00Z")
    assert "executor: claude" in meta
