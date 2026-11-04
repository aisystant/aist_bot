"""Connection identity check of the domain pools (WP-544 F16).

Each of the ten domain pools and the single subscription connection of the tier
detector verify on every new physical connection that they reached the expected
Neon database (and, when EXPECTED_DB_ROLE_<DOMAIN> is set, the expected role).
The settings fall back to DATABASE_URL when a domain URL is unset; this check is
what turns that silent substitution into a refusal. No network or database here:
asyncpg is replaced by mocks.
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

from core import tier_detector  # noqa: E402
from db import connection  # noqa: E402

FAKE_DSN = "postgresql://bot_user:s3cret-pass@ep-test.neon.tech/wrongdb"

POOL_GETTERS = {
    "COMMUNITY": ("get_community_pool", "_community_pool", "COMMUNITY_URL"),
    "HEALTH": ("get_health_pool", "_health_pool", "HEALTH_URL"),
    "INDICATORS": ("get_indicators_pool", "_indicators_pool", "INDICATORS_URL"),
    "JOURNAL": ("get_journal_pool", "_journal_pool", "JOURNAL_URL"),
    "LEAD": ("get_lead_pool", "_lead_pool", "LEAD_URL"),
    "PERSONA": ("get_persona_pool", "_persona_pool", "PERSONA_URL"),
    "PUBLICATION": ("get_publication_pool", "_publication_pool", "PUBLICATION_URL"),
    "REFERENCE": ("get_reference_pool", "_reference_pool", "REFERENCE_URL"),
    "SECRETS": ("get_secrets_pool", "_secrets_pool", "SECRETS_URL"),
    "SUBSCRIPTION": ("get_subscription_pool", "_subscription_pool", "SUBSCRIPTION_URL"),
}


def _conn(database, user="aist_pilot_bot_main", session_user=None):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "database": database,
            "current_user": user,
            "session_user": session_user or user,
        }
    )
    conn.fetchval = AsyncMock(return_value=True)
    conn.close = AsyncMock()
    return conn


@pytest.fixture(autouse=True)
def _clean_role_env(monkeypatch):
    for domain in connection.DOMAIN_DATABASES:
        monkeypatch.delenv(f"EXPECTED_DB_ROLE_{domain}", raising=False)
    monkeypatch.delenv("DB_APPLICATION_NAME", raising=False)
    connection._unverified_role_warned.clear()


def test_every_domain_has_a_pool_getter():
    assert set(connection.DOMAIN_DATABASES) == set(POOL_GETTERS)


@pytest.mark.asyncio
async def test_wrong_database_is_refused():
    with pytest.raises(RuntimeError) as exc:
        await connection.verify_connection_identity(_conn("neondb"), "SECRETS")

    assert "'secrets'" in str(exc.value)
    assert "'neondb'" in str(exc.value)


@pytest.mark.asyncio
async def test_right_database_and_role_passes(monkeypatch):
    monkeypatch.setenv("EXPECTED_DB_ROLE_SECRETS", "aist_pilot_bot_secrets")
    conn = _conn("secrets", user="aist_pilot_bot_secrets")

    with patch.object(connection, "logger") as logger:
        result = await connection.verify_connection_identity(conn, "SECRETS")

    assert result is None
    conn.fetchrow.assert_awaited_once()
    logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_role_mismatch_is_refused_when_expected_role_set(monkeypatch):
    monkeypatch.setenv("EXPECTED_DB_ROLE_INDICATORS", "aist_pilot_bot_main")

    with pytest.raises(RuntimeError) as exc:
        await connection.verify_connection_identity(
            _conn("indicators", user="neondb_owner"), "INDICATORS"
        )

    assert "'aist_pilot_bot_main'" in str(exc.value)
    assert "neondb_owner" in str(exc.value)


@pytest.mark.asyncio
async def test_session_user_mismatch_is_refused(monkeypatch):
    """SET ROLE changes current_user only; session_user must match as well."""
    monkeypatch.setenv("EXPECTED_DB_ROLE_LEAD", "aist_me_bot_main")
    conn = _conn("lead", user="aist_me_bot_main", session_user="neondb_owner")

    with pytest.raises(RuntimeError, match="session_user='neondb_owner'"):
        await connection.verify_connection_identity(conn, "LEAD")


@pytest.mark.asyncio
async def test_missing_expected_role_only_warns_once():
    with patch.object(connection, "logger") as logger:
        await connection.verify_connection_identity(
            _conn("journal", user="anyone"), "JOURNAL"
        )
        await connection.verify_connection_identity(
            _conn("journal", user="anyone"), "JOURNAL"
        )

    logger.warning.assert_called_once()
    args = logger.warning.call_args.args
    assert "EXPECTED_DB_ROLE_JOURNAL" in args
    assert "anyone" not in " ".join(str(a) for a in args)


@pytest.mark.parametrize("domain", sorted(POOL_GETTERS))
@pytest.mark.asyncio
async def test_pool_init_refuses_wrong_database_without_dsn(domain, monkeypatch):
    getter_name, global_name, url_name = POOL_GETTERS[domain]
    monkeypatch.setattr(connection, global_name, None)
    monkeypatch.setattr(connection, url_name, FAKE_DSN)
    monkeypatch.setenv("DB_APPLICATION_NAME", "aist_pilot_bot")
    create_pool = AsyncMock(return_value=MagicMock())

    with patch.object(connection.asyncpg, "create_pool", create_pool):
        await getattr(connection, getter_name)()

    args, kwargs = create_pool.call_args
    assert args == (FAKE_DSN,)
    assert kwargs["server_settings"] == {"application_name": "aist_pilot_bot"}
    with pytest.raises(RuntimeError) as exc:
        await kwargs["init"](_conn("wrongdb"))
    message = str(exc.value)
    assert repr(connection.DOMAIN_DATABASES[domain]) in message
    assert FAKE_DSN not in message
    assert "s3cret-pass" not in message
    assert "ep-test.neon.tech" not in message


@pytest.mark.asyncio
async def test_pool_init_accepts_its_own_database(monkeypatch):
    monkeypatch.setattr(connection, "_reference_pool", None)
    create_pool = AsyncMock(return_value=MagicMock())

    with patch.object(connection.asyncpg, "create_pool", create_pool):
        await connection.get_reference_pool()

    kwargs = create_pool.call_args.kwargs
    assert kwargs["statement_cache_size"] == 0
    assert kwargs["server_settings"] == {"application_name": "aist_bot"}
    conn = _conn("reference")
    await kwargs["init"](conn)
    conn.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
async def test_pre_existing_pool_options_kept(monkeypatch):
    monkeypatch.setattr(connection, "_secrets_pool", None)
    monkeypatch.setattr(connection, "_persona_pool", None)
    create_pool = AsyncMock(return_value=MagicMock())

    with patch.object(connection.asyncpg, "create_pool", create_pool):
        await connection.get_secrets_pool()
        await connection.get_persona_pool()

    secrets_kwargs, persona_kwargs = (c.kwargs for c in create_pool.call_args_list)
    assert secrets_kwargs["statement_cache_size"] == 0
    assert secrets_kwargs["max_size"] == 5
    assert persona_kwargs["statement_cache_size"] == 100
    assert persona_kwargs["min_size"] == 2
    assert persona_kwargs["max_inactive_connection_lifetime"] == 300


@pytest.mark.asyncio
async def test_subscription_connection_checks_identity_before_query(monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_DATABASE_URL", FAKE_DSN)
    monkeypatch.setenv("DB_APPLICATION_NAME", "aist_me_bot")
    conn = _conn("subscription")
    connect = AsyncMock(return_value=conn)

    with (
        patch.object(
            tier_detector,
            "_get_ory_id",
            AsyncMock(return_value="11111111-1111-1111-1111-111111111111"),
        ),
        patch("asyncpg.connect", connect),
    ):
        result = await tier_detector._check_contract_subscription(42)

    assert result is True
    assert connect.call_args.args == (FAKE_DSN,)
    assert connect.call_args.kwargs == {
        "server_settings": {"application_name": "aist_me_bot"}
    }
    conn.fetchrow.assert_awaited_once()
    conn.fetchval.assert_awaited_once()
    conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_subscription_connection_to_wrong_database_is_not_queried(monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_DATABASE_URL", FAKE_DSN)
    conn = _conn("neondb")

    with (
        patch.object(
            tier_detector,
            "_get_ory_id",
            AsyncMock(return_value="11111111-1111-1111-1111-111111111111"),
        ),
        patch("asyncpg.connect", AsyncMock(return_value=conn)),
        patch.object(tier_detector, "logger") as logger,
    ):
        result = await tier_detector._check_contract_subscription(42)

    assert result is False
    conn.fetchval.assert_not_awaited()
    conn.close.assert_awaited_once()
    logged = " ".join(str(a) for a in logger.warning.call_args.args)
    assert "'subscription'" in logged
    assert "s3cret-pass" not in logged
