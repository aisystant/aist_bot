"""
Миграция 027: таблица user_milestones — фиксация пережитых пользовательских польз P1-P9.

Контекст (WP-406 Ф6, peer-session 2026-06-10-11-wp406-f6-artifacts):
  Девять промежуточных польз от первого полезного ответа (P1) до
  собственной среды развития (P9). Flat-таблица: один ряд на пользователя,
  9 boolean-колонок. Факты фиксирует event-процессор по proxy-условиям DP.VM.001.
  Дорожник (DP.ROLE.073) читает эту таблицу, не пишет в неё напрямую.

# see DP.SC.173, DP.ROLE.073, DP.VM.001

Запуск:
    python -m db.migrations.027_user_milestones
"""

import asyncio
import asyncpg
import os


UP = """
CREATE TABLE IF NOT EXISTS user_milestones (
    user_id         BIGINT          PRIMARY KEY REFERENCES users(telegram_id),
    has_p1          BOOLEAN         NOT NULL DEFAULT FALSE,
    has_p2          BOOLEAN         NOT NULL DEFAULT FALSE,
    has_p3          BOOLEAN         NOT NULL DEFAULT FALSE,
    has_p4          BOOLEAN         NOT NULL DEFAULT FALSE,
    has_p5          BOOLEAN         NOT NULL DEFAULT FALSE,
    has_p6          BOOLEAN         NOT NULL DEFAULT FALSE,
    has_p7          BOOLEAN         NOT NULL DEFAULT FALSE,
    has_p8          BOOLEAN         NOT NULL DEFAULT FALSE,
    has_p9          BOOLEAN         NOT NULL DEFAULT FALSE,
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE user_milestones IS
    'Факты пережитых пользовательских польз P1-P9 (DP.VM.001). '
    'Пишет event-процессор, читает Дорожник (DP.SC.173). '
    'Source: WP-406 Ф6, peer-session 2026-06-10-11.';

COMMENT ON COLUMN user_milestones.has_p1 IS 'Первый полезный ответ от ИИ';
COMMENT ON COLUMN user_milestones.has_p2 IS 'Пробная тема — открыт урок бесплатного курса';
COMMENT ON COLUMN user_milestones.has_p3 IS 'Марафон на регулярной основе (>= 3 чек-ина)';
COMMENT ON COLUMN user_milestones.has_p4 IS 'Профиль в Aisystant заполнен';
COMMENT ON COLUMN user_milestones.has_p5 IS 'Первая маленькая победа (quiz или заметка)';
COMMENT ON COLUMN user_milestones.has_p6 IS 'Персональная траектория (bottleneck + stream)';
COMMENT ON COLUMN user_milestones.has_p7 IS 'Доступ к сообществу (клуб)';
COMMENT ON COLUMN user_milestones.has_p8 IS 'Цифровой двойник инициализирован';
COMMENT ON COLUMN user_milestones.has_p9 IS 'Своя среда развития (DS-strategy + 1 коммит)';
"""

DOWN = """
DROP TABLE IF EXISTS user_milestones;
"""


async def run(dsn: str | None = None) -> None:
    dsn = dsn or os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(UP)
        print("027_user_milestones: OK")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run())
