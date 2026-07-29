"""
OAuth callback сервер.

Обрабатывает OAuth callbacks от Linear, Digital Twin, GitHub, Google Calendar, WakaTime.
Запускается параллельно с ботом на порту 8080.

Endpoints:
- GET /auth/linear/callback — OAuth callback от Linear
- GET /auth/twin/callback — OAuth callback от Digital Twin
- GET /auth/github/callback — OAuth callback от GitHub (+ dual write в user_integrations, WP-109)
- GET /auth/google-calendar/callback — OAuth callback от Google Calendar
- GET /auth/wakatime/callback — OAuth callback от WakaTime (WP-109 Activity Hub)
- GET /health — health check для Railway
"""

import asyncio
import json
import os
import re
import uuid
from typing import Optional
from aiohttp import web
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import get_logger, OAUTH_SERVER_PORT
from clients.linear_oauth import linear_oauth
from clients.digital_twin import digital_twin
from clients.github_oauth import github_oauth
from clients.google_calendar_oauth import google_calendar_oauth
from clients.wakatime_oauth import wakatime_oauth
from clients.ory_oauth import ory_oauth

logger = get_logger(__name__)

# Глобальная ссылка на бота для отправки уведомлений
_bot_instance = None


def set_bot_instance(bot):
    """Устанавливает инстанс бота для отправки уведомлений."""
    global _bot_instance
    _bot_instance = bot


async def health_handler(request: web.Request) -> web.Response:
    """Health check endpoint для Railway."""
    return web.Response(text="OK", status=200)


