"""
Template release notice: boundary checks for the announcement built by the template CI.

WP-529: the text of the announcement is generated and editorially checked in
FMT-exocortex-template (scripts/release-notice-build.py). The bot is a thin client:
it only verifies that the incoming HTML is safe to hand to Telegram and falls back to
a short deterministic notice otherwise. No LLM call and no changelog parsing here.
"""

import html
import re

TEMPLATE_REPO_URL = "https://github.com/TserenTserenov/FMT-exocortex-template"
TELEGRAM_MESSAGE_LIMIT = 4096

_VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?")
_SECURITY_VERSION_RE = re.compile(r"security ((?:[0-9a-f]{40}|[0-9a-f]{12}))")
# Telegram HTML subset accepted from CI; anything else rejects the whole text.
_ALLOWED_ATTRS = {"b": (), "i": (), "u": (), "s": (), "code": (), "pre": (), "a": ("href",)}
_TAG_RE = re.compile(r'<(/?)([a-zA-Z]+)((?:\s+[a-z]+="[^"<>]*")*)\s*>')
_ATTR_RE = re.compile(r'([a-z]+)="([^"<>]*)"')
_BARE_AMP_RE = re.compile(r"&(?!(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);)")

HOW_TO_UPDATE = (
    "<b>Как обновить:</b> скажите своему Claude <i>«обнови мой экзокортекс»</i> "
    "или выполните в терминале <code>bash update.sh</code>"
)


def parse_release_version(raw: str) -> str | None:
    """Extract a release version or the security workflow's commit label."""
    security = _SECURITY_VERSION_RE.fullmatch(raw or "")
    if security:
        return security.group(0)
    if (raw or "").startswith("security"):
        return None
    match = _VERSION_RE.search(raw or "")
    return match.group(0) if match else None


def release_url(version: str) -> str:
    security = _SECURITY_VERSION_RE.fullmatch(version)
    if security:
        return f"{TEMPLATE_REPO_URL}/commit/{security.group(1)}"
    return f"{TEMPLATE_REPO_URL}/releases/tag/v{version}"


def short_notice(version: str) -> str:
    """Fallback announcement without retelling: version, link to the release, how to update."""
    if _SECURITY_VERSION_RE.fullmatch(version):
        return (
            "🔒 <b>Срочное обновление безопасности шаблона IWE</b>\n\n"
            f'Что изменилось — <a href="{release_url(version)}">в описании коммита</a>.\n\n'
            f"{HOW_TO_UPDATE}"
        )
    return (
        f"🔄 <b>Обновление шаблона IWE {version}</b>\n\n"
        f'Что изменилось — <a href="{release_url(version)}">в описании выпуска</a>.\n\n'
        f"{HOW_TO_UPDATE}"
    )


def build_security_notice(version: str, changelog: str) -> tuple[str, str]:
    """Preserve the security lines as escaped text within Telegram's message limit."""
    if not changelog.strip():
        return short_notice(version), "short"
    suffix = f'\n\n<a href="{release_url(version)}">Описание коммита</a>'
    text = html.escape(changelog.strip())
    if len(text) + len(suffix) > TELEGRAM_MESSAGE_LIMIT:
        text = text[: TELEGRAM_MESSAGE_LIMIT - len(suffix) - 1]
        if text.rfind("&") > text.rfind(";"):
            text = text[: text.rfind("&")]
        text = text.rstrip() + "…"
    return text + suffix, "security"


def _plain_text_ok(chunk: str) -> bool:
    return "<" not in chunk and ">" not in chunk and not _BARE_AMP_RE.search(chunk)


def is_safe_telegram_html(text: str) -> bool:
    """True if text fits one Telegram message and uses only balanced whitelisted tags."""
    if not text or len(text) > TELEGRAM_MESSAGE_LIMIT or "```" in text:
        return False
    open_tags: list[str] = []
    pos = 0
    for match in _TAG_RE.finditer(text):
        if not _plain_text_ok(text[pos:match.start()]):
            return False
        closing, tag, attrs = match.group(1) == "/", match.group(2).lower(), match.group(3)
        if tag not in _ALLOWED_ATTRS:
            return False
        if closing:
            if attrs.strip() or not open_tags or open_tags.pop() != tag:
                return False
        else:
            pairs = _ATTR_RE.findall(attrs)
            if any(name not in _ALLOWED_ATTRS[tag] for name, _ in pairs):
                return False
            if tag == "a" and not any(name == "href" and value.startswith("https://") for name, value in pairs):
                return False
            open_tags.append(tag)
        pos = match.end()
    return _plain_text_ok(text[pos:]) and not open_tags


def build_template_notice(version: str, message_html: str) -> tuple[str, str]:
    """Return (text, kind): the CI-built text if it passes the boundary check, else the short notice."""
    if message_html and is_safe_telegram_html(message_html):
        return message_html, "built"
    return short_notice(version), "short"
