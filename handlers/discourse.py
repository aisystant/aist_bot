"""
Хендлеры интеграции с Discourse (systemsworld.club).

Команды:
- /club — подключение/статус/мои публикации
- /club connect <URL или username> — привязать аккаунт
- /club disconnect — отвязать
- /club publish — опубликовать пост (ручной ввод или из индекса)
- /club schedule — график публикаций
- /club posts — мои публикации
"""

import asyncio
import json
import re
import logging
from datetime import datetime, timedelta
from pathlib import Path

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
    get_upcoming_schedule,
    get_scheduled_count,
    get_scheduled_publication,
    cancel_scheduled_publication,
    reschedule_publication,
    schedule_publication,
    get_all_published_source_files,
    get_all_published_titles_lower,
    get_all_scheduled_source_files,
    get_all_scheduled_titles_lower,
    reschedule_all_pending,
    mark_publication_done,
)

logger = logging.getLogger(__name__)

discourse_router = Router(name="discourse")


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


# ── Helpers ───────────────────────────────────────────────


async def _revert_frontmatter_to_draft(chat_id: int, source_file: str) -> None:
    """Revert frontmatter status → draft, чтобы smart_publisher не пере-планировал.

    Вызывается при отмене запланированной публикации.
    Best-effort: ошибка не блокирует отмену.
    """
    try:
        from clients.github_content import create_content_client, update_frontmatter_field
        from clients.github_oauth import github_oauth

        token = await github_oauth.get_access_token(chat_id)
        repo = await github_oauth.get_knowledge_repo(chat_id)
        if not token or not repo:
            return
        client = create_content_client(token, repo)
        try:
            result = await client.read_file(source_file)
            if not result:
                return
            content, sha = result
            new_content = update_frontmatter_field(content, "status", "draft")
            await client.update_file(
                source_file, new_content, sha,
                f"Reverted to draft (cancelled from schedule): {source_file}",
            )
            logger.info(f"[Publisher] Frontmatter reverted to draft: {source_file}")
        finally:
            await client.close()
    except Exception as e:
        logger.warning(f"[Publisher] Frontmatter revert failed for {source_file}: {e}")


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
    "Пришли *URL страницы своего блога* в клубе — "
    "именно туда будут публиковаться ваши посты.\n\n"
    "Как найти:\n"
    "1. Зайди на [systemsworld.club](https://systemsworld.club)\n"
    "2. В левом меню открой свой блог "
    "(или через профиль → Activity → блог)\n"
    "3. Скопируй URL из адресной строки браузера\n\n"
    "Пример: `https://systemsworld.club/c/blogs/username/37`"
)


# ── FSM States ─────────────────────────────────────────────