async def linear_callback_handler(request: web.Request) -> web.Response:
    """Обрабатывает OAuth callback от Linear.

    Linear редиректит сюда с параметрами:
    - code: authorization code
    - state: state для верификации
    """
    code = request.query.get("code")
    state = request.query.get("state")
    error = request.query.get("error")

    # Обработка ошибки
    if error:
        error_description = request.query.get("error_description", "Unknown error")
        logger.error(f"Linear OAuth error: {error} - {error_description}")
        return web.Response(
            text=f"""
            <html>
            <head><title>Ошибка авторизации</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Ошибка авторизации Linear</h1>
                <p>{error_description}</p>
                <p>Вернитесь в Telegram и попробуйте снова.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=400
        )

    # Проверяем наличие code и state
    if not code or not state:
        logger.warning(f"Missing code or state in callback")
        return web.Response(
            text="""
            <html>
            <head><title>Ошибка</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Неверный запрос</h1>
                <p>Отсутствуют необходимые параметры.</p>
                <p>Вернитесь в Telegram и попробуйте снова.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=400
        )

    # Валидируем state и получаем user_id
    telegram_user_id = await linear_oauth.validate_state(state)
    if not telegram_user_id:
        logger.warning(f"Invalid or expired state")
        return web.Response(
            text="""
            <html>
            <head><title>Сессия истекла</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Сессия авторизации истекла</h1>
                <p>Пожалуйста, вернитесь в Telegram и начните авторизацию заново.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=400
        )

    # Обмениваем code на токен
    tokens = await linear_oauth.exchange_code(code, telegram_user_id)
    if not tokens:
        logger.error(f"Failed to exchange code for user {telegram_user_id}")
        return web.Response(
            text="""
            <html>
            <head><title>Ошибка</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Ошибка получения токена</h1>
                <p>Не удалось завершить авторизацию.</p>
                <p>Вернитесь в Telegram и попробуйте снова.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=500
        )

    # Успех! Получаем информацию о пользователе Linear
    viewer = await linear_oauth.get_viewer(telegram_user_id)
    linear_name = viewer.get("name", "пользователь") if viewer else "пользователь"

    logger.info(f"User {telegram_user_id} successfully connected to Linear as {linear_name}")

    # Отправляем уведомление в Telegram с кнопками
    if _bot_instance:
        try:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои задачи", callback_data="linear_tasks")],
                [InlineKeyboardButton(text="🔌 Отключить Linear", callback_data="linear_disconnect")]
            ])

            await _bot_instance.send_message(
                chat_id=telegram_user_id,
                text=(
                    f"✅ *Linear подключён!*\n\n"
                    f"Вы авторизованы как: *{linear_name}*"
                ),
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Failed to send notification to user {telegram_user_id}: {e}")

    # Возвращаем красивую страницу успеха
    return web.Response(
        text=f"""
        <html>
        <head>
            <title>Linear подключён!</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    text-align: center;
                    padding: 50px;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    min-height: 100vh;
                    margin: 0;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }}
                .card {{
                    background: white;
                    border-radius: 16px;
                    padding: 40px;
                    box-shadow: 0 10px 40px rgba(0,0,0,0.2);
                    max-width: 400px;
                }}
                h1 {{
                    color: #5E6AD2;
                    margin-bottom: 16px;
                }}
                p {{
                    color: #666;
                    line-height: 1.6;
                }}
                .success-icon {{
                    font-size: 64px;
                    margin-bottom: 16px;
                }}
                .name {{
                    font-weight: bold;
                    color: #333;
                }}
            </style>
        </head>
        <body>
            <div class="card">
                <div class="success-icon">✅</div>
                <h1>Linear подключён!</h1>
                <p>Вы успешно авторизовались как <span class="name">{linear_name}</span>.</p>
                <p>Можете закрыть эту страницу и вернуться в Telegram.</p>
            </div>
        </body>
        </html>
        """,
        content_type="text/html",
        status=200
    )


async def twin_callback_handler(request: web.Request) -> web.Response:
    """Обрабатывает OAuth callback от Digital Twin MCP.

    Digital Twin MCP редиректит сюда с параметрами:
    - code: authorization code
    - state: state для верификации (содержит code_verifier)
    """
    code = request.query.get("code")
    state = request.query.get("state")
    error = request.query.get("error")

    if error:
        error_description = request.query.get("error_description", "Unknown error")
        logger.error(f"DT OAuth error: {error} - {error_description}")
        return web.Response(
            text=f"""
            <html>
            <head><title>Ошибка авторизации</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Ошибка авторизации Digital Twin</h1>
                <p>{error_description}</p>
                <p>Вернитесь в Telegram и попробуйте снова.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=400
        )

    if not code or not state:
        return web.Response(
            text="""
            <html>
            <head><title>Ошибка</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Неверный запрос</h1>
                <p>Отсутствуют необходимые параметры.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=400
        )

    telegram_user_id = digital_twin.validate_state(state)
    if not telegram_user_id:
        return web.Response(
            text="""
            <html>
            <head><title>Сессия истекла</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Сессия авторизации истекла</h1>
                <p>Вернитесь в Telegram и начните авторизацию заново (/twin).</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=400
        )

    tokens = await digital_twin.exchange_code(code, state)
    if not tokens:
        return web.Response(
            text="""
            <html>
            <head><title>Ошибка</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Ошибка получения токена</h1>
                <p>Не удалось завершить авторизацию Digital Twin.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=500
        )

    logger.info(f"User {telegram_user_id} connected to Digital Twin")

    # Persist DT connection in DB (for tier detection after redeploy)
    try:
        from db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                'UPDATE public.users SET dt_connected_at = NOW() WHERE telegram_id = $1',
                telegram_user_id,
            )
    except Exception as e:
        logger.warning(f"Failed to persist DT connection for {telegram_user_id}: {e}")

    # WP-268 Phase 2 dual-write: DT OAuth callback завершён
    # Высокоуровневое событие — фактически "пользователь подключил DT через OAuth UI"
    # (низкоуровневая привязка id) — блок ниже эмитит dt_linked.v1 отдельно,
    # если этот путь впервые синкает ory_id (IDCOL1 Ф2, 2026-07-23).
    # Audit fix (Phase 2): (1) PII — убран raw chat_id из payload, account_id =
    # ory_id через resolve_ory_id_from_chat; (2) external_id — стабильный (один на
    # OAuth flow для пользователя), без epoch_ns, чтобы retry не дублировал событие.
    try:
        from helpers.dual_write import (
            post_event as _post_event,
            resolve_ory_id_from_chat as _resolve_ory,
        )
        from datetime import datetime as _dt
        import asyncio as _asyncio
        import hashlib as _hashlib
        _now = _dt.utcnow()
        _ory = await _resolve_ory(telegram_user_id)
        # external_id стабильный: предпочтительно ory_id (один на flow), fallback
        # для T0 — sha1 от chat_id (без timestamp; идемпотентно для retry).
        if _ory:
            _ext_id = f"dt-oauth-completed-{_ory}"
        else:
            _chat_hash = _hashlib.sha256(str(telegram_user_id).encode()).hexdigest()[:12]
            _ext_id = f"dt-oauth-completed-anon-{_chat_hash}"
        _asyncio.create_task(_post_event(
            source="aist-bot",
            external_id=_ext_id,
            event_type="dt_oauth_completed",
            schema_version="v1",
            occurred_at=_now,
            account_id=_ory,  # ory UUID если привязан, иначе None
            payload={
                # PII-инвариант: telegram_id НЕ передаётся.
                "via": "dt_oauth_callback",
            },
        ))
    except Exception as _exc:
        logger.warning(f"[dual-write] dt_oauth_completed fire failed: {_exc}")

    # Автоматический перелив профиля бота → ЦД
    # NB: sync_profile требует Ory tokens (gateway_mcp). Если пользователь подключился
    # через DT OAuth (legacy), но не через Ory — sync пропускается (вернёт 0).
    # NB: _sync_dt_connected_users удалён (a7681a2). Если нужна синхронизация —
    # подключать через gateway_mcp.get_connected_user_ids() отдельно.
    try:
        from db.queries.users import get_intern
        intern = await get_intern(telegram_user_id)
        if intern:
            from clients.gateway_mcp import gateway_mcp
            synced = await gateway_mcp.sync_profile(telegram_user_id, intern)
            logger.info(f"DT initial sync for user {telegram_user_id}: {synced} fields")
    except Exception as e:
        logger.error(f"DT initial sync failed for user {telegram_user_id}: {e}")

    # Persist Ory UUID (extracted via DT write response, see digital_twin.py sync_profile)
    # into public.users.ory_id (WP-82; WP-7 IDCOL1 Stage 2, 2026-07-23). digital_twins.user_id
    # = Ory UUID (CLAUDE.md §12b) — this is a third independent discovery channel for the
    # same canonical identity, collision-safe guard mirrors identity._sync_dt_user_id_from_uuid.
    try:
        from db.queries.dt_tokens import get_dt_user_id
        from db.connection import get_pool
        dt_uid = await get_dt_user_id(telegram_user_id)
        if dt_uid:
            pool = await get_pool()
            async with pool.acquire() as conn:
                res = await conn.execute(
                    '''UPDATE public.users SET ory_id = $2, updated_at = NOW()
                       WHERE telegram_id = $1 AND ory_id IS NULL
                         AND NOT EXISTS (SELECT 1 FROM public.users u2 WHERE u2.ory_id = $2)''',
                    telegram_user_id, dt_uid,
                )
            if res != 'UPDATE 0':
                logger.info(f"DT: synced ory_id={dt_uid} to users for {telegram_user_id}")
                # WP-268 Phase 2 dual-write: dt_linked (сохранён после IDCOL1 Ф2 —
                # downstream-дашборды могут опираться на этот event_type; account_id
                # теперь = ory_id напрямую, не производный dt_user_id)
                from datetime import datetime as _dt2
                from helpers.dual_write import post_event as _post_event2
                asyncio.create_task(_post_event2(
                    source="aist-bot",
                    external_id=f"dt-linked-{dt_uid}",
                    event_type="dt_linked",
                    schema_version="v1",
                    occurred_at=_dt2.utcnow(),
                    account_id=str(dt_uid),
                    payload={},
                ))
    except Exception as e:
        logger.warning(f"Failed to persist ory_id for {telegram_user_id}: {e}")

    if _bot_instance:
        try:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Мой профиль", callback_data="twin_profile")],
                [InlineKeyboardButton(text="Отключить", callback_data="twin_disconnect")]
            ])

            await _bot_instance.send_message(
                chat_id=telegram_user_id,
                text="*Digital Twin подключён!*\n\nТеперь вы можете просматривать и редактировать свой профиль через /twin.",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Failed to notify user {telegram_user_id}: {e}")

    return web.Response(
        text=f"""
        <html>
        <head>
            <title>Digital Twin подключён!</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    text-align: center;
                    padding: 50px;
                    background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
                    min-height: 100vh;
                    margin: 0;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }}
                .card {{
                    background: white;
                    border-radius: 16px;
                    padding: 40px;
                    box-shadow: 0 10px 40px rgba(0,0,0,0.2);
                    max-width: 400px;
                }}
                h1 {{ color: #0ea5e9; }}
                p {{ color: #666; line-height: 1.6; }}
                .success-icon {{ font-size: 64px; margin-bottom: 16px; }}
            </style>
        </head>
        <body>
            <div class="card">
                <div class="success-icon">✅</div>
                <h1>Digital Twin подключён!</h1>
                <p>Можете закрыть эту страницу и вернуться в Telegram.</p>
            </div>
        </body>
        </html>
        """,
        content_type="text/html",
        status=200
    )


async def github_callback_handler(request: web.Request) -> web.Response:
    """Обрабатывает OAuth callback от GitHub."""
    code = request.query.get("code")
    state = request.query.get("state")
    error = request.query.get("error")

    if error:
        error_description = request.query.get("error_description", "Unknown error")
        logger.error(f"GitHub OAuth error: {error} - {error_description}")
        return web.Response(
            text="""
            <html>
            <head><title>Ошибка авторизации</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Ошибка авторизации GitHub</h1>
                <p>Не удалось завершить авторизацию. Вернитесь в Telegram и попробуйте снова.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=400,
        )

    if not code or not state:
        return web.Response(
            text="""
            <html>
            <head><title>Ошибка</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Неверный запрос</h1>
                <p>Отсутствуют необходимые параметры.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=400,
        )

    telegram_user_id = await github_oauth.validate_state(state)
    if not telegram_user_id:
        return web.Response(
            text="""
            <html>
            <head><title>Сессия истекла</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Сессия авторизации истекла</h1>
                <p>Вернитесь в Telegram и начните авторизацию заново (/github).</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=400,
        )

    tokens = await github_oauth.exchange_code(code, telegram_user_id)
    if not tokens:
        return web.Response(
            text="""
            <html>
            <head><title>Ошибка</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Ошибка получения токена</h1>
                <p>Не удалось завершить авторизацию GitHub.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=500,
        )

    # Получаем имя пользователя GitHub и сохраняем в БД
    user_info = await github_oauth.get_user(telegram_user_id)
    github_login = user_info.get("login", "user") if user_info else "user"

    if user_info and github_login != "user":
        from db.queries.github import save_github_connection, sync_github_to_user_integrations
        access_token = await github_oauth.get_access_token(telegram_user_id)
        if access_token:
            await save_github_connection(
                chat_id=telegram_user_id,
                access_token=access_token,
                github_username=github_login,
            )
            # Dual write: Activity Hub IWE-адаптер (WP-109)
            try:
                await sync_github_to_user_integrations(
                    chat_id=telegram_user_id,
                    access_token=access_token,
                    github_username=github_login,
                )
            except Exception as e:
                logger.warning(f"Failed to sync GitHub to user_integrations: {e}")

    logger.info(
        f"User {telegram_user_id} connected to GitHub as {github_login}"
    )

    if _bot_instance:
        try:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📝 Выбрать репо для заметок",
                            callback_data="github_select_repo",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="📖 Выбрать репо для публикаций",
                            callback_data="github_select_knowledge_repo",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Отключить GitHub",
                            callback_data="github_disconnect",
                        )
                    ],
                ]
            )

            await _bot_instance.send_message(
                chat_id=telegram_user_id,
                text=(
                    f"*GitHub подключён!*\n\n"
                    f"Пользователь: *{github_login}*\n\n"
                    f"Можно подключить два разных репозитория:\n"
                    f"📝 *Заметки* — быстрый захват мыслей из Telegram\n"
                    f"📖 *Публикации* — посты в Клуб\n\n"
                    f"Выберите нужный ниже."
                ),
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error(
                f"Failed to send GitHub notification to user {telegram_user_id}: {e}"
            )

    return web.Response(
        text=f"""
        <html>
        <head>
            <title>GitHub подключён!</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    text-align: center;
                    padding: 50px;
                    background: linear-gradient(135deg, #24292e 0%, #40c463 100%);
                    min-height: 100vh;
                    margin: 0;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }}
                .card {{
                    background: white;
                    border-radius: 16px;
                    padding: 40px;
                    box-shadow: 0 10px 40px rgba(0,0,0,0.2);
                    max-width: 400px;
                }}
                h1 {{ color: #24292e; }}
                p {{ color: #666; line-height: 1.6; }}
                .success-icon {{ font-size: 64px; margin-bottom: 16px; }}
                .name {{ font-weight: bold; color: #333; }}
            </style>
        </head>
        <body>
            <div class="card">
                <div class="success-icon">✅</div>
                <h1>GitHub подключён!</h1>
                <p>Вы авторизованы как <span class="name">{github_login}</span>.</p>
                <p>Можете закрыть эту страницу и вернуться в Telegram.</p>
            </div>
        </body>
        </html>
        """,
        content_type="text/html",
        status=200,
    )


async def google_calendar_callback_handler(request: web.Request) -> web.Response:
    """Обрабатывает OAuth callback от Google Calendar."""
    code = request.query.get("code")
    state = request.query.get("state")
    error = request.query.get("error")

    if error:
        error_description = request.query.get("error_description", "Unknown error")
        logger.error(f"Google Calendar OAuth error: {error} - {error_description}")
        return web.Response(
            text=f"""
            <html>
            <head><title>Ошибка авторизации</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Ошибка авторизации Google Calendar</h1>
                <p>{error_description}</p>
                <p>Вернитесь в Telegram и попробуйте снова.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=400,
        )

    if not code or not state:
        return web.Response(
            text="""
            <html>
            <head><title>Ошибка</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Неверный запрос</h1>
                <p>Отсутствуют необходимые параметры.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=400,
        )

    telegram_user_id = google_calendar_oauth.validate_state(state)
    if not telegram_user_id:
        return web.Response(
            text="""
            <html>
            <head><title>Сессия истекла</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Сессия авторизации истекла</h1>
                <p>Вернитесь в Telegram и начните авторизацию заново (/calendar).</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=400,
        )

    tokens = await google_calendar_oauth.exchange_code(code, state, telegram_user_id)
    if not tokens:
        return web.Response(
            text="""
            <html>
            <head><title>Ошибка</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Ошибка получения токена</h1>
                <p>Не удалось завершить авторизацию Google Calendar.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=500,
        )

    email = tokens.get("email", "")
    logger.info(f"User {telegram_user_id} connected to Google Calendar")

    if _bot_instance:
        try:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Сегодня",
                            callback_data="gcal_today",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Завтра",
                            callback_data="gcal_tomorrow",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Отключить",
                            callback_data="gcal_disconnect",
                        )
                    ],
                ]
            )

            email_line = f"\nАккаунт: *{email}*" if email else ""

            await _bot_instance.send_message(
                chat_id=telegram_user_id,
                text=(
                    f"*Google Calendar подключён!*{email_line}\n\n"
                    f"Используйте /calendar для просмотра событий."
                ),
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error(
                f"Failed to send Google Calendar notification to user {telegram_user_id}: {e}"
            )

    return web.Response(
        text=f"""
        <html>
        <head>
            <title>Google Calendar подключён!</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    text-align: center;
                    padding: 50px;
                    background: linear-gradient(135deg, #4285f4 0%, #34a853 100%);
                    min-height: 100vh;
                    margin: 0;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }}
                .card {{
                    background: white;
                    border-radius: 16px;
                    padding: 40px;
                    box-shadow: 0 10px 40px rgba(0,0,0,0.2);
                    max-width: 400px;
                }}
                h1 {{ color: #4285f4; }}
                p {{ color: #666; line-height: 1.6; }}
                .success-icon {{ font-size: 64px; margin-bottom: 16px; }}
                .email {{ font-weight: bold; color: #333; }}
            </style>
        </head>
        <body>
            <div class="card">
                <div class="success-icon">✅</div>
                <h1>Google Calendar подключён!</h1>
                <p>Можете закрыть эту страницу и вернуться в Telegram.</p>
            </div>
        </body>
        </html>
        """,
        content_type="text/html",
        status=200,
    )


async def wakatime_callback_handler(request: web.Request) -> web.Response:
    """Обрабатывает OAuth callback от WakaTime (WP-109 Activity Hub)."""
    code = request.query.get("code")
    state = request.query.get("state")
    error = request.query.get("error")

    if error:
        error_description = request.query.get("error_description", "Unknown error")
        logger.error(f"WakaTime OAuth error: {error} - {error_description}")
        return web.Response(
            text=f"""
            <html>
            <head><title>Ошибка авторизации</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Ошибка авторизации WakaTime</h1>
                <p>{error_description}</p>
                <p>Вернитесь в Telegram и попробуйте снова.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=400,
        )

    if not code or not state:
        return web.Response(
            text="""
            <html>
            <head><title>Ошибка</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Неверный запрос</h1>
                <p>Отсутствуют необходимые параметры.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=400,
        )

    telegram_user_id = wakatime_oauth.validate_state(state)
    if not telegram_user_id:
        return web.Response(
            text="""
            <html>
            <head><title>Сессия истекла</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Сессия авторизации истекла</h1>
                <p>Вернитесь в Telegram и начните авторизацию заново.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=400,
        )

    tokens = await wakatime_oauth.exchange_code(code, state, telegram_user_id)
    if not tokens:
        return web.Response(
            text="""
            <html>
            <head><title>Ошибка</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Ошибка получения токена</h1>
                <p>Не удалось завершить авторизацию WakaTime.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=500,
        )

    logger.info(f"User {telegram_user_id} connected to WakaTime")

    # wakatime_oauth._save_connection() уже записала в user_integrations.
    # Dual write в wakatime_connections удалён (WP-109/WP-7).

    if _bot_instance:
        try:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Отключить WakaTime",
                            callback_data="wakatime_disconnect",
                        )
                    ],
                ]
            )

            await _bot_instance.send_message(
                chat_id=telegram_user_id,
                text=(
                    "*WakaTime подключён!*\n\n"
                    "Теперь Activity Hub будет автоматически собирать данные о вашем времени в IDE."
                ),
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error(
                f"Failed to send WakaTime notification to user {telegram_user_id}: {e}"
            )

    return web.Response(
        text=f"""
        <html>
        <head>
            <title>WakaTime подключён!</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    text-align: center;
                    padding: 50px;
                    background: linear-gradient(135deg, #2595e5 0%, #2dd4bf 100%);
                    min-height: 100vh;
                    margin: 0;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }}
                .card {{
                    background: white;
                    border-radius: 16px;
                    padding: 40px;
                    box-shadow: 0 10px 40px rgba(0,0,0,0.2);
                    max-width: 400px;
                }}
                h1 {{ color: #2595e5; }}
                p {{ color: #666; line-height: 1.6; }}
                .success-icon {{ font-size: 64px; margin-bottom: 16px; }}
            </style>
        </head>
        <body>
            <div class="card">
                <div class="success-icon">✅</div>
                <h1>WakaTime подключён!</h1>
                <p>Можете закрыть эту страницу и вернуться в Telegram.</p>
            </div>
        </body>
        </html>
        """,
        content_type="text/html",
        status=200,
    )


async def workshop_payment_handler(request: web.Request) -> web.Response:
    """Webhook от Aisystant при оплате семинара WORKSHOP (WP-181).

    POST /webhook/workshop-payment
    Body: {"telegram_id": 123, "amount": 5000, "payment_id": "...", "purpose": "WORKSHOP"}
    """
    import json

    # Аутентификация: секрет в заголовке (fail-closed, аналогично template_update_handler)
    expected_secret = os.getenv("WORKSHOP_WEBHOOK_SECRET", "")
    provided = request.headers.get("X-Webhook-Secret", "")
    if not expected_secret or provided != expected_secret:
        logger.warning(
            "[WorkshopWebhook] invalid secret" if expected_secret
            else "[WorkshopWebhook] WORKSHOP_WEBHOOK_SECRET not configured — rejecting"
        )
        return web.Response(text='{"ok":false,"error":"unauthorized"}',
                            content_type="application/json", status=403)

    try:
        data = await request.json()
    except Exception:
        return web.Response(text='{"ok":false,"error":"invalid json"}',
                            content_type="application/json", status=400)

    purpose = data.get("purpose", "")

    if not _bot_instance:
        logger.error("[WorkshopWebhook] bot instance not set")
        return web.Response(text='{"ok":false,"error":"bot not ready"}',
                            content_type="application/json", status=503)

    try:
        if purpose == "SEMINAR":
            # Оплата семинара из витрины (через Aisystant/Tilda)
            from handlers.showcase import process_seminar_aisystant_webhook
            result = await process_seminar_aisystant_webhook(data, _bot_instance)
        else:
            # Оплата workshop (по умолчанию)
            from handlers.workshop import process_workshop_webhook
            result = await process_workshop_webhook(data, _bot_instance)
        return web.Response(text=json.dumps(result), content_type="application/json")
    except Exception as e:
        logger.error(f"[WorkshopWebhook] error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return web.Response(text=json.dumps({"ok": False, "error": "internal server error"}),
                            content_type="application/json", status=500)


async def yookassa_webhook_handler(request: web.Request) -> web.Response:
    """Webhook от ЮКасса при изменении статуса платежа (WP-181 Ф7).

    POST /webhook/yookassa
    Body: {"event": "payment.succeeded", "object": {"id": "...", "metadata": {"telegram_id": "123"}, ...}}
    """
    import json

    # Проверка IP-адреса отправителя (ЮКасса рекомендует)
    from clients.yookassa import YooKassaClient
    peer = request.remote or ""
    forwarded = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    sender_ip = forwarded or peer

    if not YooKassaClient.verify_notification(b"", sender_ip):
        logger.warning(f"[YooKassa Webhook] rejected: unknown IP {sender_ip}")
        return web.Response(text='{"ok":false,"error":"unauthorized"}',
                            content_type="application/json", status=403)

    # Interim monitoring (WP-458 КР-1): X-Forwarded-For is client-controlled and is the
    # trust source for sender_ip — a mismatch with the real TCP peer isn't blocked here
    # (full fix = server-side verify via YooKassa GET /payments/{id}, tracked as child WP),
    # but it's logged for later audit.
    if forwarded and peer and forwarded != peer:
        logger.warning(
            f"[YooKassa Webhook] accepted via X-Forwarded-For={forwarded}, "
            f"differs from TCP peer={peer} — interim audit trail (КР-1, full fix pending)"
        )

    try:
        data = await request.json()
    except Exception:
        return web.Response(text='{"ok":false,"error":"invalid json"}',
                            content_type="application/json", status=400)

    if not _bot_instance:
        logger.error("[YooKassa Webhook] bot instance not set")
        return web.Response(text='{"ok":false,"error":"bot not ready"}',
                            content_type="application/json", status=503)

    try:
        # Роутинг по metadata.purpose: SEMINAR → showcase, иначе → workshop
        payment_obj = data.get("object", {})
        purpose = payment_obj.get("metadata", {}).get("purpose", "")

        if purpose == "SEMINAR":
            from handlers.showcase import process_seminar_yookassa_webhook
            result = await process_seminar_yookassa_webhook(data, _bot_instance)
        else:
            from handlers.workshop import process_yookassa_webhook
            result = await process_yookassa_webhook(data, _bot_instance)

        return web.Response(text=json.dumps(result), content_type="application/json")
    except Exception as e:
        logger.error(f"[YooKassa Webhook] error: {type(e).__name__}: {str(e)[:200]}")
        return web.Response(text=json.dumps({"ok": False, "error": "internal server error"}),
                            content_type="application/json", status=500)


async def template_update_handler(request: web.Request) -> web.Response:
    """Webhook для GitHub Action: рассылка обновлений шаблона IWE подписчикам.

    POST /api/template-update
    Headers: X-Webhook-Secret: <TEMPLATE_WEBHOOK_SECRET>
    Body JSON: {version, changelog, commit_count}
    """
    import os
    import json

    # Аутентификация
    expected_secret = os.getenv('TEMPLATE_WEBHOOK_SECRET', '')
    provided_secret = request.headers.get('X-Webhook-Secret', '')

    if not expected_secret or provided_secret != expected_secret:
        logger.warning("[TemplateUpdate] Invalid or missing webhook secret")
        return web.Response(text="Forbidden", status=403)

    try:
        body = await request.json()
    except Exception:
        return web.Response(text="Bad request", status=400)

    version = body.get('version', '')
    changelog = body.get('changelog', '')
    commit_count = body.get('commit_count', 0)

    if not commit_count:
        return web.Response(text=json.dumps({"ok": True, "sent": 0, "reason": "no_commits"}), content_type="application/json")

    # Не рассылаем, если changelog пустой или тривиальный (нет содержательных изменений)
    if not changelog or not changelog.strip():
        logger.info("[TemplateUpdate] Empty changelog with %d commits — skipping notification", commit_count)
        return web.Response(text=json.dumps({"ok": True, "sent": 0, "reason": "empty_changelog"}), content_type="application/json")

    # LLM-переработка changelog в user-friendly release notes
    # Fallback: regex-очистка (прежнее поведение) при любой ошибке LLM
    repo_url = "https://github.com/TserenTserenov/FMT-exocortex-template"
    rewritten_body = None

    try:
        from clients.claude import claude
        from config import CLAUDE_MODEL_HAIKU

        release_note_prompt = (
            "Ты объясняешь другу за чашкой кофе, что нового появилось в продукте IWE "
            "(Intellectual Work Environment — интеллектуальная рабочая среда, шаблон экзокортекса для Claude Code). "
            "Друг не программист и не читал код — ему важно, что стало лучше ДЛЯ НЕГО, а не как это устроено внутри.\n\n"
            "Перепиши технический changelog в понятный рассказ на русском языке.\n\n"
            "Правила:\n"
            "1. Нумерованные пункты на верхнем уровне (1, 2, 3...), вложенные — буллеты (•)\n"
            "2. Каждый пункт начинай с пользы или эффекта («стало быстрее», «теперь не теряется» и т.п.), "
            "не с названия компонента или файла\n"
            "3. ЗАПРЕЩЕНЫ внутренние имена и коды: номера рабочих продуктов (WP-102), названия модулей/скриптов/"
            "переменных (guide-kit, ResidencyGate, USER-SPACE и т.п.), имена файлов, аббревиатуры без расшифровки. "
            "Если для сути пункта имя всё же нужно — поясни его смысл русским словом перед именем, в скобках\n"
            "4. Секции: Добавлено, Изменено, Исправлено (только те, что есть в changelog)\n"
            "5. Формат вывода — HTML для Telegram: <b>жирный</b> для заголовков секций, "
            "без markdown. Не используй <ul>/<ol>/<li> — Telegram их не поддерживает\n"
            "6. Кратко, по делу, без воды. Максимум 20 строк\n"
            "7. НЕ добавляй ничего от себя — только переформулируй то, что есть в changelog\n"
            "8. НЕ добавляй заголовок, приветствие или заключение — только тело release notes\n"
            "9. ГРУППИРОВКА ПО СМЫСЛУ (не по хронологии коммитов): объединяй пункты в 4-6 смысловых блоков "
            "(например «быстрее открывается день», «надёжнее хранятся личные данные», «починили ошибки») "
            "вместо построчного пересказа списка коммитов. Если несколько пунктов описывают одну и ту же "
            "функцию — объедини в ОДИН пункт с общим описанием, не повторяй одно и то же разными словами\n"
            "10. ПОЛНОТА: каждый пункт должен быть содержательным и отличаться от других. "
            "Если после группировки осталось мало пунктов — это нормально, не раздувай\n"
            "11. ПРОЧИЕ ИЗМЕНЕНИЯ: если в changelog есть мелкие правки, рефакторинг или "
            "технические изменения — добавь в конце строку: «А также мелкие улучшения и исправления»\n"
            "12. ССЫЛКИ НА ФАЙЛЫ: если в changelog есть markdown-ссылки вида "
            "[имя](путь/к/файлу) — преобразуй их в HTML-ссылки на GitHub: "
            f'<a href="{repo_url}/blob/main/путь/к/файлу">имя</a>. '
            "Ставь ссылку на ключевой файл изменения (1-2 ссылки на пункт, не больше). "
            "Если ссылок в changelog нет — не выдумывай"
        )

        rewritten_body = await claude.generate(
            system_prompt=release_note_prompt,
            user_prompt=changelog,
            max_tokens=1500,
            model=CLAUDE_MODEL_HAIKU,
        )

        if rewritten_body:
            rewritten_body = rewritten_body.strip()
            # Detect if LLM returned raw Markdown instead of HTML (headers, bold, tables)
            if re.search(r'(^#{1,6}\s|\*\*.+\*\*|\|[-| :]+\|)', rewritten_body, re.MULTILINE):
                logger.warning("[TemplateUpdate] LLM returned Markdown instead of HTML, falling back to regex")
                rewritten_body = None
            else:
                logger.info("[TemplateUpdate] LLM rewrite successful (%d chars)", len(rewritten_body))
        else:
            logger.warning("[TemplateUpdate] LLM returned empty, falling back to regex")

    except Exception as e:
        logger.warning("[TemplateUpdate] LLM rewrite failed (%s), falling back to regex", e)
        rewritten_body = None

    if rewritten_body:
        # LLM-переработанный вариант
        from helpers.message_split import sanitize_file_extensions
        rewritten_body = sanitize_file_extensions(rewritten_body)
        message_text = (
            f"🔄 <b>Обновление шаблона IWE {version}</b>\n\n"
            f"{rewritten_body}\n\n"
            f"<b>Как обновить:</b>\n"
            f'1. Скажите своему Claude: <i>«обнови мой экзокортекс»</i>\n'
            f"2. Или в терминале: <code>bash update.sh</code>\n"
            f"3. Проверить версию: <code>bash update.sh --check</code>\n\n"
            f'<a href="{repo_url}">Репозиторий шаблона</a>'
        )
    else:
        # Fallback: regex cleanup
        clean_changelog = changelog
        section_map = {
            'Added': 'Добавлено', 'Fixed': 'Исправлено', 'Changed': 'Изменено',
            'Removed': 'Удалено', 'Deprecated': 'Устарело', 'Security': 'Безопасность',
        }
        for en, ru in section_map.items():
            clean_changelog = re.sub(rf'^#{{1,6}}\s*{en}\b', f'\n<b>{ru}</b>', clean_changelog, flags=re.MULTILINE)
        # Strip remaining markdown headers
        clean_changelog = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', clean_changelog, flags=re.MULTILINE)
        # Strip markdown tables: separator rows removed, data rows flattened to bullet points
        clean_changelog = re.sub(r'^\|[-| :]+\|\s*$', '', clean_changelog, flags=re.MULTILINE)
        clean_changelog = re.sub(r'^\|(.+)\|$', lambda m: '• ' + ' — '.join(c.strip() for c in m.group(1).split('|') if c.strip()), clean_changelog, flags=re.MULTILINE)
        clean_changelog = re.sub(r'\*\*(.+?)\*\*', r'\1', clean_changelog)
        clean_changelog = re.sub(r'`(.+?)`', r'\1', clean_changelog)
        from helpers.message_split import sanitize_file_extensions
        clean_changelog = sanitize_file_extensions(clean_changelog)
        clean_changelog = re.sub(r'^-\s+', '• ', clean_changelog, flags=re.MULTILINE)
        clean_changelog = re.sub(r'^\s+-\s+', '  • ', clean_changelog, flags=re.MULTILINE)
        clean_changelog = re.sub(r'\n{3,}', '\n\n', clean_changelog).strip()

        message_text = (
            f"🔄 <b>Обновление шаблона IWE {version}</b>\n\n"
            f"{commit_count} коммит(ов) за последние 24ч\n\n"
            f"{clean_changelog}\n\n"
            f"<b>Как обновить:</b>\n"
            f'1. Скажите своему Claude: <i>«обнови мой экзокортекс»</i>\n'
            f"2. Или в терминале: <code>bash update.sh</code>\n"
            f"3. Проверить версию: <code>bash update.sh --check</code>\n\n"
            f'<a href="{repo_url}">Репозиторий шаблона</a>'
        )

    # Получаем подписчиков
    from db.queries.users import get_template_update_subscribers
    subscribers = await get_template_update_subscribers()

    if not subscribers:
        logger.info("[TemplateUpdate] No subscribers, skipping broadcast")
        return web.Response(text=json.dumps({"ok": True, "sent": 0}), content_type="application/json")

    # Рассылка с батчингом (30 msg/sec TG limit)
    sent = 0
    failed = 0
    for i, chat_id in enumerate(subscribers):
        try:
            await _bot_instance.send_message(
                chat_id=chat_id,
                text=message_text,
                parse_mode="HTML",
            )
            sent += 1
        except Exception as e:
            logger.warning(f"[TemplateUpdate] Failed to send to {chat_id}: {e}")
            failed += 1

        # TG rate limit: 30 msg/sec → sleep every 25 messages
        if (i + 1) % 25 == 0:
            await asyncio.sleep(1)

    logger.info(f"[TemplateUpdate] Broadcast done: sent={sent}, failed={failed}")

    result = {"ok": True, "sent": sent, "failed": failed}
    return web.Response(text=json.dumps(result), content_type="application/json")


async def github_workbook_webhook_handler(request: web.Request) -> web.Response:
    """GitHub webhook: события от GitHub App «Aisystant Personal Guide» (WP-301 Ф7+).

    Маршрутизирует три типа событий:
    - **push** в `workbook/YYYY-MM-DD.md` → ingest_event + sync_one_user_to_dt (Ф9-B)
    - **installation** (created/deleted/suspend/unsuspend) → save/delete App installation
    - **installation_repositories** (added/removed) → update app_repo_full_name

    Аутентификация: HMAC-SHA256, два секрета поддерживаются (App webhook > legacy):
    - `GITHUB_APP_WEBHOOK_SECRET` (приоритет; для App events)
    - `GITHUB_WORKBOOK_WEBHOOK_SECRET` (fallback; legacy per-repo webhooks)

    Маппинг пользователя:
    - App events: `installation_id` из payload → `github_connections.app_installation_id`
    - Legacy: `pusher.name` → `github_connections.github_username` (fallback)
    """
    import hashlib
    import hmac
    import json as _json
    import asyncio

    # ── Аутентификация: пробуем оба секрета ─────────────────────────────────
    body = await request.read()
    sig_header = request.headers.get("X-Hub-Signature-256", "")
    app_secret = os.getenv("GITHUB_APP_WEBHOOK_SECRET", "")
    legacy_secret = os.getenv("GITHUB_WORKBOOK_WEBHOOK_SECRET", "")

    auth_ok = False
    used_secret = None
    if app_secret:
        expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig_header, expected):
            auth_ok = True
            used_secret = "app"
    if not auth_ok and legacy_secret:
        expected = "sha256=" + hmac.new(legacy_secret.encode(), body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig_header, expected):
            auth_ok = True
            used_secret = "legacy"

    if not auth_ok and (app_secret or legacy_secret):
        logger.warning("[WorkbookWebhook] invalid HMAC signature")
        return web.Response(
            text='{"ok":false,"error":"unauthorized"}',
            content_type="application/json", status=403,
        )

    # ── Парсинг payload ─────────────────────────────────────────────────────
    try:
        payload = _json.loads(body)
    except Exception:
        return web.Response(
            text='{"ok":false,"error":"invalid json"}',
            content_type="application/json", status=400,
        )

    event_type = request.headers.get("X-GitHub-Event", "")
    installation = payload.get("installation") or {}
    installation_id = installation.get("id")

    # ── installation events (App установлен/удалён/suspended) ───────────────
    if event_type == "installation":
        action = payload.get("action", "")
        if not installation_id:
            return web.Response(text='{"ok":false,"error":"no installation.id"}',
                                content_type="application/json", status=400)

        from db.queries.github_app import (
            save_app_installation, mark_installation_suspended, delete_installation,
        )
        if action == "created":
            # Нужен chat_id из state (передан при /auth/github_app/setup).
            # Если установка прошла не через наш flow (пилот вручную) — installation_id
            # есть, но chat_id нет. Полагаемся на /auth/github_app/callback для
            # сохранения mapping. Здесь только логируем.
            repos = payload.get("repositories") or []
            repo_name = repos[0].get("full_name", "") if repos else ""
            logger.info(
                "[WorkbookWebhook] installation created: id=%d, repo=%s, action=%s",
                installation_id, repo_name, action,
            )
            return web.Response(text='{"ok":true,"action":"created","note":"awaiting callback"}',
                                content_type="application/json")
        elif action in ("deleted", "suspend"):
            if action == "deleted":
                await delete_installation(installation_id)
            else:
                await mark_installation_suspended(installation_id, True)
            logger.info("[WorkbookWebhook] installation %s: %d", action, installation_id)
            return web.Response(text=f'{{"ok":true,"action":"{action}"}}',
                                content_type="application/json")
        elif action == "unsuspend":
            await mark_installation_suspended(installation_id, False)
            return web.Response(text='{"ok":true,"action":"unsuspend"}',
                                content_type="application/json")
        return web.Response(text=f'{{"ok":true,"skipped":"installation/{action}"}}',
                            content_type="application/json")

    # ── push events ─────────────────────────────────────────────────────────
    if event_type != "push":
        return web.Response(
            text=f'{{"ok":true,"skipped":"event {event_type}"}}',
            content_type="application/json",
        )

    # ── Фильтр путей: workbook/ (legacy) + lesson/YYYY-MM-DD.md (WP-149 Ф4) ──
    # Еженедельные гиды (lesson/YYYY-WNN.md) не триггерят — они рендерятся
    # платформой, не пишутся учеником.
    commits = payload.get("commits") or []
    changed = [
        f for commit in commits
        for f in (commit.get("added", []) + commit.get("modified", []))
    ]
    workbook_files = [f for f in changed if f.startswith("workbook/")]
    lesson_files = [
        f for f in changed
        if re.match(r'^lesson/\d{4}-\d{2}-\d{2}\.md$', f)
    ]
    if not workbook_files and not lesson_files:
        return web.Response(
            text='{"ok":true,"skipped":"no workbook or lesson files"}',
            content_type="application/json",
        )

    # ── Маппинг user: сначала по installation_id, fallback на pusher.name ───
    # Источник user_uuid: github_connections.user_uuid (Ory UUID = dt_user_id).
    # dt_tokens используется только как fallback, если github_connections.user_uuid NULL.
    from db.connection import get_pool, get_secrets_pool
    from db.queries.dt_sync import sync_one_user_to_dt

    secrets_pool = await get_secrets_pool()
    bot_pool = await get_pool()
    chat_id = None
    user_uuid_from_gh = None
    pusher_login = (payload.get("pusher") or {}).get("name", "")

    if installation_id:
        # App path: installation_id → chat_id + user_uuid
        async with secrets_pool.acquire() as conn:
            app_row = await conn.fetchrow(
                """
                SELECT chat_id, user_uuid, app_suspended
                FROM public.github_connections
                WHERE app_installation_id = $1
                LIMIT 1
                """,
                installation_id,
            )
        if app_row and app_row.get("chat_id") and not app_row.get("app_suspended"):
            chat_id = app_row["chat_id"]
            user_uuid_from_gh = app_row["user_uuid"]

    if not chat_id and pusher_login:
        # Legacy fallback: github_username → chat_id + user_uuid
        async with secrets_pool.acquire() as conn:
            gh_row = await conn.fetchrow(
                """
                SELECT chat_id, user_uuid FROM public.github_connections
                WHERE github_username = $1 LIMIT 1
                """,
                pusher_login,
            )
        if gh_row and gh_row["chat_id"]:
            chat_id = gh_row["chat_id"]
            user_uuid_from_gh = gh_row["user_uuid"]

    if not chat_id:
        logger.warning(
            "[WorkbookWebhook] no user mapping: installation_id=%s, pusher=%s",
            installation_id, pusher_login,
        )
        return web.Response(
            text='{"ok":false,"error":"user not found"}',
            content_type="application/json", status=404,
        )

    # user_uuid: 1) из github_connections (primary), 2) fallback dt_tokens
    dt_user_id = None
    if user_uuid_from_gh:
        dt_user_id = str(user_uuid_from_gh)
    else:
        async with secrets_pool.acquire() as conn:
            dt_row = await conn.fetchrow(
                "SELECT dt_user_id FROM public.dt_tokens WHERE chat_id = $1 LIMIT 1",
                chat_id,
            )
        if dt_row and dt_row.get("dt_user_id"):
            dt_user_id = str(dt_row["dt_user_id"])

    if not dt_user_id:
        logger.warning(
            "[WorkbookWebhook] no user_uuid for chat_id=%s (installation_id=%s, pusher=%s)",
            chat_id, installation_id, pusher_login,
        )
        return web.Response(
            text='{"ok":false,"error":"user_uuid not found"}',
            content_type="application/json", status=404,
        )

    commit_sha = payload.get("after", "unknown")

    # ── Activity Hub: записать событие(я) (lightweight, без импорта activity_hub) ─
    # Note: external_id колонка отсутствует в текущей схеме development.user_events.
    # Идемпотентность достигается через дедуп по (user_uuid, event_type, payload->commit_sha)
    # на потребительской стороне (engagement view может агрегировать с DISTINCT).
    # WP-149 Ф4: lesson_closed для ежедневных файлов занятий; workbook_push — legacy путь.
    all_files = workbook_files + lesson_files
    payload_json = json.dumps({
        "files": all_files,
        "repo": (payload.get("repository") or {}).get("full_name", ""),
        "commit_sha": commit_sha,
        "installation_id": installation_id,
        "via": used_secret,
    })
    sender_type = (payload.get("sender") or {}).get("type", "User")
    event_types = []
    if lesson_files and sender_type != "Bot":
        event_types.append("lesson_closed")
    elif lesson_files:
        logger.info("[WorkbookWebhook] lesson_closed skipped: sender type=Bot (renderer push)")
    if workbook_files:
        event_types.append("workbook_push")

    try:
        async with bot_pool.acquire() as conn:
            for evt in event_types:
                await conn.execute(
                    """
                    INSERT INTO development.user_events
                        (user_id, user_uuid, event_type, source, payload,
                         confidence, created_at)
                    VALUES (0, $1, $2, $3, $4, $5, NOW())
                    """,
                    uuid.UUID(dt_user_id),
                    evt,
                    "iwe",
                    payload_json,
                    1.0,
                )
        logger.info("[WorkbookWebhook] event(s) written to user_events: %s (%s)", commit_sha, event_types)
    except Exception as e:
        logger.warning("[WorkbookWebhook] ingest_event failed: %s", e)

    # ── On-demand пересчёт ЦД ────────────────────────────────────────────────
    asyncio.create_task(sync_one_user_to_dt(dt_user_id))

    logger.info(
        "[WorkbookWebhook] push by chat_id=%s (dt=%s, install=%s, via=%s), files=%s, dt_sync scheduled",
        chat_id, dt_user_id, installation_id, used_secret, workbook_files,
    )
    return web.Response(
        text='{"ok":true}',
        content_type="application/json",
    )


