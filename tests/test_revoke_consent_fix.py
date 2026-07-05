"""Test revoke_consent fix (WP-457 Ф10): обновляет обе таблицы согласия."""
import pytest
from uuid import uuid4
from db.queries.consent import revoke_consent, set_consent_grant, set_consent


@pytest.mark.asyncio
async def test_revoke_consent_updates_both_tables():
    """revoke_consent обновляет consent_grant И удаляет tracking_consent."""
    account_id = str(uuid4())

    # Setup: добавить согласие в обе таблицы
    await set_consent(account_id, opt_in=True)  # tracking_consent (legacy)
    await set_consent_grant(account_id, "data_analysis", granted=True)  # consent_grant (новая)

    # Act: отозвать согласие
    result = await revoke_consent(account_id)

    # Assert: revoke прошёл (вернул True)
    assert result is True

    # Verify legacy таблица удалена
    from db.connection import get_consent_pool
    pool = await get_consent_pool()
    async with pool.acquire() as conn:
        legacy_row = await conn.fetchval(
            "SELECT opt_in FROM learning.tracking_consent WHERE account_id = $1::uuid",
            account_id,
        )
        assert legacy_row is None, "legacy tracking_consent должна быть удалена"

        # Verify новая таблица обновлена (не удалена, а обновлена)
        grant_rows = await conn.fetch(
            """SELECT account_id, scope, granted, revoked_at
               FROM learning.consent_grant
               WHERE account_id = $1::uuid""",
            account_id,
        )
        assert len(grant_rows) > 0, "consent_grant строки должны остаться (не удаляться)"
        for row in grant_rows:
            assert row["granted"] is False, "granted должен быть False"
            assert row["revoked_at"] is not None, "revoked_at должен быть установлен"
