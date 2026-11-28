"""WP-384: voice operation receipts in the bot's main database.

Apply as the database owner; --grant-to names the actual bot runtime roles.
No runtime role is promoted to owner, and no transcript/audio is stored.
"""

import argparse
import asyncio

from db.voice_operations_schema import ensure_voice_operations


async def migrate_if_needed(pool, runtime_roles=()) -> bool:
    async with pool.acquire() as conn, conn.transaction():
        return await ensure_voice_operations(conn, runtime_roles)


async def main():
    import asyncpg

    from config import DATABASE_URL

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grant-to", action="append", default=[])
    args = parser.parse_args()
    pool = await asyncpg.create_pool(DATABASE_URL)
    try:
        await migrate_if_needed(pool, args.grant_to)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