async def ory_callback_handler(request: web.Request) -> web.Response:
    """Обрабатывает OAuth callback от Ory (WP-187: бот+Ory, T0→T1).

    Ory Hydra редиректит сюда с параметрами:
    - code: authorization code
    - state: state для верификации (содержит telegram_user_id)
    """
    code = request.query.get("code")
    state = request.query.get("state")
    error = request.query.get("error")

    if error:
        error_description = request.query.get("error_description", "Unknown error")
        logger.error(f"Ory OAuth error: {error} - {error_description}")
        return web.Response(
            text="""
            <html>
            <head><title>Ошибка авторизации</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Ошибка авторизации</h1>
                <p>Не удалось завершить авторизацию. Вернитесь в Telegram и попробуйте снова.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=400
        )

    if not code or not state:
        return web.Response(
            text="""
            <html>
            <head><title>Ошибка</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Неверный запрос</h1>
                <p>Отсутствуют необходимые параметры.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=400
        )

    # Валидируем state и получаем user_id
    telegram_user_id = await ory_oauth.validate_state(state)
    if not telegram_user_id:
        return web.Response(
            text="""
            <html>
            <head><title>Сессия истекла</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Сессия авторизации истекла</h1>
                <p>Вернитесь в Telegram и начните авторизацию заново.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=400,
        )

    # Обмениваем code на токен
    tokens = await ory_oauth.exchange_code(code, telegram_user_id)
    if not tokens:
        return web.Response(
            text="""
            <html>
            <head><title>Ошибка</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Ошибка получения токена</h1>
                <p>Не удалось завершить авторизацию. Попробуйте снова.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=500
        )

    access_token = tokens.get("access_token")

    # Получаем профиль из Ory
    userinfo = await ory_oauth.get_userinfo(access_token) if access_token else None
    if not userinfo or not userinfo.get("sub"):
        logger.error(f"[OryOAuth] No sub in userinfo for user {telegram_user_id}")
        return web.Response(
            text="""
            <html>
            <head><title>Ошибка</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Ошибка получения профиля</h1>
                <p>Не удалось получить данные из Ory. Попробуйте снова.</p>
            </body>
            </html>
            """,
            content_type="text/html",
            status=500
        )

    ory_id = userinfo["sub"]
    email = userinfo.get("email")

    # Сохраняем Ory tokens для Gateway MCP (WP-209 Ф0)
    refresh_token = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in", 3600)
    logger.info(
        f"[OryOAuth] Token fields: access={bool(access_token)}, "
        f"refresh={bool(refresh_token)}, expires_in={expires_in}, "
        f"keys={list(tokens.keys())}"
    )
    if access_token:
        from datetime import datetime, timedelta
        from db.queries.ory_tokens import save_ory_tokens
        expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
        await save_ory_tokens(
            chat_id=telegram_user_id,
            access_token=access_token,
            refresh_token=refresh_token or "",
            expires_at=expires_at,
            ory_id=ory_id,
        )
        logger.info(f"[OryOAuth] Saved Ory tokens for user {telegram_user_id}, expires_in={expires_in}s")
        # Обновляем in-memory tokens для Gateway MCP
        from clients.gateway_mcp import gateway_mcp
        gateway_mcp.set_tokens(telegram_user_id, access_token, refresh_token or "", expires_at, ory_id)

    # Привязываем ory_id к telegram_id (T0→T1)
    from db.queries.identity import link_ory
    linked = await link_ory(telegram_user_id, ory_id, email)

    if linked:
        logger.info(f"[OryOAuth] Linked ory_id={ory_id} for telegram_id={telegram_user_id}")
    else:
        logger.warning(f"[OryOAuth] link_ory returned False for telegram_id={telegram_user_id}")

    # WP-227 Ф6: backfill ЦД при T0→T1 OAuth (вариант B).
    # T0 пользователь впервые получает ory_id — создаём запись в digital_twins.
    # Если запись уже существует (повторный OAuth) — не перезаписываем 2_collected.
    try:
        from db.connection import get_pool
        pool = await get_pool()
        async with pool.acquire() as dt_conn:
            await dt_conn.execute('''
                INSERT INTO digital_twins (user_id, data, created_at, updated_at)
                VALUES ($1, '{}'::jsonb, NOW(), NOW())
                ON CONFLICT (user_id) DO NOTHING
            ''', ory_id)
            logger.info(f"[OryOAuth] WP-227: digital_twins record ensured for ory_id={ory_id[:8]}")
    except Exception as e:
        logger.warning(f"[OryOAuth] WP-227: digital_twins backfill failed for {ory_id[:8]}: {e}")

    # Уведомляем пользователя в Telegram
    if _bot_instance:
        try:
            display = email or ory_id[:8]
            await _bot_instance.send_message(
                chat_id=telegram_user_id,
                text=(
                    f"Регистрация завершена!\n\n"
                    f"Аккаунт: {display}\n"
                    f"Теперь вам доступны расширенные функции платформы."
                ),
            )
        except Exception as e:
            logger.error(f"[OryOAuth] Failed to notify user {telegram_user_id}: {e}")

    return web.Response(
        text="""
        <html>
        <head>
            <title>Регистрация завершена!</title>
            <style>
                body {
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    text-align: center;
                    padding: 50px;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    min-height: 100vh;
                    margin: 0;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }
                .card {
                    background: white;
                    border-radius: 16px;
                    padding: 40px;
                    box-shadow: 0 10px 40px rgba(0,0,0,0.2);
                    max-width: 400px;
                }
                h1 { color: #5E6AD2; margin-bottom: 16px; }
                p { color: #666; line-height: 1.6; }
                .success-icon { font-size: 64px; margin-bottom: 16px; }
            </style>
        </head>
        <body>
            <div class="card">
                <div class="success-icon">&#10003;</div>
                <h1>Регистрация завершена!</h1>
                <p>Можете закрыть эту страницу и вернуться в Telegram.</p>
            </div>
        </body>
        </html>
        """,
        content_type="text/html",
        status=200
    )