class ClubStates(StatesGroup):
    waiting_connect_input = State()   # URL, "username ID", or username
    waiting_blog_url = State()        # URL after username verified
    waiting_post_title = State()
    waiting_post_content = State()
    confirm_publish = State()
    confirm_schedule_rebuild = State()  # подтверждение нового графика после manual publish


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
                if re.match(r'^blogs-user-\d+$', username):
                    await message.answer(
                        f"`{username}` — это slug категории, а не username.\n"
                        "Напиши свой username в клубе "
                        "(его можно найти в профиле на systemsworld.club).\n\n"
                        "Например: `tseren-tserenov`",
                        parse_mode="Markdown",
                    )
                    await state.set_state(ClubStates.waiting_connect_input)
                    return
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
                    "Теперь пришли URL страницы своего блога в клубе — "
                    "именно туда будут публиковаться ваши посты.\n\n"
                    "Открой свой блог на systemsworld.club и скопируй URL "
                    "из адресной строки.\n\n"
                    "Пример: `https://systemsworld.club/c/blogs/username/37`",
                    parse_mode="Markdown",
                )
                await state.set_state(ClubStates.waiting_blog_url)
                return

        # No arg or couldn't parse — ask for URL
        await message.answer(_CONNECT_PROMPT, parse_mode="Markdown")
        await state.set_state(ClubStates.waiting_connect_input)
        return

    # /club schedule
    if subcommand == "schedule":
        if not account:
            await message.answer("Аккаунт клуба не привязан. /club connect")
            return
        await _show_schedule(message, telegram_user_id)
        return

    # /club publish — умная публикация (из индекса или ручной ввод)
    if subcommand == "publish":
        if not account:
            await message.answer(
                "Сначала подключи аккаунт клуба:\n`/club connect`",
                parse_mode="Markdown",
            )
            return
        # Показать ready-посты из индекса как кнопки
        loading_msg = await message.answer("⏳ Сканирую индекс знаний…")
        try:
            await _show_publish_options(message, state, telegram_user_id, loading_msg)
        except Exception as e:
            logger.error(f"show_publish_options error: {e}")
            try:
                await loading_msg.edit_text(f"Ошибка загрузки постов: {e}")
            except Exception:
                await message.answer(f"Ошибка загрузки постов: {e}")
        return

    # /club reschedule — перераспределить pending посты по текущему каденсу
    if subcommand == "reschedule":
        if not account:
            await message.answer("Аккаунт клуба не привязан. /club connect")
            return
        from config.settings import PUBLISHER_DAYS, PUBLISHER_TIME
        day_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
        pub_days = [day_map[d.strip()] for d in PUBLISHER_DAYS.split(",") if d.strip() in day_map]
        if not pub_days:
            pub_days = list(range(7))
        hour, minute = 10, 0
        try:
            parts = PUBLISHER_TIME.split(":")
            hour, minute = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            pass
        result, dupes = await reschedule_all_pending(telegram_user_id, pub_days, hour, minute)
        if not result:
            await message.answer("Нет постов для перепланировки.")
            return
        lines = [f"Перепланировано {len(result)} постов (удалено дублей: {dupes}):\n"]
        for title, slot in result:
            lines.append(f"  • «{title}» — {(slot + timedelta(hours=3)).strftime('%a %d %b, %H:%M')}")  # UTC→MSK
        await message.answer("\n".join(lines))
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
        queue = await get_scheduled_count(telegram_user_id)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Опубликовать", callback_data="club_publish_start")],
            [
                InlineKeyboardButton(text=f"Расписание ({queue})", callback_data="club_schedule"),
                InlineKeyboardButton(text="Перепланировать", callback_data="club_reschedule"),
            ],
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
            "Привяжи аккаунт, чтобы публиковать посты "
            "в личный блог клуба.\n\n"
            "Пришли URL страницы своего блога — "
            "именно туда будут публиковаться ваши посты.\n\n"
            "Как найти: зайди на systemsworld.club → "
            "открой свой блог → скопируй URL из адресной строки.\n\n"
            "Пример: `https://systemsworld.club/c/blogs/username/37`",
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
        if re.match(r'^blogs-user-\d+$', username):
            await message.answer(
                f"`{username}` — это slug категории, а не username.\n"
                "Пришли ссылку на блог целиком или свой username в клубе.",
                parse_mode="Markdown",
            )
            return
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
            "Теперь пришли URL страницы своего блога в клубе — "
            "именно туда будут публиковаться ваши посты.\n\n"
            "Открой свой блог на systemsworld.club и скопируй URL "
            "из адресной строки.\n\n"
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


async def _resolve_username_from_category(discourse, category_id: int, slug: str) -> str | None:
    """Resolve real username from blogs-user-* slug via category name + user search.

    Strategy:
    1. Get category by ID → extract name (e.g. "Tseren Tserenov")
    2. Slugify name → guess username (e.g. "tseren-tserenov")
    3. Verify via get_user (always works, unlike search_users which may 403)
    4. Fallback: search_users if slugified guess didn't match
    """
    cat = await discourse.get_category(category_id)
    if not cat:
        logger.info(f"[resolve] get_category({category_id}) → None")
        return None
    cat_name = cat.get("name", "")
    logger.info(f"[resolve] cat_name='{cat_name}'")
    # Strip typical suffixes: "(блоги)", "(blogs)"
    clean_name = re.sub(r'\s*\((?:блоги|blogs)\)\s*$', '', cat_name).strip()
    if not clean_name:
        return None

    # Strategy 1: slugify name → verify via get_user (no special scope needed)
    guessed = re.sub(r'[^a-zA-Z0-9]+', '-', clean_name).strip('-').lower()
    if guessed:
        logger.info(f"[resolve] trying guessed username '{guessed}'")
        user = await discourse.get_user(guessed)
        if user:
            return guessed

    # Strategy 2: search_users (may 403 depending on API key scope)
    results = await discourse.search_users(clean_name)
    logger.info(f"[resolve] search_users('{clean_name}') → {len(results)} results")
    if results and len(results) == 1:
        return results[0].get("username")
    for u in results:
        if u.get("name", "").lower() == clean_name.lower():
            return u.get("username")
    return None


