from __future__ import annotations

"""Shared pgp_sym_encrypt/decrypt helpers for OAuth-style secrets stored in TEXT columns.

WP-554 Б4: persona.user_integrations.access_token/refresh_token and
secrets.dt_tokens.{access_token,refresh_token,token} used to be written as
plaintext by some callers while secrets.github_connections (bytea) and part of
user_integrations (github rows) already encrypted with pgp_sym_encrypt. This
module makes the TEXT-column format (already used by
github.py::sync_github_to_user_integrations) reusable instead of repeated inline
SQL: 'pgp:' + base64(pgp_sym_encrypt(plaintext, key)).

Key: GITHUB_TOKEN_ENCRYPTION_KEY (Railway env) — same key already used for
secrets.github_connections and the github rows of user_integrations; reused
here rather than introducing a second key to manage.
"""

from config import GITHUB_TOKEN_ENCRYPTION_KEY, get_logger

logger = get_logger(__name__)

_PREFIX = "pgp:"
_warned_no_key = False


def _warn_if_no_key() -> None:
    global _warned_no_key
    if not GITHUB_TOKEN_ENCRYPTION_KEY and not _warned_no_key:
        logger.warning("GITHUB_TOKEN_ENCRYPTION_KEY не установлен — токены хранятся в незашифрованном виде")
        _warned_no_key = True


async def encrypt_text_token(conn, plaintext: str | None) -> str | None:
    """Encrypt a token for storage in a TEXT column.

    Returns the value unchanged if it is None or no key is configured
    (matches the degrade-gracefully behavior already used for
    secrets.github_connections).
    """
    if plaintext is None:
        return None
    if not GITHUB_TOKEN_ENCRYPTION_KEY:
        _warn_if_no_key()
        return plaintext
    return await conn.fetchval(
        "SELECT 'pgp:' || encode(public.pgp_sym_encrypt($1::text, $2::text), 'base64')",
        plaintext, GITHUB_TOKEN_ENCRYPTION_KEY,
    )


async def decrypt_text_token(conn, stored: str | None) -> str | None:
    """Decrypt a token read from a TEXT column.

    Legacy rows without the 'pgp:' prefix are passed through unchanged —
    same backward-compat rule as github.py::sync_github_to_user_integrations.

    Raises RuntimeError if the row IS encrypted but no key is configured to
    read it. Unlike encrypt_text_token (where "no key" is a safe degrade —
    the value is simply stored as plaintext), silently returning the raw
    ciphertext here would hand callers a mangled 'pgp:...' string that looks
    like a real secret instead of failing loudly (found by cold review,
    WP-554 Б4: this previously reached WakaTime/DT API calls as a bogus
    credential, and made validate_extension_token silently reject every
    token instead of raising something callers already handle).
    """
    if stored is None or not stored.startswith(_PREFIX):
        return stored
    if not GITHUB_TOKEN_ENCRYPTION_KEY:
        _warn_if_no_key()
        raise RuntimeError("GITHUB_TOKEN_ENCRYPTION_KEY не установлен — не могу расшифровать сохранённый токен")
    return await conn.fetchval(
        "SELECT public.pgp_sym_decrypt(decode($1, 'base64'), $2::text)::text",
        stored[len(_PREFIX):], GITHUB_TOKEN_ENCRYPTION_KEY,
    )