async def internal_remind_handler(request: web.Request) -> web.Response:
    """WP-320 Ф2: пользователь-инициированные напоминания через MCP-tool send_telegram_message.
    see DP.SC.134, DP.ROLE.044

    POST /internal/remind
    Headers: X-Notify-Signature: sha256=<hmac> (same INTERNAL_NOTIFY_SECRET)
    Body JSON: {ory_user_id: str, text: str, schedule_at?: str (ISO 8601)}
    Response: {reminder_id: int, scheduled_at: str, status: "queued"|"sent"|"failed"}
    """
    import os
    import hashlib
    import hmac
    import json
    from datetime import datetime, timedelta

    secret = os.getenv('INTERNAL_NOTIFY_SECRET', '')
    if not secret:
        logger.warning("[InternalRemind] INTERNAL_NOTIFY_SECRET not configured")
        return web.Response(text="Service unavailable", status=503)

    raw_body = await request.read()

    provided_sig = request.headers.get('X-Notify-Signature', '')
    expected_mac = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    expected_sig = f"sha256={expected_mac}"
    if not hmac.compare_digest(provided_sig, expected_sig):
        logger.warning("[InternalRemind] Invalid HMAC signature")
        return web.Response(text="Forbidden", status=403)

    try:
        body = json.loads(raw_body)
    except Exception:
        return web.Response(text="Bad request", status=400)

    ory_user_id = body.get('ory_user_id')
    text = body.get('text', '').strip()
    schedule_at_str = body.get('schedule_at')

    if not ory_user_id or not text:
        return web.Response(
            text=json.dumps({"ok": False, "reason": "missing ory_user_id or text"}),
            content_type="application/json", status=400
        )

    try:
        from db.connection import get_persona_pool, get_learning_pool

        # Resolve chat_id from ory_user_id via persona.ory_identity (Neon, WP-269)
        pool = await get_persona_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT telegram_id FROM public.ory_identity WHERE account_id = $1::uuid LIMIT 1',
                ory_user_id
            )
        if not row or not row['telegram_id']:
            logger.warning("[InternalRemind] No telegram_id for ory_user_id=%s", ory_user_id)
            return web.Response(
                text=json.dumps({"ok": False, "reason": "user_not_found"}),
                content_type="application/json", status=404
            )
        chat_id = row['telegram_id']

        # Parse schedule_at
        now_naive = datetime.utcnow()
        if schedule_at_str:
            try:
                # Accept ISO 8601, strip tzinfo for naive TIMESTAMP column (rule 10.6)
                scheduled_for = datetime.fromisoformat(schedule_at_str.replace('Z', '+00:00'))
                scheduled_for = scheduled_for.replace(tzinfo=None)
            except ValueError:
                return web.Response(
                    text=json.dumps({"ok": False, "reason": "invalid schedule_at format, use ISO 8601"}),
                    content_type="application/json", status=400
                )
        else:
            # Immediate: schedule 5s in the future so scheduler picks it up on next tick
            scheduled_for = now_naive + timedelta(seconds=5)

        # Ensure text column exists in learning.reminder (idempotent migration)
        learning_pool = await get_learning_pool()
        async with learning_pool.acquire() as conn:
            await conn.execute(
                "ALTER TABLE reminder ADD COLUMN IF NOT EXISTS text TEXT"
            )
            # WP-212: bot_id из TELEGRAM_BOT_TOKEN для изоляции напоминаний по инстансам
            _bot_id = int(os.getenv('TELEGRAM_BOT_TOKEN', '0').split(':')[0])
            record = await conn.fetchrow(
                '''INSERT INTO reminder (chat_id, reminder_type, scheduled_for, text, bot_id)
                   VALUES ($1, $2, $3, $4, $5)
                   RETURNING id, scheduled_for''',
                chat_id, 'custom', scheduled_for, text, _bot_id
            )

        reminder_id = record['id']
        scheduled_at_iso = record['scheduled_for'].isoformat()
        logger.info("[InternalRemind] reminder_id=%s chat_id=%s scheduled_for=%s", reminder_id, chat_id, scheduled_for)

        return web.Response(
            text=json.dumps({"ok": True, "reminder_id": reminder_id, "scheduled_at": scheduled_at_iso, "status": "queued"}),
            content_type="application/json"
        )

    except Exception as e:
        logger.exception("[InternalRemind] Error: %s", e)
        return web.Response(
            text=json.dumps({"ok": False, "error": str(e)}),
            content_type="application/json", status=500
        )


