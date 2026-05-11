"""Migration 014: GitHub App installation_id для github_connections (WP-301 Ф7).

Добавляет колонки для отслеживания установки GitHub App "Aisystant Personal Guide"
на репо пилота (`<user>/personal-guide`).

Контракт:
- `app_installation_id` BIGINT — уникальный installation_id от GitHub (NULL если App ещё не установлен)
- `app_repo_full_name` TEXT — full_name репо (например, "TserenTserenov/personal-guide")
- `app_installed_at` TIMESTAMP — когда установлен
- `app_suspended` BOOLEAN — установка suspended (uninstall flow)

Webhook handler ищет user по installation_id (быстрее, чем по pusher.name).
OAuth flow и App install flow живут параллельно — github_username из OAuth,
installation_id из App callback.

Запуск:
    python -m db.migrations.014_github_app_installations
"""

import asyncio
import os
import asyncpg


async def migrate():
    """Применить миграцию к secrets DB (github_connections живёт там)."""
    secrets_url = os.getenv("SECRETS_URL")
    if not secrets_url:
        raise RuntimeError("SECRETS_URL не установлен")

    conn = await asyncpg.connect(secrets_url)
    try:
        print("🔧 Migration 014: adding GitHub App columns to github_connections")

        await conn.execute("""
            ALTER TABLE public.github_connections
            ADD COLUMN IF NOT EXISTS app_installation_id BIGINT,
            ADD COLUMN IF NOT EXISTS app_repo_full_name TEXT,
            ADD COLUMN IF NOT EXISTS app_installed_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS app_suspended BOOLEAN DEFAULT FALSE;
        """)
        print("✅ ALTER TABLE done")

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_github_connections_app_installation_id
            ON public.github_connections (app_installation_id)
            WHERE app_installation_id IS NOT NULL;
        """)
        print("✅ Index created (sparse on installation_id)")

        await conn.execute("""
            COMMENT ON COLUMN public.github_connections.app_installation_id IS
            'GitHub App installation_id (Aisystant Personal Guide). NULL = App не установлен. WP-301 Ф7.';
        """)
        await conn.execute("""
            COMMENT ON COLUMN public.github_connections.app_repo_full_name IS
            'Полное имя репо, на которое установлен App, e.g. "user/personal-guide". WP-301 Ф7.';
        """)

        print("\n🎉 Migration 014 complete.")
        print("   Verify: SELECT chat_id, github_username, app_installation_id, app_repo_full_name")
        print("           FROM public.github_connections WHERE app_installation_id IS NOT NULL;")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
