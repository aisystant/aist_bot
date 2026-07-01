#!/usr/bin/env python3
"""
Diagnose and manually link an Aisystant account to a Telegram user.

Usage:
    DATABASE_URL=<bot_neon_url> PERSONA_URL=<persona_neon_url> \
    AISYSTANT_BASE_URL=https://aisystant.system-school.ru \
    AISYSTANT_TECH_PASSWORD=<secret> \
        python3 scripts/admin_link_user.py \
            --chat-id 317106357 \
            --email alexandreteterin@gmail.com \
            [--dry-run]
"""

import argparse
import asyncio
import hashlib
import os
import sys
from datetime import datetime

import asyncpg
import aiohttp


async def find_aisystant_id_in_persona(pconn, email: str) -> dict | None:
    """Find aisystant identity by email in persona.ory_identity."""
    row = await pconn.fetchrow(
        """SELECT
               account_id,
               telegram_id,
               traits->>'email' AS email,
               COALESCE(traits->>'aisystant_suser_id', traits->>'aisystant_id') AS aisystant_id
           FROM public.ory_identity
           WHERE traits->>'email' = $1
           LIMIT 1""",
        email,
    )
    return dict(row) if row else None


async def get_user_state(bot_conn, chat_id: int) -> dict | None:
    """Read current user state from public.users."""
    row = await bot_conn.fetchrow(
        """SELECT telegram_id, username, aisystant_id, dt_user_id, ory_id,
                  aisystant_linked_at, created_at
           FROM public.users
           WHERE telegram_id = $1""",
        chat_id,
    )
    return dict(row) if row else None


async def check_persona_by_telegram(pconn, chat_id: int) -> dict | None:
    """Check persona.ory_identity by telegram_id."""
    row = await pconn.fetchrow(
        """SELECT
               account_id,
               telegram_id,
               traits->>'email' AS email,
               COALESCE(traits->>'aisystant_suser_id', traits->>'aisystant_id') AS aisystant_id
           FROM public.ory_identity
           WHERE telegram_id = $1
           LIMIT 1""",
        chat_id,
    )
    return dict(row) if row else None


async def check_subscription(base_url: str, tech_password: str, aisystant_id: str) -> dict:
    """Check subscription status via Aisystant API."""
    auth = aiohttp.BasicAuth("tech@aisystant.com", tech_password)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        url = f"{base_url}/api/subscriptions/active-subscription"
        async with session.get(url, auth=auth, params={"user-id": aisystant_id}) as resp:
            if resp.status == 200:
                data = await resp.json()
                return {"active": bool(data), "data": data, "status": resp.status}
            text = await resp.text()
            return {"active": False, "data": text[:200], "status": resp.status}