async def internal_notify_handler(request: web.Request) -> web.Response:
    """WP-5 Ф12: внутреннее уведомление от gateway-mcp о начале индексации форкнутого репо.

    POST /internal/notify
    Headers: X-Notify-Signature: sha256=<hmac>
    Body JSON: {type, telegram_id, repo_name, user_id}
    """
    import os
    import hashlib
    import hmac
    import json

    secret = os.getenv('INTERNAL_NOTIFY_SECRET', '')
    if not secret:
        logger.warning("[InternalNotify] INTERNAL_NOTIFY_SECRET not configured")
        return web.Response(text="Service unavailable", status=503)

    raw_body = await request.read()

    # HMAC-SHA256 verification
    provided_sig = request.headers.get('X-Notify-Signature', '')
    expected_mac = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    expected_sig = f"sha256={expected_mac}"
    if not hmac.compare_digest(provided_sig, expected_sig):
        logger.warning("[InternalNotify] Invalid HMAC signature")
        return web.Response(text="Forbidden", status=403)

    try:
        body = json.loads(raw_body)
    except Exception:
        return web.Response(text="Bad request", status=400)

    notify_type = body.get('type')
    telegram_id = body.get('telegram_id')
    repo_name = body.get('repo_name', '')

    if notify_type == 'ops_alert':
        # WP-391 Ф6 P0#2 (пир-сессия 2026-07-29, Kimi+Hermes): ops-канал для
        # reindex-алертов. Чат — OPS_ALERT_CHAT_ID (env); без него алерт деградирует
        # в лог (поведение до этой правки) и 503 вызывающему — видно в мониторинге.
        import html as _html
        ops_chat_id = os.getenv('OPS_ALERT_CHAT_ID', '')
        if not ops_chat_id:
            logger.warning("[InternalNotify] ops_alert получен, но OPS_ALERT_CHAT_ID не задан: %s", body)
            return web.Response(text=json.dumps({"ok": False, "reason": "ops_chat_not_configured"}), content_type="application/json", status=503)
        if not _bot_instance:
            logger.warning("[InternalNotify] Bot instance not ready")
            return web.Response(text=json.dumps({"ok": False, "reason": "bot_not_ready"}), content_type="application/json", status=503)
        source = _html.escape(str(body.get('source', '?')))
        failed = body.get('failed', [])
        if isinstance(failed, list):
            details = "; ".join(
                _html.escape(f"{f.get('target', '?')}: {f.get('error', '?')}") for f in failed[:3] if isinstance(f, dict)
            ) or "—"
        else:
            details = _html.escape(str(failed))[:300]
        try:
            await _bot_instance.send_message(
                chat_id=int(ops_chat_id),
                text=(
                    f"🚨 <b>Reindex alert</b>: источник <code>{source}</code>\n"
                    f"Провал целей переиндексации: {details}\n"
                    f"Индекс может устаревать молча — нужна ручная переиндексация."
                ),
                parse_mode="HTML",
            )
            logger.info("[InternalNotify] ops_alert source=%s sent", source)
            return web.Response(text=json.dumps({"ok": True}), content_type="application/json")
        except Exception as e:
            logger.exception("[InternalNotify] Error sending ops_alert: %s", e)
            return web.Response(text=json.dumps({"ok": False, "error": str(e)}), content_type="application/json", status=500)

    if notify_type != 'repo_indexing_started' or not telegram_id:
        logger.warning("[InternalNotify] Unknown type or missing telegram_id: %s", body)
        return web.Response(text=json.dumps({"ok": False, "reason": "unknown_type"}), content_type="application/json")

    if not _bot_instance:
        logger.warning("[InternalNotify] Bot instance not ready")
        return web.Response(text=json.dumps({"ok": False, "reason": "bot_not_ready"}), content_type="application/json", status=503)

    try:
        from db.queries.notifications import send_idempotent

        idempotency_key = f"repo_indexing:{telegram_id}:{repo_name}"
        text = (
            f"🔄 <b>Репо {repo_name}</b> добавлен и начал индексироваться.\n"
            f"Через несколько минут его содержимое будет доступно в поиске."
        )

        async def _send():
            await _bot_instance.send_message(chat_id=telegram_id, text=text, parse_mode="HTML")

        sent = await send_idempotent(
            chat_id=telegram_id,
            notification_type="repo_indexing_started",
            idempotency_key=idempotency_key,
            send_fn=_send,
        )
        logger.info("[InternalNotify] repo=%s telegram_id=%s sent=%s", repo_name, telegram_id, sent)
        return web.Response(text=json.dumps({"ok": True, "sent": sent}), content_type="application/json")

    except Exception as e:
        logger.exception("[InternalNotify] Error sending notification: %s", e)
        return web.Response(text=json.dumps({"ok": False, "error": str(e)}), content_type="application/json", status=500)


