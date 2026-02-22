"""
OAuth callback сервер.

Обрабатывает OAuth callbacks от Linear, Digital Twin, GitHub.
Запускается параллельно с ботом на порту 8080.

Endpoints:
- GET /auth/linear/callback — OAuth callback от Linear
- GET /auth/twin/callback — OAuth callback от Digital Twin
- GET /auth/github/callback — OAuth callback от GitHub
- GET /health — health check для Railway
"""

import asyncio
from aiohttp import web
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import get_logger, OAUTH_SERVER_PORT
from clients.linear_oauth import linear_oauth
from clients.digital_twin import digital_twin
from clients.github_oauth import github_oauth

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
    telegram_user_id = linear_oauth.validate_state(state)
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
    tokens = await linear_oauth.exchange_code(code, state)
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
                'UPDATE interns SET dt_connected_at = NOW() WHERE chat_id = $1',
                telegram_user_id,
            )
    except Exception as e:
        logger.warning(f"Failed to persist DT connection for {telegram_user_id}: {e}")

    # Автоматический перелив профиля бота → ЦД
    try:
        from db.queries.users import get_intern
        intern = await get_intern(telegram_user_id)
        if intern:
            synced = await digital_twin.sync_profile(telegram_user_id, intern)
            logger.info(f"DT initial sync for user {telegram_user_id}: {synced} fields")
    except Exception as e:
        logger.error(f"DT initial sync failed for user {telegram_user_id}: {e}")

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
            text=f"""
            <html>
            <head><title>Ошибка авторизации</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                <h1>Ошибка авторизации GitHub</h1>
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

    telegram_user_id = github_oauth.validate_state(state)
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

    tokens = await github_oauth.exchange_code(code, state)
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
        from db.queries.github import save_github_connection
        access_token = await github_oauth.get_access_token(telegram_user_id)
        if access_token:
            await save_github_connection(
                chat_id=telegram_user_id,
                access_token=access_token,
                github_username=github_login,
            )

    logger.info(
        f"User {telegram_user_id} connected to GitHub as {github_login}"
    )

    if _bot_instance:
        try:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Выбрать репо для заметок",
                            callback_data="github_select_repo",
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
                    f"Теперь выберите репозиторий для исчезающих заметок."
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


def create_oauth_app(dp=None, bot=None) -> web.Application:
    """Создаёт aiohttp приложение для OAuth + опционально Telegram webhook.

    Args:
        dp: aiogram Dispatcher (если передан, добавляет webhook handler)
        bot: aiogram Bot instance
    """
    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_get("/auth/linear/callback", linear_callback_handler)
    app.router.add_get("/auth/twin/callback", twin_callback_handler)
    app.router.add_get("/auth/github/callback", github_callback_handler)

    # Webhook route (WP-44: polling → webhooks)
    if dp is not None and bot is not None:
        try:
            from aiogram.webhook.aiohttp_server import SimpleRequestHandler
            from config.settings import WEBHOOK_PATH, WEBHOOK_SECRET

            webhook_handler = SimpleRequestHandler(
                dispatcher=dp,
                bot=bot,
                secret_token=WEBHOOK_SECRET or None,
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