async def _connect_full(message: Message, username: str, category_id: int):
    """Verify username + category and save. Max 2 API calls."""
    from clients.discourse import discourse

    # 0. Resolve blogs-user-* slug → real username
    if re.match(r'^blogs-user-\d+$', username):
        resolved = await _resolve_username_from_category(discourse, category_id, username)
        if resolved:
            logger.info(f"Resolved slug '{username}' → username '{resolved}'")
            username = resolved
        else:
            await message.answer(
                f"Ссылка содержит slug категории `{username}`, а не username.\n"
                "Не удалось определить владельца блога автоматически.\n\n"
                "Напиши свой username в клубе (без угловых скобок).\n"
                "Его можно найти на systemsworld.club в профиле.\n\n"
                "Например: `/club connect tseren-tserenov`",
                parse_mode="Markdown",
            )
            return

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
        await callback.message.answer("Данные потеряны. Попробуй /club → Опубликовать.")
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
            "/club → Отвязать → /club → Подключить"
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
                "3. Попробуй /club → Отвязать → /club → Подключить"
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

    # Убираем кнопки из /club-меню → предотвращаем повторные нажатия во время скана
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

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

    # Smart publish: показать ready-посты из индекса
    loading_msg = await callback.message.answer("⏳ Сканирую индекс знаний…")
    try:
        await _show_publish_options(callback.message, state, callback.from_user.id, loading_msg)
    except Exception as e:
        logger.error(f"show_publish_options error: {e}")
        try:
            await loading_msg.edit_text(f"Ошибка загрузки постов: {e}")
        except Exception:
            await callback.message.answer(f"Ошибка загрузки постов: {e}")


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


# ── Schedule ──────────────────────────────────────────────