def _make_app_setup_state(chat_id: int) -> str:
    """Подписанный state-token для GitHub App install flow.

    Формат: <chat_id>.<timestamp>.<hmac>
    Подпись: HMAC-SHA256 с GITHUB_APP_WEBHOOK_SECRET (или INTERNAL_NOTIFY_SECRET fallback).
    TTL проверки: 2 часа в _verify_app_setup_state (расширено с 30 мин по UX feedback —
    пилот может оставить GitHub-вкладку открытой и вернуться позже).
    """
    import hashlib
    import hmac as _hmac
    import time as _time
    secret = (os.getenv("GITHUB_APP_WEBHOOK_SECRET")
              or os.getenv("INTERNAL_NOTIFY_SECRET") or "")
    if not secret:
        # fail-safe: state без подписи (только для dev)
        return f"{chat_id}.{int(_time.time())}.dev"
    ts = int(_time.time())
    msg = f"{chat_id}.{ts}".encode()
    sig = _hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:32]
    return f"{chat_id}.{ts}.{sig}"


def _verify_app_setup_state(state: str, max_age_seconds: int = 7200) -> Optional[int]:
    """Проверить state-token, вернуть chat_id или None при невалидности."""
    import hashlib
    import hmac as _hmac
    import time as _time
    try:
        chat_id_str, ts_str, sig = state.split(".", 2)
        chat_id = int(chat_id_str)
        ts = int(ts_str)
    except (ValueError, AttributeError):
        return None
    if _time.time() - ts > max_age_seconds:
        return None
    secret = (os.getenv("GITHUB_APP_WEBHOOK_SECRET")
              or os.getenv("INTERNAL_NOTIFY_SECRET") or "")
    if not secret:
        return chat_id if sig == "dev" else None
    expected = _hmac.new(secret.encode(), f"{chat_id}.{ts}".encode(), hashlib.sha256).hexdigest()[:32]
    if not _hmac.compare_digest(sig, expected):
        return None
    return chat_id


async def github_app_setup_handler(request: web.Request) -> web.Response:
    """GET /auth/github_app/setup?telegram_user_id=X

    Стартует install flow: подписывает state с chat_id, редиректит на GitHub
    App install URL. После установки GitHub отправит ответ в callback.

    Требует env-vars:
    - GITHUB_APP_SLUG (например, "aisystant-personal-guide")
    """
    chat_id_param = request.query.get("telegram_user_id", "").strip()
    if not chat_id_param or not chat_id_param.isdigit():
        return web.Response(
            text="Missing or invalid telegram_user_id", status=400,
        )
    chat_id = int(chat_id_param)
    app_slug = os.getenv("GITHUB_APP_SLUG", "").strip()
    if not app_slug:
        return web.Response(
            text="GITHUB_APP_SLUG не установлен (платформа ещё не зарегистрировала App)",
            status=500,
        )
    state = _make_app_setup_state(chat_id)
    install_url = f"https://github.com/apps/{app_slug}/installations/new?state={state}"
    logger.info("[GitHubApp] redirect chat_id=%d to install_url", chat_id)
    raise web.HTTPFound(install_url)


