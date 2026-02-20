"""
Хендлеры интеграции с Discourse (systemsworld.club).

Команды:
- /club — подключение/статус/мои публикации
- /club connect <URL или username> — привязать аккаунт
- /club disconnect — отвязать
- /club publish — опубликовать пост
- /club posts — мои публикации
"""

import re
import logging

from aiogram import Router
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from db.queries import get_intern
from db.queries.discourse import (
    get_discourse_account,
    link_discourse_account,
    unlink_discourse_account,
    get_published_posts,
    save_published_post,
    is_title_published,
)

logger = logging.getLogger(__name__)

discourse_router = Router(name="discourse")


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


# ── Helpers ───────────────────────────────────────────────

def _parse_blog_input(text: str) -> tuple[str | None, int | None]:
    """Parse blog URL or text → (username_guess, category_id).

    Accepts:
    - URL: https://systemsworld.club/c/blogs/tseren-tserenov/37
    - "username 37"
    - Plain username
    """
    text = text.strip()

    # URL: /c/parent_slug/child_slug/ID
    m = re.search(r'systemsworld\.club/c/[^/]+/([^/]+)/(\d+)', text)
    if m:
        return m.group(1), int(m.group(2))

    # URL: /c/slug/ID (no child slug)
    m = re.search(r'systemsworld\.club/c/[^/]+/(\d+)', text)
    if m:
        return None, int(m.group(1))

    # "username 37"
    parts = text.split()
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0].lstrip('@'), int(parts[1])

    # Plain username
    if parts and not text.startswith('http'):
        return parts[0].lstrip('@'), None

    return None, None


_CONNECT_PROMPT = (
    "Пришли *ссылку на свой блог* в клубе.\n\n"
    "Зайди на systemsworld.club → свой блог → скопируй URL.\n\n"
    "Пример: `https://systemsworld.club/c/blogs/username/37`"
)


# ── FSM States ─────────────────────────────────────────────

class ClubStates(StatesGroup):
    waiting_connect_input = State()   # URL, "username ID", or username
    waiting_blog_url = State()        # URL after username verified
    waiting_post_title = State()
    waiting_post_content = State()
    confirm_publish = State()


# ── /club command ──────────────────────────────────────────