async def do_link(bot_conn, pconn, chat_id: int, aisystant_id: str, dry_run: bool) -> None:
    """Perform the link: update public.users.aisystant_id + persona.telegram_id."""
    print(f"\n{'[DRY-RUN] ' if dry_run else ''}Linking chat_id={chat_id} ↔ aisystant_id={aisystant_id}")

    if not dry_run:
        result = await bot_conn.execute(
            """UPDATE public.users
               SET aisystant_id = $2, aisystant_linked_at = NOW(), updated_at = NOW()
               WHERE telegram_id = $1""",
            chat_id, aisystant_id,
        )
        print(f"  public.users UPDATE: {result}")

        # Set telegram_id in persona.ory_identity
        persona_result = await pconn.execute(
            """WITH target AS (
                   SELECT account_id FROM public.ory_identity
                   WHERE (traits->>'aisystant_suser_id' = $2 OR traits->>'aisystant_id' = $2)
                     AND (telegram_id IS NULL OR telegram_id <> $1)
                   ORDER BY (traits->>'aisystant_suser_id' = $2) DESC
                   LIMIT 1
               )
               UPDATE public.ory_identity oi
               SET telegram_id = $1
               FROM target
               WHERE oi.account_id = target.account_id""",
            chat_id, str(aisystant_id),
        )
        print(f"  persona.ory_identity UPDATE telegram_id: {persona_result}")

        # Sync dt_user_id → Ory UUID
        ory_uuid = await pconn.fetchval(
            """SELECT account_id FROM public.ory_identity
               WHERE (traits->>'aisystant_suser_id' = $1 OR traits->>'aisystant_id' = $1)
               ORDER BY (traits->>'aisystant_suser_id' = $1) DESC
               LIMIT 1""",
            str(aisystant_id),
        )
        if ory_uuid:
            dt_result = await bot_conn.execute(
                "UPDATE public.users SET dt_user_id = $2, updated_at = NOW() WHERE telegram_id = $1",
                chat_id, str(ory_uuid),
            )
            print(f"  public.users.dt_user_id sync: {dt_result} (ory_uuid={ory_uuid})")
        else:
            print("  WARNING: No Ory UUID found — dt_user_id not synced")
    else:
        print("  [dry-run] Would UPDATE public.users SET aisystant_id + persona.ory_identity SET telegram_id")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose and link Aisystant account to Telegram user")
    parser.add_argument("--chat-id", type=int, required=True, help="Telegram chat_id")
    parser.add_argument("--email", required=True, help="Aisystant email to link")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen, don't write")
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    persona_url = os.environ.get("PERSONA_URL")
    aisystant_base = os.environ.get("AISYSTANT_BASE_URL", "https://aisystant.system-school.ru")
    aisystant_pass = os.environ.get("AISYSTANT_TECH_PASSWORD", "")

    if not db_url or not persona_url:
        print("ERROR: DATABASE_URL and PERSONA_URL required", file=sys.stderr)
        sys.exit(1)

    bot_pool = await asyncpg.create_pool(db_url, statement_cache_size=0)
    persona_pool = await asyncpg.create_pool(persona_url, statement_cache_size=0)

    async with bot_pool.acquire() as bot_conn, persona_pool.acquire() as pconn:

        # ── 1. Current bot DB state ──────────────────────────────────────────
        print(f"\n=== Bot DB: public.users for chat_id={args.chat_id} ===")
        user = await get_user_state(bot_conn, args.chat_id)
        if not user:
            print(f"  NOT FOUND in public.users — has the user ever started the bot?")
            await bot_pool.close()
            await persona_pool.close()
            sys.exit(1)
        print(f"  username      : {user.get('username')}")
        print(f"  aisystant_id  : {user.get('aisystant_id')} {'✅' if user.get('aisystant_id') else '❌ NULL'}")
        print(f"  dt_user_id    : {user.get('dt_user_id')} {'✅' if user.get('dt_user_id') else '❌ NULL'}")
        print(f"  ory_id        : {user.get('ory_id')}")
        print(f"  linked_at     : {user.get('aisystant_linked_at')}")

        # ── 2. Persona: check by telegram_id (already linked via Ory OAuth?) ─
        print(f"\n=== Persona: ory_identity for telegram_id={args.chat_id} ===")
        persona_by_tg = await check_persona_by_telegram(pconn, args.chat_id)
        if persona_by_tg:
            print(f"  account_id    : {persona_by_tg.get('account_id')}")
            print(f"  email         : {persona_by_tg.get('email')}")
            print(f"  aisystant_id  : {persona_by_tg.get('aisystant_id')} {'✅' if persona_by_tg.get('aisystant_id') else '❌ NULL in traits'}")
        else:
            print("  NOT FOUND — telegram_id not in persona.ory_identity")

        # ── 3. Persona: look up by email ─────────────────────────────────────
        print(f"\n=== Persona: ory_identity for email={args.email} ===")
        persona_by_email = await find_aisystant_id_in_persona(pconn, args.email)
        if not persona_by_email:
            print(f"  NOT FOUND — email not in persona.ory_identity")
            print("  → User may not have registered on Aisystant website yet")
            await bot_pool.close()
            await persona_pool.close()
            sys.exit(1)
        print(f"  account_id    : {persona_by_email.get('account_id')}")
        print(f"  telegram_id   : {persona_by_email.get('telegram_id')} {'✅ already linked' if persona_by_email.get('telegram_id') else '❌ NULL'}")
        print(f"  aisystant_id  : {persona_by_email.get('aisystant_id')} {'✅' if persona_by_email.get('aisystant_id') else '❌ NULL in traits'}")

        aisystant_id = persona_by_email.get('aisystant_id')
        if not aisystant_id:
            print("\n  ERROR: No aisystant_id in traits for this email")
            print("  → Aisystant account may not be fully set up")
            await bot_pool.close()
            await persona_pool.close()
            sys.exit(1)

        # ── 4. Subscription check ─────────────────────────────────────────────
        print(f"\n=== Subscription check for aisystant_id={aisystant_id} ===")
        if aisystant_pass:
            sub = await check_subscription(aisystant_base, aisystant_pass, aisystant_id)
            print(f"  HTTP {sub['status']}: active={sub['active']}")
            if sub.get('data') and sub['active']:
                data = sub['data']
                if isinstance(data, dict):
                    print(f"  tariff  : {data.get('tariff', {}).get('name', '?')}")
                    print(f"  valid_to: {data.get('endDate', '?')}")
        else:
            print("  SKIPPED — AISYSTANT_TECH_PASSWORD not set")

        # ── 5. Decision ────────────────────────────────────────────────────────
        already_linked = (user.get('aisystant_id') == aisystant_id or
                          (persona_by_tg and persona_by_tg.get('aisystant_id') == aisystant_id))

        print(f"\n=== Decision ===")
        if already_linked:
            print(f"  ✅ Already fully linked — no action needed")
        else:
            print(f"  ❌ Link missing — will link chat_id={args.chat_id} ↔ aisystant_id={aisystant_id}")
            await do_link(bot_conn, pconn, args.chat_id, aisystant_id, args.dry_run)
            if not args.dry_run:
                print("\n  ✅ Done. User should get upgraded tier on next bot interaction.")
                print("     Note: stage/derived will appear after profiler runs at 04:30 MSK")
                print("     and after user accumulates learning activity in Aisystant.")

    await bot_pool.close()
    await persona_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