async def github_app_callback_handler(request: web.Request) -> web.Response:
    """GET /auth/github_app/callback?installation_id=Y&setup_action=install&state=<token>

    GitHub вызывает после успешной установки App. Сохраняет (chat_id, installation_id, repo).
    """
    installation_id_str = request.query.get("installation_id", "").strip()
    setup_action = request.query.get("setup_action", "").strip()
    state = request.query.get("state", "").strip()

    if not installation_id_str or not installation_id_str.isdigit():
        return web.Response(
            text="<h1>Ошибка</h1><p>Нет installation_id в callback</p>",
            content_type="text/html", status=400,
        )
    installation_id = int(installation_id_str)
    chat_id = _verify_app_setup_state(state) if state else None

    # Graceful fallback: state протух или невалиден, но installation_id есть в БД —
    # значит установка уже была успешно сохранена ранее. Показать success вместо ошибки.
    if not chat_id:
        from db.queries.github_app import find_user_by_installation_id
        existing = await find_user_by_installation_id(installation_id)
        if existing and existing.get("chat_id"):
            logger.info(
                "[GitHubApp] callback with stale state but installation_id=%d already mapped to chat_id=%s — graceful success",
                installation_id, existing["chat_id"],
            )
            return web.Response(
                text=f"""<!DOCTYPE html>
<html><head><title>Уже установлен</title><meta charset="utf-8"></head>
<body style="font-family: sans-serif; max-width: 600px; margin: 50px auto;">
<h1>✅ App уже установлен</h1>
<p>Установка для этого репозитория уже зарегистрирована в системе ранее.</p>
<p>Можно закрыть это окно и вернуться в Telegram или Claude Code.</p>
<p><small>Installation ID: <code>{installation_id}</code></small></p>
</body></html>""",
                content_type="text/html",
            )
        return web.Response(
            text="""<!DOCTYPE html>
<html><head><title>Сессия истекла</title><meta charset="utf-8"></head>
<body style="font-family: sans-serif; max-width: 600px; margin: 50px auto;">
<h1>⏱ Сессия установки истекла</h1>
<p>С момента запуска установки прошло больше 2 часов, ссылка стала невалидной.</p>
<p>Не страшно — запусти установку заново:</p>
<ul>
<li>В Telegram: <code>/connect_guide</code> в боте</li>
<li>В Claude Code: <code>/connect-guide</code></li>
</ul>
</body></html>""",
            content_type="text/html", status=400,
        )

    # Получить репо через App API (нужны installation_token + list repos)
    from clients import github_app as gha
    repos = await gha.get_installation_repos(installation_id)
    repo_full_name = repos[0].get("full_name", "") if repos else ""
    if not repo_full_name:
        logger.warning(
            "[GitHubApp] callback: no repos for installation_id=%d (chat_id=%d)",
            installation_id, chat_id,
        )

    # github_username: попробуем получить через API
    github_username = ""
    if repo_full_name and "/" in repo_full_name:
        github_username = repo_full_name.split("/", 1)[0]

    from db.queries.github_app import save_app_installation
    ok, status = await save_app_installation(chat_id, installation_id, repo_full_name, github_username)
    if not ok:
        if status == "ory_login_required":
            return web.Response(
                text="""<!DOCTYPE html>
<html><head><title>Нужен Ory-логин</title><meta charset="utf-8"></head>
<body style="font-family: sans-serif; max-width: 600px; margin: 50px auto;">
<h1>⚠️ Сначала логин Ory</h1>
<p>App установлен на GitHub, но платформа ещё не знает, кто ты —
нужно завершить onboarding в боте.</p>
<p>1. Открой бот в Telegram</p>
<p>2. Нажми /start (если не делал) или /github</p>
<p>3. Вернись и нажми /connect_guide ещё раз</p>
<p><small>Installation ID: {installation_id} — сохранится при повторной попытке.</small></p>
</body></html>""".replace("{installation_id}", str(installation_id)),
                content_type="text/html", status=409,
            )
        return web.Response(
            text=f"<h1>Ошибка сохранения</h1><p>Статус: {status}. Попробуйте ещё раз или напишите в поддержку.</p>",
            content_type="text/html", status=500,
        )

    # Уведомление в бот
    if _bot_instance:
        try:
            await _bot_instance.send_message(
                chat_id,
                f"✅ <b>GitHub App установлен</b>\n\n"
                f"Репо: <code>{repo_full_name or 'не определён'}</code>\n"
                f"Installation ID: <code>{installation_id}</code>\n\n"
                f"Теперь Портной может писать assignments в твой репо, "
                f"а твои push в <code>workbook/</code> автоматически обновляют ЦД.",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("[GitHubApp] notify bot failed: %s", e)

    return web.Response(
        text=f"""<!DOCTYPE html>
<html><head><title>Установка завершена</title><meta charset="utf-8"></head>
<body style="font-family: sans-serif; max-width: 600px; margin: 50px auto;">
<h1>✅ App установлен</h1>
<p>Репозиторий: <code>{repo_full_name or '(не определён)'}</code></p>
<p>Installation ID: <code>{installation_id}</code></p>
<p>Можно закрыть это окно и вернуться в Telegram.</p>
</body></html>""",
        content_type="text/html",
    )


async def chatwoot_webhook_handler(request: web.Request) -> web.Response:
    """Получает события от Chatwoot и пересылает ответы агентов пользователям TG.

    WP-341 Этап 2 (DP.SC.150).
    """
    body = await request.read()

    from clients.chatwoot import verify_signature
    sig = request.headers.get("X-Chatwoot-Signature", "")
    if not verify_signature(body, sig):
        logger.warning("[Chatwoot webhook] invalid signature")
        return web.Response(status=401, text="invalid signature")

    try:
        payload = json.loads(body)
    except Exception:
        return web.Response(status=400, text="bad json")

    event = payload.get("event")
    message_type = payload.get("message_type")
    conv_id = (payload.get("conversation") or {}).get("id") or payload.get("conversation_id")
    logger.info("[Chatwoot webhook] event=%r message_type=%r conv_id=%r keys=%s",
                event, message_type, conv_id, list(payload.keys()))

    if event != "message_created":
        return web.Response(text="ok")

    # message_type 1 = outgoing (agent → customer); also accept string "outgoing"
    if message_type not in (1, "outgoing"):
        return web.Response(text="ok")

    content = payload.get("content", "").strip()
    if not content:
        return web.Response(text="ok")

    if not conv_id:
        return web.Response(text="ok")

    try:
        from db.queries.helpdesk import get_chat_id_by_conversation
        chat_id = await get_chat_id_by_conversation(conv_id)
        if chat_id and _bot_instance:
            await _bot_instance.send_message(
                chat_id,
                f"🛟 *Агент поддержки:*\n\n{content}",
                parse_mode="Markdown",
            )
            logger.info("[Chatwoot webhook] forwarded conv=%s → chat_id=%s", conv_id, chat_id)
    except Exception as e:
        logger.error("[Chatwoot webhook] forward error: %s", e)

    return web.Response(text="ok")


# ── Discourse events → IWE event pipeline ──────────────────────────────────

_DISCOURSE_EVENT_MAP = {
    "post_created": "club_post_created",
    "topic_created": "club_topic_created",
    "user_created": "club_user_created",
    "like_created": "club_like_created",
    "badge_granted": "club_badge_granted",
    "trust_level_changed": "club_trust_promoted",
}


async def discourse_webhook_handler(request: web.Request) -> web.Response:
    """POST /webhook/discourse — события Discourse → IWE event pipeline.

    WP-327 Этап 23 (DP.SC.154 peer-session 2026-05-28-26).
    HMAC-SHA256 через X-Discourse-Event-Signature: sha256=<hmac> + DISCOURSE_WEBHOOK_SECRET.
    Если секрет не задан — пропускаем проверку (development mode).
    """
    import hashlib
    import hmac

    body = await request.read()

    secret = os.getenv("DISCOURSE_WEBHOOK_SECRET", "")
    if secret:
        sig_header = request.headers.get("X-Discourse-Event-Signature", "")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            logger.warning("[DiscourseWebhook] invalid HMAC signature")
            return web.Response(status=403, text="unauthorized")

    try:
        payload = json.loads(body)
    except Exception:
        return web.Response(status=400, text="bad json")

    discourse_event = request.headers.get("X-Discourse-Event", "")
    iwe_event_type = _DISCOURSE_EVENT_MAP.get(discourse_event)

    if iwe_event_type is None:
        return web.Response(text="ok")  # неизвестное событие — тихо игнорируем

    # trust_level_changed: пропускаем downgrade
    if discourse_event == "trust_level_changed":
        changes = payload.get("previous_changes", {}).get("trust_level", [])
        if len(changes) != 2 or changes[0] >= changes[1]:
            return web.Response(text="ok")

    # Определяем username и event_id для идентификации
    user = payload.get("user") or payload.get("post", {}).get("user") or {}
    username = (
        payload.get("username")
        or user.get("username")
        or (payload.get("post") or {}).get("username")
        or (payload.get("topic") or {}).get("last_poster_username")
    )
    if not username:
        logger.info("[DiscourseWebhook] event=%s: no username in payload", discourse_event)
        return web.Response(text="ok")

    # event_id для идемпотентности
    post_id = (payload.get("post") or {}).get("id")
    topic_id = (payload.get("topic") or {}).get("id")
    user_id = (user or {}).get("id") or payload.get("user_id")
    badge_id = (payload.get("user_badge") or {}).get("badge_id")

    if discourse_event == "post_created" and post_id:
        external_id = f"discourse-post-created-{post_id}"
    elif discourse_event == "topic_created" and topic_id:
        external_id = f"discourse-topic-created-{topic_id}"
    elif discourse_event == "user_created" and user_id:
        external_id = f"discourse-user-created-{user_id}"
    elif discourse_event == "like_created" and post_id:
        external_id = f"discourse-like-created-{post_id}-{user_id}"
    elif discourse_event == "badge_granted" and badge_id and user_id:
        external_id = f"discourse-badge-granted-{user_id}-{badge_id}"
    elif discourse_event == "trust_level_changed" and user_id:
        changes = payload.get("previous_changes", {}).get("trust_level", [0, 0])
        external_id = f"discourse-trust-promoted-{user_id}-{changes[0]}-{changes[1]}"
    else:
        external_id = f"discourse-{discourse_event}-{username}"

    try:
        from db.queries.discourse import get_chat_id_by_discourse_username
        chat_id = await get_chat_id_by_discourse_username(username)
        if chat_id is None:
            logger.info("[DiscourseWebhook] event=%s username=%r: no account mapping", discourse_event, username)
            return web.Response(text="ok")

        from helpers.dual_write import resolve_ory_id_from_chat, post_event
        from datetime import datetime, timezone
        ory_id = await resolve_ory_id_from_chat(chat_id)
        if not ory_id:
            logger.info("[DiscourseWebhook] event=%s username=%r chat_id=%s: no ory_id", discourse_event, username, chat_id)
            return web.Response(text="ok")

        # Фильтрация PII: передаём только структурные идентификаторы (B7.3)
        safe_payload: dict = {}
        if discourse_event == "post_created":
            post = payload.get("post") or {}
            safe_payload = {"post_id": post.get("id"), "topic_id": post.get("topic_id"), "post_number": post.get("post_number")}
        elif discourse_event == "topic_created":
            topic = payload.get("topic") or {}
            safe_payload = {"topic_id": topic.get("id"), "category_id": topic.get("category_id")}
        elif discourse_event == "user_created":
            safe_payload = {"user_id": user_id, "username": username, "trust_level": (payload.get("user") or {}).get("trust_level")}
        elif discourse_event == "like_created":
            safe_payload = {"post_id": post_id, "user_id": user_id}
        elif discourse_event == "badge_granted":
            safe_payload = {"badge_id": badge_id, "user_id": user_id}
        elif discourse_event == "trust_level_changed":
            changes = payload.get("previous_changes", {}).get("trust_level", [0, 0])
            safe_payload = {"user_id": user_id, "from_level": changes[0], "to_level": changes[1]}

        await post_event(
            source="discourse",
            external_id=external_id,
            event_type=iwe_event_type,
            schema_version="v1",
            occurred_at=datetime.now(timezone.utc),
            account_id=ory_id,
            payload=safe_payload,
        )
        logger.info("[DiscourseWebhook] event=%s → %s external_id=%s ory_id=%s", discourse_event, iwe_event_type, external_id, ory_id)
    except Exception as e:
        logger.error("[DiscourseWebhook] event=%s error: %s", discourse_event, e)

    return web.Response(text="ok")


_typing_rate_limit: dict[str, float] = {}  # token → last_request_ts (WP-327 Phase 4)


async def typing_delta_handler(request: web.Request) -> web.Response:
    """POST /api/typing_delta — Browser Extension heartbeat endpoint.

    Auth: X-IWE-Api-Key header (32-byte hex token из user_integrations).
    Rate limit: 1 req/30s per token.
    WP-327 Phase 4 (DP.SC.NNN).
    """
    import time
    from helpers.dual_write import post_event
    from db.queries.iwe_extension import validate_extension_token
    from datetime import timezone, datetime

    token = request.headers.get("X-IWE-Api-Key", "").strip()
    if not token:
        return web.Response(status=401, text="missing X-IWE-Api-Key")

    # Validate token first — don't write invalid tokens to rate-limit dict.
    account_id = await validate_extension_token(token)
    if not account_id:
        return web.Response(status=401, text="invalid token")

    # Rate limit: 1 req/30s per token (in-memory, resets on restart).
    # TTL cleanup: evict entries older than 1h to bound dict growth.
    now = time.monotonic()
    last = _typing_rate_limit.get(token, 0.0)
    if now - last < 30.0:
        return web.Response(status=429, text="rate limit: 1 req/30s")
    _typing_rate_limit[token] = now
    cutoff = now - 3600
    for k in [k for k, v in _typing_rate_limit.items() if v < cutoff]:
        _typing_rate_limit.pop(k, None)

    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="bad json")

    try:
        net_typed: int = int(body.get("net_typed", 0))
        paste_events: int = int(body.get("paste_events", 0))
        interface: str = str(body.get("interface", "claude_ai"))
        duration_ms: int = int(body.get("duration_ms", 30000))
    except (ValueError, TypeError):
        return web.Response(status=400, text="invalid field types")

    if net_typed <= 0:
        return web.Response(text="ok")

    channel_weight = 0.5  # keydown без paste = medium accuracy
    weighted = round(net_typed * channel_weight, 2)
    ts = datetime.now(timezone.utc)
    date_str = ts.date().isoformat()
    # external_id в миллисекундах — уникален для каждого heartbeat (rate limit 30s >> 1ms коллизия)
    external_id = f"extension-{account_id}-{int(ts.timestamp() * 1000)}"

    await post_event(
        source="aist-bot",
        external_id=external_id,
        event_type="user_typing_tracked",
        schema_version="v1",
        occurred_at=ts.replace(tzinfo=None),
        account_id=account_id,
        payload={
            "interface": interface,
            "date": date_str,
            "net_typed": net_typed,
            "paste_events": paste_events,
            "weighted_chars": weighted,
            "channel_weight": channel_weight,
            "duration_ms": duration_ms,
        },
    )
    logger.info("[typing_delta] account=%s net_typed=%d weighted=%.1f",
                account_id, net_typed, weighted)
    return web.Response(text="ok")


