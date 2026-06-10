"""
Миграция 028: таблица user_milestone_offers — история офферов Дорожника пользователям.

Контекст (WP-406 Ф6, peer-session 2026-06-10-11-wp406-f6-artifacts):
  Дорожник (DP.SC.173) фиксирует каждое предложение «пользы» (P1-P9) и ответ
  пользователя. Partial unique index не даёт одновременно существовать двум
  активным офферам одной пользы — гарантия на уровне схемы, не прикладного кода.

# see DP.SC.173, DP.ROLE.073, migration 027

Запуск:
    python -m db.migrations.028_user_milestone_offers
"""

import asyncio
import asyncpg
import os


UP = """
CREATE TABLE IF NOT EXISTS user_milestone_offers (
    id              BIGSERIAL       PRIMARY KEY,
    user_id         BIGINT          NOT NULL REFERENCES users(telegram_id),
    milestone_p     SMALLINT        NOT NULL CHECK (milestone_p BETWEEN 1 AND 9),
    offered_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    response        TEXT            CHECK (response IN ('accepted', 'declined', 'ignored')),
    responded_at    TIMESTAMPTZ,
    decline_reason  TEXT
);

-- Не более одного активного оффера на (user_id, milestone_p).
-- "Активный" = response IS NULL (ждёт ответа) или response = 'ignored' (истёк без ответа).
-- Позволяет создать новый оффер после accepted/declined (старая строка уже не NULL/ignored).
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_offer_per_milestone
    ON user_milestone_offers (user_id, milestone_p)
    WHERE response IS NULL OR response = 'ignored';

CREATE INDEX IF NOT EXISTS idx_offers_user_id
    ON user_milestone_offers (user_id);

COMMENT ON TABLE user_milestone_offers IS
    'История офферов Дорожника (DP.SC.173). '
    'Partial unique index предотвращает дублирующиеся активные офферы. '
    'Source: WP-406 Ф6, peer-session 2026-06-10-11.';

COMMENT ON COLUMN user_milestone_offers.milestone_p IS 'Номер пользы (1-9), см. DP.VM.001';
COMMENT ON COLUMN user_milestone_offers.response IS
    'accepted — принял, declined — отказался явно, ignored — не ответил (истёк таймаут)';
COMMENT ON COLUMN user_milestone_offers.decline_reason IS
    'Причина отказа (свободный текст из диалога с пользователем)';
"""

DOWN = """
DROP INDEX IF EXISTS idx_one_active_offer_per_milestone;
DROP INDEX IF EXISTS idx_offers_user_id;
DROP TABLE IF EXISTS user_milestone_offers;
"""


async def run(dsn: str | None = None) -> None:
    dsn = dsn or os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(UP)
        print("028_user_milestone_offers: OK")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run())
