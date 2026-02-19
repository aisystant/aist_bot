"""
Markdown sanitizer for Telegram's Markdown v1 parser.

Fixes unclosed entities that would cause TelegramBadRequest: can't parse entities.
Handles: *bold*, _italic_, `code`, ```code blocks```, [links](url).

Usage:
    from helpers.markdown_sanitizer import sanitize_markdown
    clean = sanitize_markdown(text)  # safe to send with parse_mode="Markdown"
"""

import re
import logging

logger = logging.getLogger(__name__)

# Placeholder prefix (unlikely to appear in real text)
_PH = "\x00MD"


def sanitize_markdown(text: str) -> str:
    """Fix unclosed Markdown entities for Telegram Markdown v1.

    Algorithm:
    1. Extract code blocks (```) → placeholders (content inside untouched)
    2. Extract inline code (`) → placeholders (content inside untouched)
    3. Fix broken links [text](url)
    4. Close unclosed * (bold) and _ (italic)
    5. Restore placeholders
    """
    if not text:
        return text

    placeholders: list[str] = []

    def _save(match: re.Match) -> str:
        idx = len(placeholders)
        placeholders.append(match.group(0))
        return f"{_PH}{idx}{_PH}"

    # Phase 1: Protect code blocks (``` ... ```)
    # Match triple backtick blocks (with optional language tag)
    text = re.sub(r'```[\s\S]*?```', _save, text)

    # Unclosed code block: ``` without closing → close at end
    if '```' in text:
        text = text + '\n```'
        # Re-protect the now-closed block
        text = re.sub(r'```[\s\S]*?```', _save, text)

    # Phase 2: Protect inline code (` ... `)
    text = re.sub(r'`[^`]+`', _save, text)

    # Unclosed inline code: odd number of ` remaining
    remaining_backticks = text.count('`')
    if remaining_backticks % 2 != 0:
        # Find the last unclosed ` and close it at end of line/text
        last_bt = text.rfind('`')
        # Append closing backtick
        text = text + '`'
        # Re-protect
        text = re.sub(r'`[^`]+`', _save, text)

    # Phase 3: Fix broken links
    # Valid links: [text](url) — keep them
    text = re.sub(r'\[[^\]]+\]\([^)]+\)', _save, text)

    # Strip orphaned [ without matching ](url)
    # Only strip [ that don't have a matching ] before next [
    text = _fix_orphaned_brackets(text)

    # Phase 4: Close unclosed bold (*) and italic (_)
    text = _close_unclosed_markers(text, '*')
    text = _close_unclosed_markers(text, '_')

    # Phase 5: Restore placeholders (reverse order to handle nested)
    for i in range(len(placeholders) - 1, -1, -1):
        text = text.replace(f"{_PH}{i}{_PH}", placeholders[i])

    return text


def _fix_orphaned_brackets(text: str) -> str:
    """Remove orphaned [ and ] that aren't part of valid links."""
    # Already-valid links were replaced with placeholders in Phase 3
    # Any remaining [ or ] are orphaned — strip them
    result = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '[':
            # Check if this starts a valid link pattern [...](...) ahead
            close_bracket = text.find(']', i + 1)
            if close_bracket != -1 and close_bracket + 1 < len(text) and text[close_bracket + 1] == '(':
                close_paren = text.find(')', close_bracket + 2)
                if close_paren != -1:
                    # Valid link — keep as is
                    result.append(text[i:close_paren + 1])
                    i = close_paren + 1
                    continue
            # Orphaned [ — strip it
            i += 1
            continue
        elif ch == ']' and (i + 1 >= len(text) or text[i + 1] != '('):
            # Orphaned ] — strip it
            i += 1
            continue
        result.append(ch)
        i += 1
    return ''.join(result)


def _close_unclosed_markers(text: str, marker: str) -> str:
    """Close unclosed bold (*) or italic (_) markers.

    Telegram Markdown v1 treats * and _ as toggle markers.
    Odd count = unclosed entity → append closing marker.
    """
    # Count markers that are NOT inside placeholders
    count = 0
    i = 0
    while i < len(text):
        # Skip placeholders
        if text[i:i + len(_PH)] == _PH:
            end = text.find(_PH, i + len(_PH))
            if end != -1:
                i = end + len(_PH)
                continue
        if text[i] == marker:
            count += 1
        i += 1

    if count % 2 != 0:
        # Odd count — append closing marker
        text = text + marker

    return text
