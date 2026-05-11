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
    # update_user_dt() ниже отдельно эмитит dt_linked.v1 (низкоуровневая привязка id).
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
            _chat_hash = _hashlib.sha1(str(telegram_user_id).encode()).hexdigest()[:12]
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
    # Синхронизация произойдёт при следующем _sync_dt_connected_users из scheduler.
    try:
        from db.queries.users import get_intern
        intern = await get_intern(telegram_user_id)
        if intern:
            from clients.gateway_mcp import gateway_mcp
            synced = await gateway_mcp.sync_profile(telegram_user_id, intern)
            logger.info(f"DT initial sync for user {telegram_user_id}: {synced} fields")
    except Exception as e:
        logger.error(f"DT initial sync failed for user {telegram_user_id}: {e}")

    # Persist dt_user_id in public.users (WP-82)
    try:
        from db.queries.dt_tokens import get_dt_user_id
        dt_uid = await get_dt_user_id(telegram_user_id)
        if dt_uid:
            from db.queries.identity import update_user_dt
            await update_user_dt(telegram_user_id, dt_uid)
            logger.info(f"DT: saved dt_user_id={dt_uid} to users for {telegram_user_id}")
    except Exception as e:
        logger.warning(f"Failed to persist dt_user_id for {telegram_user_id}: {e}")

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
    logger.info(f"User {telegram_user_id} connected to Google Calendar ({email})")

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

    # Аутентификация: секрет в заголовке (аналогично template_update_handler)
    expected_secret = os.getenv("WORKSHOP_WEBHOOK_SECRET", "")
    if expected_secret:
        provided = request.headers.get("X-Webhook-Secret", "")
        if provided != expected_secret:
            logger.warning("[WorkshopWebhook] invalid secret")
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
        logger.error(f"[YooKassa Webhook] error: {e}")
        import traceback
        logger.error(traceback.format_exc())
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
            "Ты — копирайтер, пишущий release notes для продукта IWE "
            "(Intellectual Work Environment — интеллектуальная рабочая среда, шаблон экзокортекса для Claude Code).\n\n"
            "Перепиши технический changelog в user-friendly release notes на русском языке.\n\n"
            "Правила:\n"
            "1. Нумерованные пункты на верхнем уровне (1, 2, 3...), вложенные — буллеты (•)\n"
            "2. Объясняй СУТЬ изменения для пользователя, не начинай с имён файлов\n"
            "3. Убери внутренние номера (WP-102 и т.п.) — пользователи их не знают\n"
            "4. Секции: Добавлено, Изменено, Исправлено (только те, что есть в changelog)\n"
            "5. Формат вывода — HTML для Telegram: <b>жирный</b> для заголовков секций, "
            "без markdown. Не используй <ul>/<ol>/<li> — Telegram их не поддерживает\n"
            "6. Кратко, по делу, без воды. Максимум 20 строк\n"
            "7. НЕ добавляй ничего от себя — только переформулируй то, что есть в changelog\n"
            "8. НЕ добавляй заголовок, приветствие или заключение — только тело release notes\n"
            "9. ГРУППИРОВКА: если несколько пунктов описывают одну и ту же функцию "
            "(например, одна фича добавлена в 3 места) — объедини в ОДИН пункт с общим описанием. "
            "Не повторяй одно и то же разными словами\n"
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
        # Fallback: regex-очистка (прежнее поведение)
        import re
        clean_changelog = changelog
        section_map = {
            'Added': 'Добавлено', 'Fixed': 'Исправлено', 'Changed': 'Изменено',
            'Removed': 'Удалено', 'Deprecated': 'Устарело', 'Security': 'Безопасность',
        }
        for en, ru in section_map.items():
            clean_changelog = re.sub(rf'^#{{1,3}}\s*{en}\b', f'\n<b>{ru}</b>', clean_changelog, flags=re.MULTILINE)
        clean_changelog = re.sub(r'^#{1,3}\s*', '', clean_changelog, flags=re.MULTILINE)
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

    # ── Фильтр путей: только workbook/ ──────────────────────────────────────
    commits = payload.get("commits") or []
    workbook_files = [
        f for commit in commits
        for f in (commit.get("added", []) + commit.get("modified", []))
        if f.startswith("workbook/")
    ]
    if not workbook_files:
        return web.Response(
            text='{"ok":true,"skipped":"no workbook files"}',
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

    # ── Activity Hub: записать событие (lightweight, без импорта activity_hub) ─
    # Note: external_id колонка отсутствует в текущей схеме development.user_events.
    # Идемпотентность достигается через дедуп по (user_uuid, event_type, payload->commit_sha)
    # на потребительской стороне (engagement view может агрегировать с DISTINCT).
    payload_json = json.dumps({
        "files": workbook_files,
        "repo": (payload.get("repository") or {}).get("full_name", ""),
        "commit_sha": commit_sha,
        "installation_id": installation_id,
        "via": used_secret,
    })
    try:
        async with bot_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO development.user_events
                    (user_id, user_uuid, event_type, source, payload,
                     confidence, created_at)
                VALUES (0, $1, $2, $3, $4, $5, NOW())
                """,
                uuid.UUID(dt_user_id),
                "workbook_push",
                "iwe",
                payload_json,
                1.0,
            )
        logger.info("[WorkbookWebhook] event written to user_events: %s", commit_sha)
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
    TTL проверки: 30 мин в _verify_app_setup_state.
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


def _verify_app_setup_state(state: str, max_age_seconds: int = 1800) -> Optional[int]:
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
    if not chat_id:
        return web.Response(
            text="<h1>Ошибка</h1><p>Невалидный state. Запустите установку из бота заново.</p>",
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

    site = web.TCPSite(runner, "0.0.0.0", OAUTH_SERVER_PORT)
    await site.start()

    logger.info(f"OAuth server started on port {OAUTH_SERVER_PORT}")
    return runner


async def stop_oauth_server(runner: web.AppRunner):
    """Останавливает OAuth сервер."""
    await runner.cleanup()
    logger.info("OAuth server stopped")
