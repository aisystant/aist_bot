"""
IWE style enforcement for LLM responses before Telegram send.

Used by SafeBot (core/safe_bot.py) at transport layer — covers all send paths.

Style rule: em-dash (U+2014) is forbidden except in "X — это Y" construction.
"""

import re

# Placeholder for the allowed "X — это Y" em-dash construction.
# Unlikely in real text; multi-byte sentinel makes debugging easier than a bare NUL.
_EM_DASH_PRESERVE_PH = "\x00EMDASH\x00"

# Captures "... word — это ..." construction with any horizontal whitespace.
# [^\S\n]* = horizontal whitespace only; keeps newlines intact.
_ALLOWED_EM_DASH_RE = re.compile(r"(\w+)[^\S\n]*—[^\S\n]*(это\b)")

# [^\S\n] = horizontal whitespace only; avoids collapsing newlines around em-dash
_FORBIDDEN_EM_DASH_RE = re.compile(r"[^\S\n]*—[^\S\n]*")


def enforce_iwe_style(text: str) -> str:
    """Replace forbidden em-dashes per IWE style rules.

    Keeps "X — это Y" pattern intact and normalizes whitespace around it;
    replaces all other em-dashes with " - ".
    Uses horizontal-only whitespace match to preserve newlines in multiline text.
    """
    if not text:
        return text
    text = _ALLOWED_EM_DASH_RE.sub(r"\1" + _EM_DASH_PRESERVE_PH + r"\2", text)
    text = _FORBIDDEN_EM_DASH_RE.sub(" - ", text)
    text = text.replace(_EM_DASH_PRESERVE_PH, " — ")
    return text