@discourse_router.message(Command("club"))
async def cmd_club(message: Message, state: FSMContext):
    """Команда /club — подключение к клубу, публикация, статус."""
    from clients.discourse import discourse

    if not discourse:
        await message.answer("Интеграция с клубом не настроена (нет DISCOURSE_API_URL).")
        return

    telegram_user_id = message.chat.id
    intern = await get_intern(telegram_user_id)
    if not intern:
        await message.answer("Сначала пройди /start.")
        return

    text = message.text or ""
    parts = text.strip().split(maxsplit=2)
    subcommand = parts[1].lower() if len(parts) > 1 else None
    arg = parts[2] if len(parts) > 2 else None

    account = await get_discourse_account(telegram_user_id)

    # /club disconnect
    if subcommand == "disconnect":
        if account:
            await unlink_discourse_account(telegram_user_id)
            await message.answer("Аккаунт клуба отвязан.")
        else:
            await message.answer("Аккаунт клуба не привязан.")
        return

    # /club connect [URL | username | username ID]
    if subcommand == "connect":
        if arg:
            username, category_id = _parse_blog_input(arg)
            if username and category_id:
                # Full info — verify and save
                await _connect_full(message, username, category_id)
                return
            elif username:
                # Only username — verify, then ask for URL
                user = await discourse.get_user(username)
                if not user:
                    await message.answer(
                        f"Пользователь `{username}` не найден в клубе.",
                        parse_mode="Markdown",
                    )
                    return
                await state.update_data(discourse_username=username)
                await message.answer(
                    f"*{username}* найден.\n\n"
                    "Теперь пришли ссылку на свой блог в клубе.\n\n"
                    "Пример: `https://systemsworld.club/c/blogs/username/37`",
                    parse_mode="Markdown",
                )
                await state.set_state(ClubStates.waiting_blog_url)
                return

        # No arg or couldn't parse — ask for URL
        await message.answer(_CONNECT_PROMPT, parse_mode="Markdown")
        await state.set_state(ClubStates.waiting_connect_input)
        return

    # /club publish
    if subcommand == "publish":
        if not account:
            await message.answer(
                "Сначала подключи аккаунт клуба:\n`/club connect`",
                parse_mode="Markdown",
            )
            return
        await message.answer(
            "Введи *заголовок* поста для публикации в блог:",
            parse_mode="Markdown",
        )
        await state.set_state(ClubStates.waiting_post_title)
        return

    # /club posts
    if subcommand == "posts":
        if not account:
            await message.answer("Аккаунт клуба не привязан. /club connect")
            return
        posts = await get_published_posts(telegram_user_id)
        if not posts:
            await message.answer("Ещё нет опубликованных постов.")
            return
        lines = ["*Мои публикации:*\n"]
        for p in posts[:20]:
            url = f"https://systemsworld.club/t/{p['discourse_topic_id']}"
            lines.append(f"- [{p['title']}]({url})")
        await message.answer("\n".join(lines), parse_mode="Markdown")
        return

    # /club (без аргументов) — статус
    if account:
        username = account["discourse_username"]
        posts = await get_published_posts(telegram_user_id)
        cat_id = account.get("blog_category_id") or "?"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Опубликовать", callback_data="club_publish_start")],
            [
                InlineKeyboardButton(text="Мои публикации", callback_data="club_posts"),
                InlineKeyboardButton(text="Отвязать", callback_data="club_disconnect"),
            ],
        ])
        await message.answer(
            f"*Клуб подключён*\n\n"
            f"Username: `{username}`\n"
            f"Блог: категория {cat_id}\n"
            f"Публикаций: {len(posts)}",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Подключить аккаунт", callback_data="club_connect_start")],
        ])
        await message.answer(
            "*Подключение к systemsworld.club*\n\n"
            "Привяжи свой аккаунт, чтобы публиковать посты в личный блог клуба.\n\n"
            "Для подключения нужна ссылка на твой блог в клубе.",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )


# ── Connect flow ───────────────────────────────────────────

@discourse_router.message(ClubStates.waiting_connect_input)
async def on_connect_input(message: Message, state: FSMContext):
    """URL, 'username ID', or plain username."""
    from clients.discourse import discourse

    text = (message.text or "").strip()
    if not text or text.lower() in ("отмена", "cancel"):
        await state.clear()
        await message.answer("Подключение отменено.")
        return

    username, category_id = _parse_blog_input(text)

    if username and category_id:
        await state.clear()
        await _connect_full(message, username, category_id)
        return

    if username:
        # Verify username, ask for blog URL
        user = await discourse.get_user(username)
        if not user:
            await message.answer(
                f"Пользователь `{username}` не найден в клубе.",
                parse_mode="Markdown",
            )
            return
        await state.update_data(discourse_username=username)
        await message.answer(
            f"*{username}* найден.\n\n"
            "Теперь пришли ссылку на свой блог.\n\n"
            "Пример: `https://systemsworld.club/c/blogs/username/37`",
            parse_mode="Markdown",
        )
        await state.set_state(ClubStates.waiting_blog_url)
        return

    await message.answer(
        "Не удалось распознать.\n\n" + _CONNECT_PROMPT,
        parse_mode="Markdown",
    )


@discourse_router.message(ClubStates.waiting_blog_url)
async def on_blog_url_input(message: Message, state: FSMContext):
    """URL блога после того как username уже определён."""
    text = (message.text or "").strip()
    if not text or text.lower() in ("отмена", "cancel"):
        await state.clear()
        await message.answer("Подключение отменено.")
        return

    _, category_id = _parse_blog_input(text)

    # Принимаем и просто число
    if category_id is None and text.isdigit():
        category_id = int(text)

    if not category_id:
        await message.answer(
            "Не удалось определить категорию из ссылки.\n\n"
            "Пришли URL блога или просто номер категории.\n"
            "Пример: `https://systemsworld.club/c/blogs/username/37`",
            parse_mode="Markdown",
        )
        return

    data = await state.get_data()
    username = data.get("discourse_username")
    await state.clear()

    if not username:
        await message.answer("Данные потеряны. Начни заново: /club connect")
        return

    await _connect_full(message, username, category_id)


