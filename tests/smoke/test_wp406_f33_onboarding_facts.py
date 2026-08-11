"""
WP-406 Ф33 §4: read-контракт GET /internal/onboarding-facts.

Контракт: concept-checklist-data-ownership-2026-08-10.md §4 + консенсус
пир-сессии 2026-08-11-25-wp406-onboarding-facts-read (Claude+Codex, 3 хода):
HMAC над канонической строкой (dual-key), окно ±300с, in-process anti-replay,
вокабула без расширения (pending_evaluation = наблюдаемое «не сделано»,
unknown = сбой чтения или пробел наблюдаемости), guide_issued из status='done'
очереди рендера, meeting_held всегда unknown (стола нет, С2).

Слои:
  handler  — подпись/timestamp/валидация/replay/shape (make_mocked_request,
             collect_onboarding_facts замокан)
  queries  — маппинг строк БД в статусы фактов (пулы замоканы)
"""

import hashlib
import hmac as hmac_mod
import json
import time
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

SECRET = "test-onboarding-secret"
PREV_SECRET = "old-onboarding-secret"
ACCOUNT = str(uuid.uuid4())
_DT = datetime(2026, 8, 11, 12, 0, 0)

FACTS_STUB = {
    "telegram_linked": {"status": "confirmed", "source": "persona.ory_identity",
                        "occurred_at": _DT.isoformat()},
    "guide_issued": {"status": "pending_evaluation",
                     "source": "learning.guide_render_queue", "occurred_at": None},
    "meeting_held": {"status": "unknown", "source": None, "occurred_at": None},
    "trajectory_confirmed": {"status": "pending_evaluation",
                             "source": "development.user_state.x3_completed_at",
                             "occurred_at": None},
}


