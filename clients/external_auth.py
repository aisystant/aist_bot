"""
Crypto helpers for external client token auth (WP-411 Ф2).

Generates, encrypts and hashes access/refresh token pairs for bot-mediated
delegation: one-time bootstrap code → ory_client_tokens table.

Env:
    EXTERNAL_AUTH_KEY   — Fernet key (32-byte URL-safe base64).
                          Generate: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    INTROSPECT_SECRET   — shared secret between gateway-mcp and bot
                          (X-Introspect-Secret header). Min 32 chars.
"""

import hashlib
import os
import secrets
from functools import lru_cache

from cryptography.fernet import Fernet


ACCESS_TOKEN_PREFIX = "ict_"   # IWE Client Token
REFRESH_TOKEN_PREFIX = "irt_"  # IWE Refresh Token


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    key = os.getenv("EXTERNAL_AUTH_KEY")
    if not key:
        raise RuntimeError("EXTERNAL_AUTH_KEY env var not set")
    return Fernet(key.encode())


def encrypt_token(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(enc: str) -> str:
    return _get_fernet().decrypt(enc.encode()).decode()


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def generate_access_token() -> tuple[str, str]:
    """Returns (plaintext, sha256_hash)."""
    plaintext = ACCESS_TOKEN_PREFIX + secrets.token_urlsafe(32)
    return plaintext, hash_token(plaintext)


def generate_refresh_token() -> tuple[str, str]:
    """Returns (plaintext, sha256_hash)."""
    plaintext = REFRESH_TOKEN_PREFIX + secrets.token_urlsafe(40)
    return plaintext, hash_token(plaintext)


def verify_introspect_secret(header_value: str) -> bool:
    """Constant-time compare against INTROSPECT_SECRET."""
    secret = os.getenv("INTROSPECT_SECRET", "")
    if not secret:
        return False
    return secrets.compare_digest(header_value, secret)
