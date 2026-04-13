"""
Миграция 013: Row-Level Security для изоляции данных пользователей — WP-212 B4.23 пр.3

Таблицы:
  1. development.user_state  — PK = user_id UUID (FK users.id)
     Политика: user_id = (SELECT id FROM public.users WHERE ory_id = current_user_id())

  2. public.github_connections      — PK = chat_id BIGINT
  3. public.google_calendar_connections — PK = chat_id BIGINT
  4. public.dt_tokens               — PK = chat_id BIGINT
  5. public.ory_tokens              — PK = chat_id BIGINT
     Политика для всех chat_id таблиц:
     chat_id = (SELECT telegram_id FROM public.users WHERE ory_id = current_user_id())

Механизм (Вариант A, Supabase-паттерн):
  Приложение устанавливает SET LOCAL app.user_id = $ory_id внутри транзакции.
  current_user_id() = current_setting('app.user_id', true)::TEXT
  Функция создана в knowledge-mcp (migration 006). Здесь создаём аналог в aist_bot БД.

NULL-guard:
  Если current_user_id() IS NULL → никто не видит личные строки (fail safe, не fail open).
  Без SET LOCAL → RLS блокирует все личные данные.

T0-пользователи (без ory_id):
  users.ory_id IS NULL → subquery вернёт NULL → chat_id = NULL → строка не видна.
  Это корректное поведение: T0 ещё не прошёл OAuth в Ory.

Применять: psql (unpooled Neon endpoint) -f migrations/013_rls_user_isolation.py
  или: python -m db.migrations.013_rls_user_isolation

Порядок отмены (если нужно откатить):
  ALTER TABLE development.user_state DISABLE ROW LEVEL SECURITY;
  DROP POLICY IF EXISTS user_state_isolation ON development.user_state;
  -- Аналогично для остальных таблиц.
"""

import asyncio
import asyncpg
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import DATABASE_URL


async def migrate():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # -----------------------------------------------------------------------
        # Вспомогательная функция current_user_id() в aist_bot БД
        # Аналог той, что создана в knowledge-mcp (migration 006).
        # -----------------------------------------------------------------------
        await conn.execute("""
            CREATE OR REPLACE FUNCTION current_user_id() RETURNS TEXT AS $$
              SELECT current_setting('app.user_id', true)::TEXT;
            $$ LANGUAGE SQL STABLE;
        """)
        print("✅ Function current_user_id() created/updated")

        # -----------------------------------------------------------------------
        # 1. development.user_state
        #    PK = user_id UUID (FK public.users.id)
        #    Политика: users.id связан с ory_id → ищем через JOIN
        # -----------------------------------------------------------------------
        await conn.execute("""
            ALTER TABLE development.user_state ENABLE ROW LEVEL SECURITY;
        """)
        await conn.execute("""
            ALTER TABLE development.user_state FORCE ROW LEVEL SECURITY;
        """)
        # Удаляем старую политику если есть (idempotent)
        await conn.execute("""
            DROP POLICY IF EXISTS user_state_isolation ON development.user_state;
        """)
        await conn.execute("""
            CREATE POLICY user_state_isolation ON development.user_state
              FOR ALL
              USING (
                user_id = (
                  SELECT id FROM public.users
                  WHERE ory_id::TEXT = current_user_id()
                  LIMIT 1
                )
              );
        """)
        print("✅ RLS enabled: development.user_state")

        # -----------------------------------------------------------------------
        # 2. public.github_connections
        #    PK = chat_id BIGINT (= telegram_id)
        #    Политика: ищем telegram_id пользователя по его ory_id
        # -----------------------------------------------------------------------
        await conn.execute("""
            ALTER TABLE public.github_connections ENABLE ROW LEVEL SECURITY;
        """)
        await conn.execute("""
            ALTER TABLE public.github_connections FORCE ROW LEVEL SECURITY;
        """)
        await conn.execute("""
            DROP POLICY IF EXISTS github_connections_isolation ON public.github_connections;
        """)
        await conn.execute("""
            CREATE POLICY github_connections_isolation ON public.github_connections
              FOR ALL
              USING (
                chat_id = (
                  SELECT telegram_id FROM public.users
                  WHERE ory_id::TEXT = current_user_id()
                  LIMIT 1
                )
              );
        """)
        print("✅ RLS enabled: public.github_connections")

        # -----------------------------------------------------------------------
        # 3. public.google_calendar_connections
        #    PK = chat_id BIGINT
        # -----------------------------------------------------------------------
        await conn.execute("""
            ALTER TABLE public.google_calendar_connections ENABLE ROW LEVEL SECURITY;
        """)
        await conn.execute("""
            ALTER TABLE public.google_calendar_connections FORCE ROW LEVEL SECURITY;
        """)
        await conn.execute("""
            DROP POLICY IF EXISTS google_calendar_connections_isolation ON public.google_calendar_connections;
        """)
        await conn.execute("""
            CREATE POLICY google_calendar_connections_isolation ON public.google_calendar_connections
              FOR ALL
              USING (
                chat_id = (
                  SELECT telegram_id FROM public.users
                  WHERE ory_id::TEXT = current_user_id()
                  LIMIT 1
                )
              );
        """)
        print("✅ RLS enabled: public.google_calendar_connections")

        # -----------------------------------------------------------------------
        # 4. public.dt_tokens
        #    PK = chat_id BIGINT
        # -----------------------------------------------------------------------
        await conn.execute("""
            ALTER TABLE public.dt_tokens ENABLE ROW LEVEL SECURITY;
        """)
        await conn.execute("""
            ALTER TABLE public.dt_tokens FORCE ROW LEVEL SECURITY;
        """)
        await conn.execute("""
            DROP POLICY IF EXISTS dt_tokens_isolation ON public.dt_tokens;
        """)
        await conn.execute("""
            CREATE POLICY dt_tokens_isolation ON public.dt_tokens
              FOR ALL
              USING (
                chat_id = (
                  SELECT telegram_id FROM public.users
                  WHERE ory_id::TEXT = current_user_id()
                  LIMIT 1
                )
              );
        """)
        print("✅ RLS enabled: public.dt_tokens")

        # -----------------------------------------------------------------------
        # 5. public.ory_tokens
        #    PK = chat_id BIGINT
        # -----------------------------------------------------------------------
        await conn.execute("""
            ALTER TABLE public.ory_tokens ENABLE ROW LEVEL SECURITY;
        """)
        await conn.execute("""
            ALTER TABLE public.ory_tokens FORCE ROW LEVEL SECURITY;
        """)
        await conn.execute("""
            DROP POLICY IF EXISTS ory_tokens_isolation ON public.ory_tokens;
        """)
        await conn.execute("""
            CREATE POLICY ory_tokens_isolation ON public.ory_tokens
              FOR ALL
              USING (
                chat_id = (
                  SELECT telegram_id FROM public.users
                  WHERE ory_id::TEXT = current_user_id()
                  LIMIT 1
                )
              );
        """)
        print("✅ RLS enabled: public.ory_tokens")

        print("\n🔒 Migration 013 complete. RLS enabled on 5 tables.")
        print("   To verify: SELECT tablename, rowsecurity FROM pg_tables WHERE tablename IN")
        print("   ('user_state','github_connections','google_calendar_connections','dt_tokens','ory_tokens');")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