async def _show_schedule(message_or_cb, chat_id: int):
    """Показать ближайшие запланированные публикации."""
    schedule = await get_upcoming_schedule(chat_id, limit=10)
    queue = await get_scheduled_count(chat_id)

    if not schedule:
        text = "График публикаций пуст.\n\nДобавь посты: /club → Опубликовать"
        if hasattr(message_or_cb, "answer"):
            await message_or_cb.answer(text, parse_mode="Markdown")
        else:
            await message_or_cb.message.answer(text, parse_mode="Markdown")
        return

    lines = [f"*График публикаций* ({queue} в очереди):\n"]
    buttons = []
    for i, pub in enumerate(schedule, 1):
        t = pub["schedule_time"] + timedelta(hours=3)  # UTC→MSK
        time_str = t.strftime("%a %d %b, %H:%M") if hasattr(t, "strftime") else str(t)
        lines.append(f"{i}. «{pub['title']}» — {time_str}")
        buttons.append([
            InlineKeyboardButton(
                text=f"🚀 {pub['title'][:25]}",
                callback_data=f"club_sched_pub_now:{pub['id']}",
            ),
            InlineKeyboardButton(
                text=f"❌",
                callback_data=f"club_sched_cancel:{pub['id']}",
            ),
        ])

    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="club_main")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    text = "\n".join(lines)
    if hasattr(message_or_cb, "answer"):
        await message_or_cb.answer(text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await message_or_cb.message.answer(text, parse_mode="Markdown", reply_markup=keyboard)


@discourse_router.callback_query(lambda c: c.data == "club_schedule")
async def on_club_schedule(callback: CallbackQuery):
    """Показать график (из кнопки)."""
    await callback.answer()
    try:
        await _show_schedule(callback.message, callback.from_user.id)
    except Exception as e:
        logger.error(f"show_schedule error: {e}")
        await callback.message.answer(f"Ошибка загрузки графика: {e}")


@discourse_router.callback_query(lambda c: c.data and c.data.startswith("club_sched_cancel:"))
async def on_schedule_cancel_item(callback: CallbackQuery):
    """Отменить одну запланированную публикацию + revert frontmatter → draft."""
    await callback.answer()
    pub_id = int(callback.data.split(":")[1])
    # Получить данные до удаления (нужен source_file)
    pub = await get_scheduled_publication(pub_id)
    await cancel_scheduled_publication(pub_id)

    # Revert frontmatter: status → draft (чтобы smart_publisher не пере-планировал)
    if pub and pub.get("source_file"):
        await _revert_frontmatter_to_draft(pub["chat_id"], pub["source_file"])

    await callback.message.answer("Публикация удалена из графика.")
    await _show_schedule(callback.message, callback.from_user.id)


@discourse_router.callback_query(lambda c: c.data and c.data.startswith("club_sched_pub_now:"))
async def on_schedule_publish_now(callback: CallbackQuery):
    """Опубликовать запланированный пост прямо сейчас + перепланировать остальные."""
    from clients.discourse import discourse

    await callback.answer()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if not discourse:
        await callback.message.answer("Интеграция с клубом не настроена.")
        return

    pub_id = int(callback.data.split(":")[1])
    pub = await get_scheduled_publication(pub_id)
    if not pub:
        await callback.message.answer("Публикация не найдена или уже опубликована.")
        return

    chat_id = callback.from_user.id

    # Публикуем в Discourse
    await callback.message.answer(f"⏳ Публикую «{pub['title']}»...")
    try:
        raw = pub["raw"]
        source_file = pub.get("source_file")

        # Единый GitHub-клиент для cover + frontmatter (S48 refactor)
        if source_file:
            try:
                from clients.github_content import create_content_client, update_frontmatter_field
                from clients.github_oauth import github_oauth
                token = await github_oauth.get_access_token(chat_id)
                knowledge_repo = await github_oauth.get_knowledge_repo(chat_id)
                if token and knowledge_repo:
                    gh_client = create_content_client(token, knowledge_repo)
                    try:
                        # Cover (S48)
                        cover_path = str(Path(source_file).parent / "cover.png")
                        cover_bytes = await gh_client.read_binary_file(cover_path)
                        if cover_bytes:
                            cover_md = await discourse.upload_image(
                                "cover.png", cover_bytes, pub["discourse_username"]
                            )
                            if cover_md:
                                raw = f"{cover_md}\n\n{raw}"
                    except Exception as cover_err:
                        logger.warning(f"Cover image skip (scheduled): {cover_err}")
            except Exception as gh_err:
                logger.warning(f"GitHub client init failed: {gh_err}")
                gh_client = None
        else:
            gh_client = None

        result = await discourse.create_topic(
            category_id=pub["category_id"],
            title=pub["title"],
            raw=raw,
            username=pub["discourse_username"],
        )
        topic_id = result.get("topic_id")
        post_id = result.get("id")
        slug = result.get("topic_slug", "")

        # Обновить статус в БД
        await mark_publication_done(pub_id, topic_id)
        await save_published_post(
            chat_id=chat_id,
            discourse_topic_id=topic_id,
            discourse_post_id=post_id,
            title=pub["title"],
            category_id=pub["category_id"],
            source_file=source_file,
        )

        # Обновить frontmatter → published (тот же gh_client)
        if source_file and gh_client:
            try:
                file_result = await gh_client.read_file(source_file)
                if file_result:
                    content, sha = file_result
                    new_content = update_frontmatter_field(content, "status", "published")
                    await gh_client.update_file(
                        source_file, new_content, sha,
                        f"Published to club: {pub['title']}"
                    )
            except Exception as fm_err:
                logger.warning(f"Frontmatter update failed: {fm_err}")

        # Закрыть gh_client после всех операций
        if gh_client:
            try:
                await gh_client.close()
            except Exception:
                pass

        url = f"https://systemsworld.club/t/{slug}/{topic_id}"

        # Перепланировать оставшиеся
        from config.settings import PUBLISHER_DAYS, PUBLISHER_TIME
        day_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
        pub_days = [day_map[d.strip()] for d in PUBLISHER_DAYS.split(",") if d.strip() in day_map]
        if not pub_days:
            pub_days = list(range(7))
        hour, minute = 10, 0
        try:
            parts = PUBLISHER_TIME.split(":")
            hour, minute = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            pass
        reschedule_result, dupes = await reschedule_all_pending(chat_id, pub_days, hour, minute)

        queue = await get_scheduled_count(chat_id)
        lines = [
            f"✅ Опубликовано: «{pub['title']}»",
            url,
            f"В очереди: {queue}",
        ]
        if reschedule_result:
            lines.append(f"\n📅 Перепланировано {len(reschedule_result)} постов:")
            for title, slot in reschedule_result[:5]:
                lines.append(f"  • «{title}» — {(slot + timedelta(hours=3)).strftime('%a %d %b, %H:%M')}")  # UTC→MSK
            if len(reschedule_result) > 5:
                lines.append(f"  ... и ещё {len(reschedule_result) - 5}")

        await callback.message.answer("\n".join(lines))
    except Exception as e:
        logger.error(f"Publish now error: {e}")
        await callback.message.answer(f"Ошибка публикации: {e}")


@discourse_router.callback_query(lambda c: c.data == "club_reschedule")
async def on_club_reschedule(callback: CallbackQuery):
    """Перепланировать все pending посты по текущему каденсу (из кнопки)."""
    await callback.answer()
    from config.settings import PUBLISHER_DAYS, PUBLISHER_TIME
    day_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    pub_days = [day_map[d.strip()] for d in PUBLISHER_DAYS.split(",") if d.strip() in day_map]
    if not pub_days:
        pub_days = list(range(7))
    hour, minute = 10, 0
    try:
        parts = PUBLISHER_TIME.split(":")
        hour, minute = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        pass
    result, dupes = await reschedule_all_pending(callback.from_user.id, pub_days, hour, minute)
    if not result:
        await callback.message.answer("Нет постов для перепланировки.")
        return
    lines = [f"Перепланировано {len(result)} постов (удалено дублей: {dupes}):\n"]
    for title, slot in result:
        lines.append(f"  • «{title}» — {(slot + timedelta(hours=3)).strftime('%a %d %b, %H:%M')}")  # UTC→MSK
    await callback.message.answer("\n".join(lines))


@discourse_router.callback_query(lambda c: c.data == "club_main")
async def on_club_main(callback: CallbackQuery):
    """Вернуться в главное меню /club."""
    await callback.answer()
    # Повторим статус-экран
    account = await get_discourse_account(callback.from_user.id)
    if not account:
        await callback.message.answer("Аккаунт клуба не привязан. /club connect")
        return
    username = account["discourse_username"]
    posts = await get_published_posts(callback.from_user.id)
    queue = await get_scheduled_count(callback.from_user.id)
    cat_id = account.get("blog_category_id") or "?"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Опубликовать", callback_data="club_publish_start")],
        [
            InlineKeyboardButton(text=f"Расписание ({queue})", callback_data="club_schedule"),
            InlineKeyboardButton(text="Перепланировать", callback_data="club_reschedule"),
        ],
        [
            InlineKeyboardButton(text="Мои публикации", callback_data="club_posts"),
            InlineKeyboardButton(text="Отвязать", callback_data="club_disconnect"),
        ],
    ])
    await callback.message.answer(
        f"*Клуб подключён*\n\n"
        f"Username: `{username}`\n"
        f"Блог: категория {cat_id}\n"
        f"Публикаций: {len(posts)}",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# ── Smart Publish (из индекса) ────────────────────────────


async def _scan_ready_posts(chat_id: int) -> list[dict]:
    """Сканировать индекс знаний → вернуть ready+club посты, не в published/scheduled."""
    from clients.github_content import create_content_client, parse_frontmatter
    from clients.github_oauth import github_oauth

    token = await github_oauth.get_access_token(chat_id)
    knowledge_repo = await github_oauth.get_knowledge_repo(chat_id)
    if not token or not knowledge_repo:
        return []

    client = create_content_client(token, knowledge_repo)
    try:
        published_files = await get_all_published_source_files(chat_id)
        published_titles = await get_all_published_titles_lower(chat_id)
        scheduled_files = await get_all_scheduled_source_files(chat_id)
        scheduled_titles = await get_all_scheduled_titles_lower(chat_id)

        today = datetime.now().date()
        cutoff = today - timedelta(days=14)
        current_year = today.year
        candidates = []

        # Семафор: макс 10 параллельных запросов к GitHub API
        sem = asyncio.Semaphore(10)

        def _is_recent(filename: str) -> bool:
            """Проверить дату в имени файла (YYYY-MM-DD-*). Файлы за последние 14 дней."""
            try:
                parts = filename.split("-", 3)
                file_date = datetime(int(parts[0]), int(parts[1]), int(parts[2])).date()
                return file_date >= cutoff
            except (ValueError, IndexError):
                return True  # Не удалось распарсить → читаем на всякий случай

        async def _read_and_check(file_info: dict) -> dict | None:
            """Прочитать файл и проверить frontmatter. Возвращает кандидата или None."""
            async with sem:
                result = await client.read_file(file_info["path"])
            if not result:
                return None
            content, sha = result
            # Ранний выход: если файл не начинается с ---, frontmatter нет
            if not content.startswith("---"):
                return None
            fm = parse_frontmatter(content)
            if fm.get("type") != "post":
                return None
            if fm.get("status") != "ready":
                return None
            if fm.get("target") != "club":
                return None
            title = fm.get("title", file_info["name"])
            if file_info["path"] in published_files:
                return None
            if title.lower() in published_titles:
                return None
            if file_info["path"] in scheduled_files:
                return None
            if title.lower() in scheduled_titles:
                return None
            return {
                "path": file_info["path"],
                "sha": sha,
                "title": title,
                "tags": fm.get("tags", []),
                "content": content,
            }

        files = await client.list_files(f"docs/{current_year}")
        files = [f for f in files if f["name"] != "README.md" and _is_recent(f["name"])]

        if files:
            # Параллельное чтение файлов
            results = await asyncio.gather(
                *[_read_and_check(f) for f in files],
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, dict):
                    candidates.append(r)

        return candidates
    except Exception as e:
        logger.error(f"Scan ready posts error: {e}")
        return []
    finally:
        await client.close()


async def _show_publish_options(
    message: Message, state: FSMContext, chat_id: int, loading_msg: Message | None = None,
):
    """Показать ready-посты из индекса как кнопки + вариант ручного ввода."""
    candidates = await _scan_ready_posts(chat_id)

    buttons = []
    if candidates:
        # Сохраняем кандидатов в FSM для callback
        posts_data = [
            {"path": c["path"], "title": c["title"], "tags": c["tags"]}
            for c in candidates[:8]  # Макс 8 кнопок
        ]
        await state.update_data(ready_posts=posts_data)

        for i, c in enumerate(candidates[:8]):
            buttons.append([InlineKeyboardButton(
                text=f"📄 {c['title'][:45]}",
                callback_data=f"club_smart_pub:{i}",
            )])

    buttons.append([InlineKeyboardButton(text="✍️ Ввести вручную", callback_data="club_publish_manual")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="club_main")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    if candidates:
        text = f"*Готовые к публикации* ({len(candidates)}):\n\nВыбери пост для мгновенной публикации или введи вручную:"
    else:
        text = (
            "*Публикация в клуб*\n\n"
            "Готовых постов в индексе нет.\n\n"
            "Что можно сделать:\n"
            "• *Ввести вручную* — заголовок и текст прямо здесь\n"
            "• Пометить пост в индексе: `status: ready`, `target: club`\n"
            "• Опубликовать из расписания: /club → Расписание → 🚀"
        )

    # Заменяем loading-сообщение результатом, или шлём новое
    if loading_msg:
        try:
            await loading_msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
            return
        except Exception:
            # Fallback: если edit не удался, удалим loading и отправим новое
            try:
                await loading_msg.delete()
            except Exception:
                pass
    await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)


@discourse_router.callback_query(lambda c: c.data == "club_publish_manual")
async def on_publish_manual(callback: CallbackQuery, state: FSMContext):
    """Ручной ввод поста (как раньше)."""
    await callback.answer()

    # Убираем кнопки из списка постов → предотвращаем race с _show_publish_options
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await state.set_state(ClubStates.waiting_post_title)
    await callback.message.answer(
        "Введи *заголовок* поста для публикации в блог:",
        parse_mode="Markdown",
    )


@discourse_router.callback_query(lambda c: c.data and c.data.startswith("club_smart_pub:"))
async def on_smart_publish_select(callback: CallbackQuery, state: FSMContext):
    """Пользователь выбрал пост из индекса → опубликовать сейчас + перестроить график."""
    from clients.discourse import discourse
    from clients.github_content import create_content_client, strip_frontmatter, update_frontmatter_field
    from clients.github_oauth import github_oauth

    await callback.answer()

    # Убираем кнопки из списка постов → предотвращаем повторные нажатия
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    idx = int(callback.data.split(":")[1])
    data = await state.get_data()
    ready_posts = data.get("ready_posts", [])

    if idx >= len(ready_posts):
        await callback.message.answer("Данные устарели. Попробуй /club → Опубликовать.")
        return

    post = ready_posts[idx]
    account = await get_discourse_account(callback.from_user.id)
    if not account:
        await callback.message.answer("Аккаунт клуба не привязан.")
        return

    category_id = account.get("blog_category_id")
    username = account["discourse_username"]
    if not category_id:
        await callback.message.answer("Блог не указан. /club → Отвязать → /club → Подключить")
        return

    # Прочитать контент из GitHub (per-user OAuth)
    token = await github_oauth.get_access_token(callback.from_user.id)
    knowledge_repo = await github_oauth.get_knowledge_repo(callback.from_user.id)
    if not token or not knowledge_repo:
        await callback.message.answer("GitHub не настроен. Настройки → GitHub → Выбрать индекс знаний.")
        return

    client = create_content_client(token, knowledge_repo)
    try:
        file_result = await client.read_file(post["path"])
        if not file_result:
            await callback.message.answer(f"Не удалось прочитать {post['path']}.")
            return

        content, sha = file_result
        raw = strip_frontmatter(content)

        # Загрузить cover.png если есть (S48)
        cover_path = str(Path(post["path"]).parent / "cover.png")
        try:
            cover_bytes = await client.read_binary_file(cover_path)
            if cover_bytes:
                cover_md = await discourse.upload_image(
                    "cover.png", cover_bytes, username
                )
                if cover_md:
                    raw = f"{cover_md}\n\n{raw}"
                    logger.info(f"Cover image prepended to post")
        except Exception as cover_err:
            logger.warning(f"Cover image skip: {cover_err}")

        # Публикуем
        result = await discourse.create_topic(
            category_id=category_id,
            title=post["title"],
            raw=raw,
            username=username,
        )
        topic_id = result.get("topic_id")
        post_id = result.get("id")
        slug = result.get("topic_slug", "")

        # Сохранить в БД
        await save_published_post(
            chat_id=callback.from_user.id,
            discourse_topic_id=topic_id,
            discourse_post_id=post_id,
            title=post["title"],
            category_id=category_id,
            source_file=post["path"],
        )

        # Обновить frontmatter → published
        try:
            new_content = update_frontmatter_field(content, "status", "published")
            await client.update_file(
                post["path"], new_content, sha,
                f"Published to club: {post['title']}"
            )
        except Exception as fm_err:
            logger.warning(f"Frontmatter update failed: {fm_err}")

        url = f"https://systemsworld.club/t/{slug}/{topic_id}"

        # Перестроить график: сдвинуть scheduled posts
        rebuild_msg = await _rebuild_schedule_after_manual(callback.from_user.id)

        queue = await get_scheduled_count(callback.from_user.id)
        await callback.message.answer(
            f"✅ Опубликовано: «{post['title']}»\n"
            f"{url}\n"
            f"В очереди: {queue}"
            f"{rebuild_msg}",
        )
    except Exception as e:
        logger.error(f"Smart publish error: {e}")
        await callback.message.answer(f"Ошибка публикации: {e}")
    finally:
        await client.close()

    await state.clear()


async def _rebuild_schedule_after_manual(chat_id: int) -> str:
    """Перестроить график после ручной публикации.

    Логика: если ближайший scheduled post был на сегодня — он уже «заменён» ручной публикацией.
    Сдвигаем все pending на -1 слот (каждый берёт slot предыдущего).
    Возвращает текст для уведомления.
    """
    import pytz
    from config.settings import PUBLISHER_DAYS, PUBLISHER_TIME

    schedule = await get_upcoming_schedule(chat_id, limit=20)
    if not schedule:
        return ""

    msk = pytz.timezone("Europe/Moscow")
    now_msk = datetime.now(msk)
    today = now_msk.date()

    # Проверяем: если ближайший в графике — сегодня, отменяем его (заменён ручной публикацией)
    first = schedule[0]
    first_time = first["schedule_time"]
    if hasattr(first_time, "date") and first_time.date() == today:
        # Revert frontmatter перед удалением
        pub = await get_scheduled_publication(first["id"])
        if pub and pub.get("source_file"):
            await _revert_frontmatter_to_draft(chat_id, pub["source_file"])
        await cancel_scheduled_publication(first["id"])
        schedule = schedule[1:]

    if not schedule:
        return "\n\n📅 График: пуст (ближайший пост заменён ручной публикацией)."

    # Показать оставшийся график
    lines = ["\n\n📅 Обновлённый график:"]
    for pub in schedule[:5]:
        t = pub["schedule_time"] + timedelta(hours=3)  # UTC→MSK
        time_str = t.strftime("%a %d %b, %H:%M") if hasattr(t, "strftime") else str(t)
        lines.append(f"  • «{pub['title']}» — {time_str}")
    if len(schedule) > 5:
        lines.append(f"  ... и ещё {len(schedule) - 5}")

    return "\n".join(lines)