_TIER_INT_TO_STR = {0: "T1", 1: "T1", 2: "T2", 3: "T3", 4: "T4", 5: "T4"}


async def external_auth_exchange_handler(request: web.Request) -> web.Response:
    """WP-411 Ф2/Ф4: обмен одноразового кода на пару access+refresh токенов.

    POST /internal/auth/exchange
    Body JSON: {"code": "<uuid>"}
    Response: {"access_token": "ict_...", "refresh_token": "irt_...", "scope": "full"}
    Errors: 400 missing code | 404 code not found/expired | 500 internal
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    code = body.get("code", "").strip()
    if not code:
        return web.json_response({"error": "code required"}, status=400)

    from db.queries.external_clients import exchange_code_and_store_tokens, peek_auth_code_chat_id
    from clients.external_auth import generate_access_token, generate_refresh_token
    from core.tier_detector import detect_ui_tier

    # Peek chat_id to compute tier before consuming the code.
    chat_id = await peek_auth_code_chat_id(code)
    if chat_id is None:
        return web.json_response({"error": "code not found or expired"}, status=404)

    try:
        tier_int = await detect_ui_tier(chat_id)
    except Exception:
        logger.warning("[ExternalAuth] exchange: detect_ui_tier failed for chat_id=%s, defaulting T1", chat_id)
        tier_int = 1
    computed_tier = _TIER_INT_TO_STR.get(tier_int, "T1")

    try:
        at_plain, at_hash = generate_access_token()
        rt_plain, rt_hash = generate_refresh_token()
    except RuntimeError as e:
        logger.error("[ExternalAuth] exchange: EXTERNAL_AUTH_KEY not configured: %s", e)
        return web.json_response({"error": "server misconfigured"}, status=500)

    try:
        result = await exchange_code_and_store_tokens(
            code=code,
            access_hash=at_hash,
            refresh_hash=rt_hash,
            computed_tier=computed_tier,
        )
    except Exception:
        logger.exception("[ExternalAuth] exchange: exchange_code_and_store_tokens failed")
        return web.json_response({"error": "internal error"}, status=500)

    if result is None:
        logger.warning("[ExternalAuth] exchange: code not found or expired")
        return web.json_response({"error": "code not found or expired"}, status=404)

    logger.info("[ExternalAuth] exchange: issued token tier=%s for account %s", computed_tier, result["account_id"])

    # DP.SC.190 Q2: аудит-лог выдачи внешнего пропуска (кто и когда получил доступ).
    try:
        from db.queries.events import log_event
        await log_event(
            chat_id,
            "external_client_token_issued",
            payload={"account_id": result["account_id"], "tier": computed_tier, "scope": result["scope"]},
            source="bot",
        )
    except Exception:
        logger.warning("[ExternalAuth] exchange: audit log_event failed for chat_id=%s", chat_id)

    return web.json_response({
        "access_token": at_plain,
        "refresh_token": rt_plain,
        "scope": result["scope"],
    })


async def external_auth_introspect_handler(request: web.Request) -> web.Response:
    """WP-411 Ф2: gateway-mcp → проверка access-токена, возврат account_id.

    POST /internal/auth/introspect
    Headers: X-Introspect-Secret: <secret>
    Body JSON: {"access_token": "ict_..."}
    Response: {"account_id": "uuid", "scope": "full", "tier": "T1".."T4"}
    Errors: 401 bad secret | 400 missing token | 404 token not found/revoked
    """
    from clients.external_auth import verify_introspect_secret, hash_token
    from db.queries.external_clients import lookup_client_token, touch_client_token

    provided = request.headers.get("X-Introspect-Secret", "")
    if not verify_introspect_secret(provided):
        logger.warning("[ExternalAuth] introspect: bad secret from %s", request.remote)
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    token = body.get("access_token", "").strip()
    if not token:
        return web.json_response({"error": "access_token required"}, status=400)

    token_hash = hash_token(token)
    row = await lookup_client_token(token_hash)
    if row is None:
        return web.json_response({"error": "token not found"}, status=404)

    # fire-and-forget last_used update
    asyncio.ensure_future(touch_client_token(row["id"]))

    return web.json_response({
        "account_id": row["account_id"],
        "scope": row["scope"],
        "tier": row["computed_tier"],
    })


async def external_auth_refresh_handler(request: web.Request) -> web.Response:
    """WP-411 Ф4: refresh irt_ token → new ict_+irt_ pair with recomputed tier.

    POST /internal/auth/refresh
    Headers: X-Introspect-Secret: <secret>
    Body JSON: {"refresh_token": "irt_..."}
    Response: {"access_token": "ict_...", "refresh_token": "irt_..."}
    Errors: 401 bad secret | 400 missing token | 404 not found/revoked | 500 internal
    """
    from clients.external_auth import (
        verify_introspect_secret, hash_token,
        generate_access_token, generate_refresh_token,
    )
    from db.queries.external_clients import lookup_refresh_token, refresh_client_token
    from core.tier_detector import detect_ui_tier

    provided = request.headers.get("X-Introspect-Secret", "")
    if not verify_introspect_secret(provided):
        logger.warning("[ExternalAuth] refresh: bad secret from %s", request.remote)
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    rt_plain = body.get("refresh_token", "").strip()
    if not rt_plain:
        return web.json_response({"error": "refresh_token required"}, status=400)

    rt_hash = hash_token(rt_plain)
    old = await lookup_refresh_token(rt_hash)
    if old is None:
        return web.json_response({"error": "refresh token not found"}, status=404)

    chat_id = old.get("chat_id")
    if chat_id:
        try:
            tier_int = await detect_ui_tier(chat_id)
        except Exception:
            logger.warning("[ExternalAuth] refresh: detect_ui_tier failed for chat_id=%s", chat_id)
            tier_int = 1
        computed_tier = _TIER_INT_TO_STR.get(tier_int, "T1")
    else:
        computed_tier = "T1"
        logger.warning("[ExternalAuth] refresh: no chat_id on token %s, tier defaulting T1", old["id"])

    try:
        at_plain, at_hash = generate_access_token()
        new_rt_plain, new_rt_hash = generate_refresh_token()
    except RuntimeError as e:
        logger.error("[ExternalAuth] refresh: EXTERNAL_AUTH_KEY not configured: %s", e)
        return web.json_response({"error": "server misconfigured"}, status=500)

    ok = await refresh_client_token(
        old_refresh_hash=rt_hash,
        new_access_hash=at_hash,
        new_refresh_hash=new_rt_hash,
        computed_tier=computed_tier,
    )
    if not ok:
        return web.json_response({"error": "refresh token already revoked"}, status=404)

    logger.info("[ExternalAuth] refresh: re-issued token tier=%s for account %s", computed_tier, old["account_id"])
    return web.json_response({
        "access_token": at_plain,
        "refresh_token": new_rt_plain,
    })


def create_oauth_app(dp=None, bot=None) -> web.Application:
    """Создаёт aiohttp приложение для OAuth + опционально Telegram webhook.

    Args:
        dp: aiogram Dispatcher (если передан, добавляет webhook handler)
        bot: aiogram Bot instance
    """
    app = web.Application()

    # Webhook request logging (diagnose silent failures)
    @web.middleware
    async def webhook_logging_middleware(request: web.Request, handler):
        if request.path == "/telegram" and request.method == "POST":
            has_secret = "X-Telegram-Bot-Api-Secret-Token" in request.headers
            logger.info(f"[Webhook] POST /telegram (secret_header={'yes' if has_secret else 'NO'})")
        resp = await handler(request)
        if request.path == "/telegram" and request.method == "POST":
            logger.info(f"[Webhook] Response: {resp.status}")
        return resp

    app.middlewares.append(webhook_logging_middleware)

    app.router.add_get("/health", health_handler)
    app.router.add_get("/auth/linear/callback", linear_callback_handler)
    app.router.add_get("/auth/twin/callback", twin_callback_handler)
    app.router.add_get("/auth/github/callback", github_callback_handler)
    app.router.add_get("/auth/google-calendar/callback", google_calendar_callback_handler)
    app.router.add_get("/auth/wakatime/callback", wakatime_callback_handler)
    app.router.add_get("/auth/ory/callback", ory_callback_handler)
    app.router.add_post("/api/template-update", template_update_handler)
    app.router.add_post("/webhook/workshop-payment", workshop_payment_handler)
    app.router.add_post("/webhook/yookassa", yookassa_webhook_handler)
    app.router.add_post("/webhook/github/workbook", github_workbook_webhook_handler)
    app.router.add_get("/auth/github_app/setup", github_app_setup_handler)  # WP-301 Ф7
    app.router.add_get("/auth/github_app/callback", github_app_callback_handler)  # WP-301 Ф7
    app.router.add_post("/internal/notify", internal_notify_handler)  # WP-5 Ф12
    app.router.add_post("/internal/remind", internal_remind_handler)  # WP-320 Ф2 DP.SC.134
    app.router.add_post("/internal/auth/exchange", external_auth_exchange_handler)      # WP-411 Ф2
    app.router.add_post("/internal/auth/introspect", external_auth_introspect_handler)  # WP-411 Ф2
    app.router.add_post("/internal/auth/refresh", external_auth_refresh_handler)        # WP-411 Ф4
    app.router.add_post("/webhook/chatwoot", chatwoot_webhook_handler)   # WP-341 Этап 2
    app.router.add_post("/webhook/discourse", discourse_webhook_handler) # WP-327 Этап 23
    app.router.add_post("/api/typing_delta", typing_delta_handler)       # WP-327 Phase 4: Browser Extension heartbeat

    # Webhook route (WP-44: polling → webhooks)
    if dp is not None and bot is not None:
        try:
            from aiogram.webhook.aiohttp_server import SimpleRequestHandler
            from config.settings import WEBHOOK_PATH, WEBHOOK_SECRET

            webhook_handler = SimpleRequestHandler(
                dispatcher=dp,
                bot=bot,
                secret_token=WEBHOOK_SECRET,
            )
            webhook_handler.register(app, path=WEBHOOK_PATH)
            logger.info(f"Webhook handler registered at {WEBHOOK_PATH}")
        except Exception as e:
            logger.error(f"❌ Failed to register webhook handler: {e}")

    return app


async def start_oauth_server():
    """Запускает OAuth сервер."""
    app = create_oauth_app()
    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", OAUTH_SERVER_PORT)  # nosec B104 (Railway container)
    await site.start()

    logger.info(f"OAuth server started on port {OAUTH_SERVER_PORT}")
    return runner


async def stop_oauth_server(runner: web.AppRunner):
    """Останавливает OAuth сервер."""
    await runner.cleanup()
    logger.info("OAuth server stopped")