async def _connect_full(message: Message, username: str, category_id: int):
    """Verify username + category and save. Max 2 API calls."""
    from clients.discourse import discourse

    # 1. Verify username
    user = await discourse.get_user(username)
    if not user:
        await message.answer(
            f"Пользователь `{username}` не найден в клубе.\nПроверь написание.",
            parse_mode="Markdown",
        )
        return

    # 2. Verify category
    cat = await discourse.get_category(category_id)
    if not cat:
        await message.answer(
            f"Категория {category_id} не найдена в клубе. Проверь ссылку.",
        )
        return

    # 3. Save
    cat_slug = cat.get("slug", "")
    cat_name = cat.get("name", f"#{category_id}")
    await link_discourse_account(
        chat_id=message.chat.id,
        discourse_username=username,
        blog_category_id=category_id,
        blog_category_slug=cat_slug,
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Опубликовать", callback_data="club_publish_start")],
    ])

    await message.answer(
        f"Аккаунт подключён: `{username}`\n"
        f"Блог: *{cat_name}* (категория {category_id})",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# ── Publish flow ───────────────────────────────────────────

@discourse_router.message(ClubStates.waiting_post_title)
async def on_post_title(message: Message, state: FSMContext):
    """Получили заголовок — запрашиваем контент."""
    title = (message.text or "").strip()
    if not title:
        await message.answer("Введи заголовок.")
        return

    # Дедупликация
    already = await is_title_published(message.chat.id, title)
    if already:
        await message.answer(
            f"Пост с заголовком *{title}* уже опубликован.",
            parse_mode="Markdown",
        )
        await state.clear()
        return

    await state.update_data(post_title=title)
    await message.answer(
        "Теперь введи *текст* поста (Markdown).\n\n"
        "Или отправь `отмена` для отмены.",
        parse_mode="Markdown",
    )
    await state.set_state(ClubStates.waiting_post_content)


@discourse_router.message(ClubStates.waiting_post_content)
async def on_post_content(message: Message, state: FSMContext):
    """Получили контент — показываем превью и просим подтвердить."""
    text = (message.text or "").strip()
    if text.lower() in ("отмена", "cancel"):
        await state.clear()
        await message.answer("Публикация отменена.")
        return

    if not text:
        await message.answer("Введи текст поста.")
        return

    data = await state.get_data()
    title = data.get("post_title", "")

    await state.update_data(post_content=text)

    # Превью (первые 300 символов)
    preview = text[:300] + ("..." if len(text) > 300 else "")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Опубликовать", callback_data="club_publish_confirm"),
            InlineKeyboardButton(text="Отмена", callback_data="club_publish_cancel"),
        ]
    ])

    await message.answer(
        f"*Превью публикации:*\n\n"
        f"*{title}*\n\n"
        f"{preview}\n\n"
        f"---\n"
        f"Длина: {len(text)} символов",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    await state.set_state(ClubStates.confirm_publish)


@discourse_router.callback_query(lambda c: c.data == "club_publish_confirm")
async def on_publish_confirm(callback: CallbackQuery, state: FSMContext):
    """Подтверждение — публикуем. Используем cached category_id."""
    from clients.discourse import discourse

    await callback.answer()
    data = await state.get_data()
    title = data.get("post_title", "")
    content = data.get("post_content", "")
    await state.clear()

    if not title or not content:
        await callback.message.answer("Данные потеряны. Попробуй /club publish заново.")
        return

    account = await get_discourse_account(callback.from_user.id)
    if not account:
        await callback.message.answer("Аккаунт клуба не привязан.")
        return

    username = account["discourse_username"]
    category_id = account.get("blog_category_id")

    if not category_id:
        await callback.message.answer(
            "Блог не указан. Переподключись:\n"
            "/club disconnect → /club connect"
        )
        return

    logger.info(f"Publishing to category={category_id}, user={username}")

    try:
        result = await discourse.create_topic(
            category_id=category_id,
            title=title,
            raw=content,
            username=username,
        )
        topic_id = result.get("topic_id")
        post_id = result.get("id")
        topic_slug = result.get("topic_slug", "")

        # Сохранить в БД
        await save_published_post(
            chat_id=callback.from_user.id,
            discourse_topic_id=topic_id,
            discourse_post_id=post_id,
            title=title,
            category_id=category_id,
        )

        url = f"https://systemsworld.club/t/{topic_slug}/{topic_id}"
        await callback.message.answer(
            f"Опубликовано!\n\n{url}",
        )
    except Exception as e:
        logger.error(f"Discourse publish error: {e}")
        err_str = str(e)
        hint = ""
        if "403" in err_str or "not permitted" in err_str.lower():
            hint = (
                "\n\nВозможные причины:\n"
                "1. API-ключ должен быть типа «All Users» (Admin > API > Keys)\n"
                "2. Категория блога должна разрешать Create "
                "(Admin > Categories > blogs > Security)\n"
                "3. Попробуй /club disconnect → /club connect"
            )
        await callback.message.answer(
            f"Ошибка публикации: {e}\n"
            f"(category={category_id}, user={username}){hint}"
        )


@discourse_router.callback_query(lambda c: c.data == "club_publish_start")
async def on_club_publish_start(callback: CallbackQuery, state: FSMContext):
    """Начать публикацию из экрана подключений (/settings)."""
    from clients.discourse import discourse

    await callback.answer()

    if not discourse:
        await callback.message.answer("Интеграция с клубом не настроена.")
        return

    account = await get_discourse_account(callback.from_user.id)
    if not account:
        await callback.message.answer(
            "Аккаунт клуба не привязан.\n`/club connect`",
            parse_mode="Markdown",
        )
        return

    await callback.message.answer(
        "Введи *заголовок* поста для публикации в блог:",
        parse_mode="Markdown",
    )
    await state.set_state(ClubStates.waiting_post_title)


@discourse_router.callback_query(lambda c: c.data == "club_publish_cancel")
async def on_publish_cancel(callback: CallbackQuery, state: FSMContext):
    """Отмена публикации."""
    await callback.answer()
    await state.clear()
    await callback.message.answer("Публикация отменена.")


@discourse_router.callback_query(lambda c: c.data == "club_posts")
async def on_club_posts(callback: CallbackQuery):
    """Мои публикации (из кнопки)."""
    await callback.answer()
    account = await get_discourse_account(callback.from_user.id)
    if not account:
        await callback.message.answer("Аккаунт клуба не привязан.")
        return
    posts = await get_published_posts(callback.from_user.id)
    if not posts:
        await callback.message.answer("Ещё нет опубликованных постов.")
        return
    lines = ["*Мои публикации:*\n"]
    for p in posts[:20]:
        url = f"https://systemsworld.club/t/{p['discourse_topic_id']}"
        lines.append(f"- [{p['title']}]({url})")
    await callback.message.answer("\n".join(lines), parse_mode="Markdown")


@discourse_router.callback_query(lambda c: c.data == "club_disconnect")
async def on_club_disconnect(callback: CallbackQuery):
    """Отвязать аккаунт клуба (из кнопки)."""
    await callback.answer()
    account = await get_discourse_account(callback.from_user.id)
    if account:
        await unlink_discourse_account(callback.from_user.id)
        await callback.message.answer("Аккаунт клуба отвязан.")
    else:
        await callback.message.answer("Аккаунт клуба не привязан.")


@discourse_router.callback_query(lambda c: c.data == "club_connect_start")
async def on_club_connect_start(callback: CallbackQuery, state: FSMContext):
    """Начать подключение аккаунта (из кнопки)."""
    from clients.discourse import discourse

    await callback.answer()
    if not discourse:
        await callback.message.answer("Интеграция с клубом не настроена.")
        return
    await callback.message.answer(_CONNECT_PROMPT, parse_mode="Markdown")
    await state.set_state(ClubStates.waiting_connect_input)