def _sign(account_id: str, ts: int, key: str = SECRET) -> str:
    canonical = f"GET\n/internal/onboarding-facts\nuser_id={account_id}\n{ts}"
    return "sha256=" + hmac_mod.new(key.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def _request(account_id: str = ACCOUNT, ts: int = None, sig: str = None,
             query: str = None):
    ts = int(time.time()) if ts is None else ts
    sig = _sign(account_id, ts) if sig is None else sig
    query = f"user_id={account_id}" if query is None else query
    return make_mocked_request(
        "GET", f"/internal/onboarding-facts?{query}",
        headers={"X-Onboarding-Timestamp": str(ts), "X-Onboarding-Signature": sig},
    )


def _env(**extra):
    env = {"GATEWAY_ONBOARDING_READ_SECRET": SECRET}
    env.update(extra)
    return patch.dict("os.environ", env, clear=False)


@pytest.fixture(autouse=True)
def _clean_replay_cache():
    import oauth_server
    oauth_server._ONBOARDING_REPLAY_CACHE.clear()
    yield
    oauth_server._ONBOARDING_REPLAY_CACHE.clear()


# ─── handler: happy path и shape ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_valid_signature_returns_contract_v1():
    from oauth_server import onboarding_facts_handler
    with _env(), patch("db.queries.onboarding_facts.collect_onboarding_facts",
                       new_callable=AsyncMock, return_value=FACTS_STUB):
        resp = await onboarding_facts_handler(_request())
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["contract_version"] == "onboarding-facts-v1"
    assert body["user_id"] == ACCOUNT
    assert set(body["facts"]) == {"telegram_linked", "guide_issued",
                                  "meeting_held", "trajectory_confirmed"}
    for fact in body["facts"].values():
        assert set(fact) == {"status", "source", "occurred_at"}


@pytest.mark.asyncio
async def test_previous_secret_accepted_during_rotation():
    from oauth_server import onboarding_facts_handler
    ts = int(time.time())
    with _env(GATEWAY_ONBOARDING_READ_SECRET_PREVIOUS=PREV_SECRET), \
         patch("db.queries.onboarding_facts.collect_onboarding_facts",
               new_callable=AsyncMock, return_value=FACTS_STUB):
        resp = await onboarding_facts_handler(
            _request(ts=ts, sig=_sign(ACCOUNT, ts, key=PREV_SECRET)))
    assert resp.status == 200


# ─── handler: отказы ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_secret_configured_503():
    from oauth_server import onboarding_facts_handler
    with patch.dict("os.environ", {"GATEWAY_ONBOARDING_READ_SECRET": ""}, clear=False):
        resp = await onboarding_facts_handler(_request())
    assert resp.status == 503


@pytest.mark.asyncio
async def test_invalid_signature_403():
    from oauth_server import onboarding_facts_handler
    with _env():
        resp = await onboarding_facts_handler(_request(sig="sha256=" + "0" * 64))
    assert resp.status == 403


@pytest.mark.asyncio
async def test_expired_timestamp_403():
    from oauth_server import onboarding_facts_handler
    ts = int(time.time()) - 301
    with _env():
        resp = await onboarding_facts_handler(_request(ts=ts))
    assert resp.status == 403


@pytest.mark.asyncio
async def test_unicode_digit_timestamp_403_not_500():
    """review-01 Critical: U+00B2 проходит isdigit(), но роняет int() до auth."""
    from oauth_server import onboarding_facts_handler
    with _env():
        resp = await onboarding_facts_handler(
            _request(ts=0, sig="sha256=" + "0" * 64).clone(
                headers={"X-Onboarding-Timestamp": "²",
                         "X-Onboarding-Signature": "sha256=" + "0" * 64}))
    assert resp.status == 403


@pytest.mark.asyncio
async def test_huge_digit_timestamp_403_not_500():
    """review-01 Critical: >4300 цифр = ValueError int-лимита Python 3.11."""
    from oauth_server import onboarding_facts_handler
    with _env():
        resp = await onboarding_facts_handler(
            _request(ts=0, sig="sha256=" + "0" * 64).clone(
                headers={"X-Onboarding-Timestamp": "9" * 5000,
                         "X-Onboarding-Signature": "sha256=" + "0" * 64}))
    assert resp.status == 403


@pytest.mark.asyncio
async def test_replayed_signature_403():
    from oauth_server import onboarding_facts_handler
    ts = int(time.time())
    with _env(), patch("db.queries.onboarding_facts.collect_onboarding_facts",
                       new_callable=AsyncMock, return_value=FACTS_STUB):
        first = await onboarding_facts_handler(_request(ts=ts))
        second = await onboarding_facts_handler(_request(ts=ts))
    assert first.status == 200
    assert second.status == 403


@pytest.mark.asyncio
async def test_non_uuid_user_id_400():
    from oauth_server import onboarding_facts_handler
    with _env():
        resp = await onboarding_facts_handler(
            _request(query="user_id=1;DROP", sig=_sign("1;DROP", int(time.time()))))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_duplicate_user_id_params_400():
    from oauth_server import onboarding_facts_handler
    with _env():
        resp = await onboarding_facts_handler(
            _request(query=f"user_id={ACCOUNT}&user_id={uuid.uuid4()}"))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_oversized_query_400_before_hmac():
    from oauth_server import onboarding_facts_handler
    with _env():
        resp = await onboarding_facts_handler(
            _request(query=f"user_id={ACCOUNT}&pad=" + "x" * 300))
    assert resp.status == 400


# ─── queries: маппинг строк в статусы ─────────────────────────────────────

def _pool_with(conn):
    pool = MagicMock()
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


@pytest.mark.asyncio
async def test_facts_unknown_only_on_read_failure():
    """Сбой каждого источника даёт unknown этому факту, остальные живут (Т3)."""
    from db.queries import onboarding_facts as mod
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))
    with patch.object(mod, "get_persona_pool", new_callable=AsyncMock,
                      return_value=_pool_with(conn)), \
         patch.object(mod, "get_learning_pool", new_callable=AsyncMock,
                      return_value=_pool_with(conn)):
        facts = await mod.collect_onboarding_facts(ACCOUNT)
    assert facts["telegram_linked"]["status"] == "unknown"
    assert facts["guide_issued"]["status"] == "unknown"
    # маппинг Ory->Telegram упал => траектория непроверяема, тоже unknown
    assert facts["trajectory_confirmed"]["status"] == "unknown"
    assert facts["meeting_held"]["status"] == "unknown"


