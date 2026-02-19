"""
Markdown-aware message splitting for Telegram.

Telegram limit: 4096 chars per message. This module splits long text
by paragraphs/lines, never breaking inside Markdown entities.

For LLM-generated content, use prepare_html_parts() which converts
Markdown → HTML (deterministic, crash-proof parse_mode="HTML").
"""

import re
import logging

logger = logging.getLogger(__name__)

MAX_TG_LEN = 4000  # 4096 limit, 96 chars margin for safety

# Placeholder for \n\n inside code blocks during split
_CODE_BLOCK_NL = "\x00CB\x00"


def prepare_html_parts(text: str, max_len: int = MAX_TG_LEN) -> list[str]:
    """Convert Markdown → HTML, then split into Telegram-safe chunks.

    Pipeline:
    1. md_to_html(text)               — convert full text to safe HTML
    2. split_message_safe(html_text)  — split by paragraphs (pre-block-aware)

    HTML parse_mode is deterministic: invalid markup shows as plain text,
    never crashes with TelegramBadRequest. Use with parse_mode="HTML".
    """
    from helpers.markdown_to_html import md_to_html

    html = md_to_html(text)
    return split_message_safe(html, max_len)


def prepare_markdown_parts(text: str, max_len: int = MAX_TG_LEN) -> list[str]:
    """Legacy wrapper — calls prepare_html_parts().

    Kept for backward compatibility during migration.
    Callers should switch to prepare_html_parts() + parse_mode="HTML".
    """
    return prepare_html_parts(text, max_len)


def split_message_safe(text: str, max_len: int = MAX_TG_LEN) -> list[str]:
    """Split text into chunks by paragraphs, protecting code blocks.

    Works with both raw Markdown (``` blocks) and HTML (<pre> blocks).

    Strategy:
    1. Protect code blocks from being split by \\n\\n
    2. Split by double newlines (paragraphs)
    3. If a paragraph exceeds max_len, split by single newlines (lines)
    4. If a line exceeds max_len, hard-split at max_len (last resort)
    5. Restore code block newlines

    Returns list of chunks, each <= max_len.
    """
    if len(text) <= max_len:
        return [text]

    # Protect code blocks: replace \n\n inside ``` and <pre> regions
    protected = _protect_code_blocks(text)

    paragraphs = protected.split('\n\n')
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        candidate = (current + '\n\n' + para) if current else para

        if len(candidate) <= max_len:
            current = candidate
            continue

        # Current chunk is full — flush it
        if current:
            chunks.append(current)
            current = ""

        # This paragraph alone fits
        if len(para) <= max_len:
            current = para
            continue

        # Code block paragraph — keep atomic even if over max_len
        # (better to send a slightly oversized message than break code)
        if _CODE_BLOCK_NL in para or '```' in para or '<pre>' in para:
            chunks.append(para)
            continue

        # Paragraph too long — split by lines
        lines = para.split('\n')
        for line in lines:
            candidate = (current + '\n' + line) if current else line

            if len(candidate) <= max_len:
                current = candidate
                continue

            if current:
                chunks.append(current)
                current = ""

            # Single line fits
            if len(line) <= max_len:
                current = line
                continue

            # Line too long — hard split (last resort)
            for sub in _hard_split(line, max_len):
                chunks.append(sub)

    if current:
        chunks.append(current)

    # Restore code block newlines in all chunks
    result = [c.replace(_CODE_BLOCK_NL, '\n\n') for c in chunks]
    return result if result else [text[:max_len]]


# Keep old name as alias for backward compatibility
split_markdown_safe = split_message_safe


def _protect_code_blocks(text: str) -> str:
    """Replace \\n\\n inside code blocks with placeholder to keep them atomic.

    Handles both Markdown (```) and HTML (<pre>) code blocks.
    """
    def _replace_nl(match: re.Match) -> str:
        return match.group(0).replace('\n\n', _CODE_BLOCK_NL)

    text = re.sub(r'```[\s\S]*?```', _replace_nl, text)
    text = re.sub(r'<pre>[\s\S]*?</pre>', _replace_nl, text)
    return text


def _hard_split(text: str, max_len: int) -> list[str]:
    """Hard-split a long line by words, falling back to character split."""
    words = text.split(' ')
    chunks: list[str] = []
    current = ""

    for word in words:
        candidate = (current + ' ' + word) if current else word
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # Single word exceeds max_len — character split
            if len(word) > max_len:
                for i in range(0, len(word), max_len):
                    chunks.append(word[i:i + max_len])
                current = ""
            else:
                current = word

    if current:
        chunks.append(current)

    return chunks


def truncate_safe(text: str, max_len: int = MAX_TG_LEN, suffix: str = "\n\n... (обрезано)") -> str:
    """Truncate text at paragraph/line boundary, not mid-entity."""
    if len(text) <= max_len:
        return text

    target = max_len - len(suffix)
    # Try to cut at paragraph boundary
    cut = text.rfind('\n\n', 0, target)
    if cut == -1:
        # Try line boundary
        cut = text.rfind('\n', 0, target)
    if cut == -1:
        # Try word boundary
        cut = text.rfind(' ', 0, target)
    if cut == -1:
        cut = target

    return text[:cut] + suffix
