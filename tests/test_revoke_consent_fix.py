"""Test revoke_consent fix (WP-457 Ф10): обновляет обе таблицы согласия."""
import pytest
from uuid import uuid4
from db.queries.consent import revoke_consent, set_consent_grant, set_consent


@pytest.mark.asyncio
async def test_revoke_consent_updates_both_tables():
    """revoke_consent отзывает consent_grant и оставляет в tracking_consent строку-отказ (WP-5 Ф74)."""
    account_id = str(uuid4())

    # Setup: добавить согласие в обе таблицы
    await set_consent(account_id, opt_in=True)  # tracking_consent (legacy)
    await set_consent_grant(account_id, "data_analysis", granted=True)  # consent_grant (новая)

    # Act: отозвать согласие
    result = await revoke_consent(account_id)

    # Assert: revoke прошёл (вернул True)
    assert result is True

    # Verify: legacy-строка осталась как отказ, а не удалена (след отказа сохраняется)
    from db.connection import get_consent_pool
    pool = await get_consent_pool()
    async with pool.acquire() as conn:
        legacy = await conn.fetchrow(
            "SELECT opt_in, scope FROM learning.tracking_consent WHERE account_id = $1::uuid",
            account_id,
        )
        assert legacy is not None, "строка-отказ должна остаться"
        assert legacy["opt_in"] is False
        assert list(legacy["scope"]) == []

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


@pytest.mark.asyncio
async def test_revoke_without_prior_consent_leaves_refusal_and_returns_false():
    """Нет активного согласия: отзыв возвращает False, но строка-отказ появляется,
    чтобы подписка не стала основанием учёта (WP-5 Ф74)."""
    account_id = str(uuid4())

    result = await revoke_consent(account_id)

    assert result is False
    from db.connection import get_consent_pool
    pool = await get_consent_pool()
    async with pool.acquire() as conn:
        opt_in = await conn.fetchval(
            "SELECT opt_in FROM learning.tracking_consent WHERE account_id = $1::uuid",
            account_id,
        )
    assert opt_in is False


@pytest.mark.asyncio
async def test_revoke_then_get_consent_reads_as_refused():
    """Читатели (get_consent) видят отказ: opt_in=false, как и до правки без строки."""
    from db.queries.consent import get_consent
    account_id = str(uuid4())
    await set_consent(account_id, opt_in=True)

    assert await revoke_consent(account_id) is True
    consent = await get_consent(account_id)
    assert consent is not None and consent["opt_in"] is False
