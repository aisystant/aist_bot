"""
Хендлеры интеграции с Discourse (systemsworld.club).

Команды:
- /club — подключение/статус/мои публикации
- /club connect <username> — привязать аккаунт
- /club disconnect — отвязать
- /club publish — опубликовать пост
- /club posts — мои публикации
"""

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


# ── FSM States ─────────────────────────────────────────────

class ClubStates(StatesGroup):
    waiting_username = State()
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

    # /club connect <username>
    if subcommand == "connect":
        if arg:
            await _connect_account(message, state, arg)
        else:
            await message.answer(
                "Введи свой *username* в клубе:\n\n"
                "`/club connect username`\n\n"
                "Username можно найти в настройках профиля клуба, рядом с фото.\n"
                "Или просто напиши username — я жду.",
                parse_mode="Markdown",
            )
            await state.set_state(ClubStates.waiting_username)
        return

    # /club publish
    if subcommand == "publish":
        if not account:
            await message.answer(
                "Сначала подключи аккаунт клуба:\n`/club connect username`",
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
            await message.answer("Аккаунт клуба не привязан. /club connect username")
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
            "Username — твоё имя в клубе.\n"
            "Найти его можно в настройках профиля клуба, рядом с фото.",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )


# ── Connect flow ───────────────────────────────────────────

@discourse_router.message(ClubStates.waiting_username)
async def on_username_input(message: Message, state: FSMContext):
    """Получили username — проверяем и подключаем."""
    username = (message.text or "").strip().lstrip("@")
    if not username:
        await message.answer("Введи username.")
        return
    await _connect_account(message, state, username)


async def _connect_account(message: Message, state: FSMContext, username: str):
    """Проверить username в Discourse и привязать."""
    from clients.discourse import discourse

    await state.clear()

    # 1. Проверить пользователя
    user = await discourse.get_user(username)
    if not user:
        await message.answer(
            f"Пользователь `{username}` не найден в клубе.\n"
            "Проверь правильность написания.",
            parse_mode="Markdown",
        )
        return

    # 2. Найти подкатегорию блога
    blog = await discourse.find_user_blog(username)
    blog_cat_id = blog.get("id") if blog else None
    blog_slug = blog.get("slug") if blog else None

    # 3. Сохранить
    await link_discourse_account(
        chat_id=message.chat.id,
        discourse_username=username,
        blog_category_id=blog_cat_id,
        blog_category_slug=blog_slug,
    )

    blog_info = f"\nБлог найден: категория {blog_cat_id}" if blog_cat_id else "\nБлог не найден (публикация в общий раздел)."

    await message.answer(
        f"Аккаунт подключён: `{username}`{blog_info}\n\n"
        f"Теперь можешь публиковать: /club publish",
        parse_mode="Markdown",
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
    """Подтверждение — публикуем."""
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
    cached_cat_id = account.get("blog_category_id")

    # Всегда свежий поиск блога (кеш мог устареть)
    blog = await discourse.find_user_blog(username)
    if blog and blog.get("id"):
        category_id = blog["id"]
        if category_id != cached_cat_id:
            logger.info(f"Blog category updated: {cached_cat_id} → {category_id}")
            await link_discourse_account(
                chat_id=callback.from_user.id,
                discourse_username=username,
                blog_category_id=category_id,
                blog_category_slug=blog.get("slug"),
            )
    elif cached_cat_id:
        category_id = cached_cat_id
        logger.warning(f"Fresh discovery failed, using cached category_id={cached_cat_id}")
    else:
        await callback.message.answer(
            "Блог не найден в клубе. Публикация невозможна.\n\n"
            "Убедись, что у тебя есть персональный блог на systemsworld.club "
            "и попробуй переподключить: /club disconnect → /club connect username"
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
        if "403" in err_str or "not permitted" in err_str.lower() or "не разрешено" in err_str.lower():
            hint = (
                "\n\nВозможные причины:\n"
                "1. API-ключ должен быть типа «All Users» (Admin > API > Keys)\n"
                "2. Категория блога должна разрешать Create "
                "(Admin > Categories > blogs > Security)\n"
                "3. Попробуй /club disconnect → /club connect username"
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
            "Аккаунт клуба не привязан.\n`/club connect username`",
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
    await callback.message.answer(
        "Введи свой *username* в клубе:\n\n"
        "Username можно найти в настройках профиля клуба, рядом с фото.",
        parse_mode="Markdown",
    )
    await state.set_state(ClubStates.waiting_username)
