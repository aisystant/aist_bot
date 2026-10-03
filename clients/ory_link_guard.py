"""Thin client for the user-profile-service Ory account-link decision (WP-5 Ф58.4)."""

from dataclasses import dataclass
from typing import Optional

import aiohttp

from urllib.parse import urlparse

from config.settings import BOT_LINK_GUARD_SECRET, USER_PROFILE_SERVICE_URL


class ExistingOryAccount(Exception):
    """A different Ory account with the verified email owns an active contract."""


class VerifiedEmailRequired(Exception):
    """Kratos has no verified primary email for this Ory account."""


class OryLinkGuardUnavailable(Exception):
    """The decision cannot be trusted; the bot must not write the link."""


@dataclass(frozen=True)
class OryLinkApproval:
    sub: str
    email: Optional[str]


async def check_ory_link(access_token: str, expected_sub: str) -> OryLinkApproval:
    """Ask the profile service to decide before link_ory or token storage.

    The bot supplies only the user's access token. The service obtains the
    verified email itself, then checks contracts; a caller-supplied email or sub
    cannot affect its decision.
    """
    parsed_url = urlparse(USER_PROFILE_SERVICE_URL)
    if (parsed_url.scheme != "https" or not parsed_url.netloc or parsed_url.username or parsed_url.password
            or not BOT_LINK_GUARD_SECRET or not access_token):
        raise OryLinkGuardUnavailable("guard_not_configured")

    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{USER_PROFILE_SERVICE_URL}/api/v1/ory/link-guard",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "X-Bot-Link-Guard-Secret": BOT_LINK_GUARD_SECRET,
                },
                allow_redirects=False,
            ) as response:
                if response.status == 409:
                    raise ExistingOryAccount()
                if response.status == 422:
                    raise VerifiedEmailRequired()
                if response.status != 200:
                    raise OryLinkGuardUnavailable(f"guard_status_{response.status}")
                payload = await response.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise OryLinkGuardUnavailable("guard_transport_error") from exc

    if not isinstance(payload, dict) or payload.get("allowed") is not True or payload.get("sub") != expected_sub:
        raise OryLinkGuardUnavailable("guard_response_mismatch")
    email = payload.get("email")
    if email is not None and not isinstance(email, str):
        raise OryLinkGuardUnavailable("guard_invalid_email")
    return OryLinkApproval(sub=expected_sub, email=email)
