"""
Хендлеры стратега — команды для просмотра планов и отчётов.

/rp     — текущий WeekPlan (рабочие продукты на неделю)
/plan   — DayPlan на сегодня
/report — последний WeekReport
"""

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config import get_logger

logger = get_logger(__name__)

strategist_router = Router(name="strategist")

MAX_MESSAGE_LEN = 4000  # Telegram limit 4096, оставляем запас


def _truncate(text: str, max_len: int = MAX_MESSAGE_LEN) -> str:
    """Обрезает текст до лимита Telegram."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "\n\n... (обрезано)"


async def _check_strategy_ready_msg(message: Message) -> tuple[bool, int]:
    """Проверяет подключение GitHub и наличие strategy_repo (для Message)."""
    from clients.github_oauth import github_oauth

    telegram_user_id = message.chat.id

    if not await github_oauth.is_connected(telegram_user_id):
        await message.answer("GitHub не подключён. Подключите через /github")
        return False, telegram_user_id

    strategy_repo = await github_oauth.get_strategy_repo(telegram_user_id)
    if not strategy_repo:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="Выбрать репо стратега",
                    callback_data="strategy_select_repo",
                )]
            ]
        )
        await message.answer("Репозиторий стратега не выбран.", reply_markup=keyboard)
        return False, telegram_user_id

    return True, telegram_user_id


async def _check_strategy_ready_cb(callback: CallbackQuery) -> tuple[bool, int]:
    """Проверяет подключение GitHub и наличие strategy_repo (для Callback)."""
    from clients.github_oauth import github_oauth

    telegram_user_id = callback.from_user.id

    if not await github_oauth.is_connected(telegram_user_id):
        await callback.answer("GitHub не подключён", show_alert=True)
        return False, telegram_user_id

    strategy_repo = await github_oauth.get_strategy_repo(telegram_user_id)
    if not strategy_repo:
        await callback.answer("Репо стратега не выбран", show_alert=True)
        return False, telegram_user_id

    return True, telegram_user_id


@strategist_router.message(F.text == "/rp")
async def cmd_rp(message: Message):
    """Показывает текущий WeekPlan."""
    ready, user_id = await _check_strategy_ready_msg(message)
    if not ready:
        return

    from clients.github_strategy import github_strategy

    content = await github_strategy.get_week_plan(user_id)
    if not content:
        await message.answer("WeekPlan не найден.")
        return

    repo_url = await github_strategy.get_strategy_repo_url(user_id)
    text = _truncate(content)
    if repo_url:
        text += f"\n\n[Открыть в GitHub]({repo_url}/tree/main/current)"

    await message.answer(text, parse_mode="Markdown", disable_web_page_preview=True)


@strategist_router.message(F.text == "/plan")
async def cmd_plan(message: Message):
    """Показывает DayPlan на сегодня."""
    ready, user_id = await _check_strategy_ready_msg(message)
    if not ready:
        return

    from clients.github_strategy import github_strategy

    content = await github_strategy.get_day_plan(user_id)
    if not content:
        await message.answer("DayPlan на сегодня не найден.")
        return

    repo_url = await github_strategy.get_strategy_repo_url(user_id)
    text = _truncate(content)
    if repo_url:
        text += f"\n\n[Открыть в GitHub]({repo_url}/tree/main/current)"

    await message.answer(text, parse_mode="Markdown", disable_web_page_preview=True)


@strategist_router.message(F.text == "/report")
async def cmd_report(message: Message):
    """Показывает последний WeekReport."""
    ready, user_id = await _check_strategy_ready_msg(message)
    if not ready:
        return

    from clients.github_strategy import github_strategy

    content = await github_strategy.get_week_report(user_id)
    if not content:
        await message.answer("WeekReport не найден.")
        return

    repo_url = await github_strategy.get_strategy_repo_url(user_id)
    text = _truncate(content)
    if repo_url:
        text += f"\n\n[Открыть в GitHub]({repo_url}/tree/main/current)"

    await message.answer(text, parse_mode="Markdown", disable_web_page_preview=True)


# --- Callback-кнопки из уведомлений стратега ---

@strategist_router.callback_query(F.data == "strat_plan")
async def callback_strat_plan(callback: CallbackQuery):
    """Кнопка: показать DayPlan."""
    ready, user_id = await _check_strategy_ready_cb(callback)
    if not ready:
        return

    await callback.answer()

    from clients.github_strategy import github_strategy

    content = await github_strategy.get_day_plan(user_id)
    if not content:
        await callback.message.answer("DayPlan на сегодня не найден.")
        return

    await callback.message.answer(
        _truncate(content),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


@strategist_router.callback_query(F.data == "strat_rp")
async def callback_strat_rp(callback: CallbackQuery):
    """Кнопка: показать WeekPlan."""
    ready, user_id = await _check_strategy_ready_cb(callback)
    if not ready:
        return

    await callback.answer()

    from clients.github_strategy import github_strategy

    content = await github_strategy.get_week_plan(user_id)
    if not content:
        await callback.message.answer("WeekPlan не найден.")
        return

    await callback.message.answer(
        _truncate(content),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


@strategist_router.callback_query(F.data == "strat_report")
async def callback_strat_report(callback: CallbackQuery):
    """Кнопка: показать WeekReport."""
    ready, user_id = await _check_strategy_ready_cb(callback)
    if not ready:
        return

    await callback.answer()

    from clients.github_strategy import github_strategy

    content = await github_strategy.get_week_report(user_id)
    if not content:
        await callback.message.answer("WeekReport не найден.")
        return

    await callback.message.answer(
        _truncate(content),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


@strategist_router.callback_query(F.data == "strat_day_close")
async def callback_strat_day_close(callback: CallbackQuery):
    """Кнопка: закрыть день (пока показывает инструкцию)."""
    await callback.answer()
    await callback.message.answer(
        "Закрытие дня запускается локально:\n\n"
        "`~/Github/DS-strategist-agent/scripts/strategist.sh day-close`\n\n"
        "После завершения вы получите уведомление.",
        parse_mode="Markdown",
    )


# --- Выбор strategy_repo ---

@strategist_router.callback_query(F.data == "strategy_select_repo")
async def callback_strategy_select_repo(callback: CallbackQuery):
    """Показывает список репозиториев для выбора strategy_repo."""
    from clients.github_oauth import github_oauth

    telegram_user_id = callback.from_user.id

    if not await github_oauth.is_connected(telegram_user_id):
        await callback.answer("GitHub не подключён", show_alert=True)
        return

    await callback.answer()

    repos = await github_oauth.get_repos(telegram_user_id, limit=20)
    if not repos:
        await callback.message.answer("Не удалось получить список репозиториев.")
        return

    buttons = []
    for repo in repos:
        full_name = repo["full_name"]
        name = repo["name"]
        buttons.append([
            InlineKeyboardButton(
                text=name,
                callback_data=f"strategy_repo:{full_name}",
            )
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.answer(
        "Выберите репозиторий стратега:",
        reply_markup=keyboard,
    )


@strategist_router.callback_query(F.data.startswith("strategy_repo:"))
async def callback_strategy_repo_selected(callback: CallbackQuery):
    """Обработка выбора strategy_repo."""
    from clients.github_oauth import github_oauth

    telegram_user_id = callback.from_user.id
    repo_full_name = callback.data.split(":", 1)[1]

    await github_oauth.set_strategy_repo(telegram_user_id, repo_full_name)

    await callback.answer("Репозиторий стратега выбран!", show_alert=True)

    await callback.message.edit_text(
        f"*Репозиторий стратега настроен!*\n\n"
        f"Репо: `{repo_full_name}`\n\n"
        f"Доступные команды:\n"
        f"/rp — WeekPlan (план недели)\n"
        f"/plan — DayPlan (план дня)\n"
        f"/report — WeekReport (отчёт недели)",
        parse_mode="Markdown",
    )
