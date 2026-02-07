"""
OAuth callback сервер — тестовая интеграция.

Этот сервер обрабатывает OAuth callbacks от Linear.
Запускается параллельно с ботом на порту 8080.

Endpoints:
- GET /auth/linear/callback — OAuth callback от Linear
- GET /health — health check для Railway
"""

import asyncio
from aiohttp import web
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import get_logger, OAUTH_SERVER_PORT
from clients.linear_oauth import linear_oauth

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


def create_oauth_app() -> web.Application:
    """Создаёт aiohttp приложение для OAuth."""
    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_get("/auth/linear/callback", linear_callback_handler)
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