@pytest.mark.asyncio
async def test_single_source_failure_isolated():
    """verify-01 Medium: падает ТОЛЬКО learning — telegram/trajectory живут (Т3)."""
    from db.queries import onboarding_facts as mod
    persona_conn = MagicMock()
    persona_conn.fetchrow = AsyncMock(
        return_value={"telegram_id": 317106357, "created_at": _DT})
    learning_conn = MagicMock()
    learning_conn.fetchrow = AsyncMock(side_effect=RuntimeError("learning down"))
    dev_conn = MagicMock()
    dev_conn.fetchval = AsyncMock(return_value=_DT)
    with patch.object(mod, "get_persona_pool", new_callable=AsyncMock,
                      return_value=_pool_with(persona_conn)), \
         patch.object(mod, "get_learning_pool", new_callable=AsyncMock,
                      return_value=_pool_with(learning_conn)), \
         patch.object(mod, "get_pool", new_callable=AsyncMock,
                      return_value=_pool_with(dev_conn)):
        facts = await mod.collect_onboarding_facts(ACCOUNT)
    assert facts["guide_issued"]["status"] == "unknown"
    assert facts["telegram_linked"]["status"] == "confirmed"
    assert facts["trajectory_confirmed"]["status"] == "confirmed"


@pytest.mark.asyncio
async def test_no_link_row_is_pending_not_unknown():
    """Отсутствие привязки — наблюдаемое «не сделано», не сбой (консенсус, ход 1)."""
    from db.queries import onboarding_facts as mod
    persona_conn = MagicMock()
    persona_conn.fetchrow = AsyncMock(return_value=None)
    learning_conn = MagicMock()
    learning_conn.fetchrow = AsyncMock(return_value={"done_at": None, "total": 0})
    with patch.object(mod, "get_persona_pool", new_callable=AsyncMock,
                      return_value=_pool_with(persona_conn)), \
         patch.object(mod, "get_learning_pool", new_callable=AsyncMock,
                      return_value=_pool_with(learning_conn)):
        facts = await mod.collect_onboarding_facts(ACCOUNT)
    assert facts["telegram_linked"]["status"] == "pending_evaluation"
    assert facts["guide_issued"]["status"] == "pending_evaluation"
    # без привязки X3 заведомо не проходился — pending, не unknown
    assert facts["trajectory_confirmed"]["status"] == "pending_evaluation"


@pytest.mark.asyncio
async def test_done_queue_row_confirms_guide_issued():
    """status='done' в очереди = успешный render_pilot() = гайд выдан (С1)."""
    from db.queries import onboarding_facts as mod
    persona_conn = MagicMock()
    persona_conn.fetchrow = AsyncMock(
        return_value={"telegram_id": 317106357, "created_at": _DT})
    learning_conn = MagicMock()
    learning_conn.fetchrow = AsyncMock(return_value={"done_at": _DT, "total": 2})
    dev_conn = MagicMock()
    dev_conn.fetchval = AsyncMock(return_value=_DT)
    with patch.object(mod, "get_persona_pool", new_callable=AsyncMock,
                      return_value=_pool_with(persona_conn)), \
         patch.object(mod, "get_learning_pool", new_callable=AsyncMock,
                      return_value=_pool_with(learning_conn)), \
         patch.object(mod, "get_pool", new_callable=AsyncMock,
                      return_value=_pool_with(dev_conn)):
        facts = await mod.collect_onboarding_facts(ACCOUNT)
    assert facts["telegram_linked"]["status"] == "confirmed"
    assert facts["guide_issued"]["status"] == "confirmed"
    assert facts["guide_issued"]["occurred_at"] == _DT.isoformat()
    assert facts["trajectory_confirmed"]["status"] == "confirmed"
    assert facts["meeting_held"]["status"] == "unknown"
