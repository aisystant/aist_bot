"""
Deterministic Markdown → Telegram HTML converter.

Converts Claude-generated Markdown to Telegram-safe HTML.
HTML parse_mode is deterministic: invalid markup shows as plain text,
never crashes with TelegramBadRequest.

Algorithm:
1. Extract code blocks (``` ```) → protect from processing
2. Extract inline code (` `) → protect from processing
3. html.escape() everything else
4. Convert: *bold* → <b>, _italic_ → <i>, [text](url) → <a>
5. Restore code blocks → <pre>, inline code → <code>

Usage:
    from helpers.markdown_to_html import md_to_html
    await bot.send_message(chat_id, md_to_html(text), parse_mode="HTML")
"""

import html
import re

# Placeholder prefix (unlikely in real text)
_PH = "\x00PH"


def md_to_html(text: str) -> str:
    """Convert Markdown to Telegram-safe HTML.

    Safe: if conversion fails on any part, that part stays as
    html-escaped plain text (no crash, just no formatting).
    """
    if not text:
        return text

    placeholders: list[str] = []

    def _save_code_block(match: re.Match) -> str:
        """Save code block as <pre> HTML, protect from further processing."""
        idx = len(placeholders)
        content = match.group(1) or ""
        # html.escape the content inside code block
        placeholders.append(f"<pre>{html.escape(content)}</pre>")
        return f"{_PH}{idx}{_PH}"

    def _save_inline_code(match: re.Match) -> str:
        """Save inline code as <code> HTML, protect from further processing."""
        idx = len(placeholders)
        content = match.group(1)
        placeholders.append(f"<code>{html.escape(content)}</code>")
        return f"{_PH}{idx}{_PH}"

    # Phase 1: Extract code blocks (``` ... ```)
    # Match with optional language tag: ```python\n...\n```
    text = re.sub(r"```(?:\w*\n)?([\s\S]*?)```", _save_code_block, text)

    # Phase 2: Extract inline code (` ... `)
    text = re.sub(r"`([^`]+)`", _save_inline_code, text)

    # Phase 3: html.escape() everything remaining
    # (placeholders contain \x00 which html.escape won't touch)
    text = html.escape(text)

    # Phase 4: Convert Markdown formatting → HTML tags
    # Links: [text](url) → <a href="url">text</a>
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        r'<a href="\2">\1</a>',
        text
    )

    # Bold: **text** (double asterisk first, before single)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

    # Bold: *text* (single asterisk)
    text = re.sub(r"\*(.+?)\*", r"<b>\1</b>", text)

    # Italic: _text_
    text = re.sub(r"_(.+?)_", r"<i>\1</i>", text)

    # Phase 5: Restore placeholders
    for i, replacement in enumerate(placeholders):
        text = text.replace(f"{_PH}{i}{_PH}", replacement)

    return text
