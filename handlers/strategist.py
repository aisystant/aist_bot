"""
Хендлеры стратега — команды для просмотра планов и отчётов.

/rp     — текущий WeekPlan (рабочие продукты на неделю)
/plan   — DayPlan на сегодня
/report — последний WeekReport
"""

from datetime import datetime, timezone, timedelta

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config import get_logger

MOSCOW_TZ = timezone(timedelta(hours=3))
from db.queries import get_intern
from helpers.telegram_format import format_strategy_content
from helpers.message_split import truncate_safe
from i18n import t

logger = get_logger(__name__)

strategist_router = Router(name="strategist")

# Навигация между планами (для strat_* callbacks из уведомлений)
_STRAT_NAV = {
    "strat_plan": [
        ("plans.week_plan", "strat_rp"),
        ("plans.week_report", "strat_report"),
    ],
    "strat_rp": [
        ("plans.day_plan", "strat_plan"),
        ("plans.week_report", "strat_report"),
    ],
    "strat_report": [
        ("plans.day_plan", "strat_plan"),
        ("plans.week_plan", "strat_rp"),
    ],
}


def _nav_keyboard(current: str, lang: str) -> InlineKeyboardMarkup:
    """Кнопки навигации: два других плана."""
    buttons = []
    for label_key, cb_data in _STRAT_NAV.get(current, []):
        buttons.append([InlineKeyboardButton(text=t(label_key, lang), callback_data=cb_data)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


def _truncate(text: str, lang: str = 'ru') -> str:
    return truncate_safe(text, suffix=f"\n\n{t('strategist.truncated', lang)}")


def _format(content: str, lang: str = 'ru', repo_url: str = None) -> str:
    text = format_strategy_content(content)
    text = _truncate(text, lang)
    if repo_url:
        text += f'\n\n<a href="{repo_url}/tree/main/current">{t("strategist.open_in_github", lang)}</a>'
    return text


async def _check_strategy_ready_msg(message: Message, lang: str) -> tuple[bool, int]:
    from clients.github_oauth import github_oauth
    telegram_user_id = message.chat.id

    if not await github_oauth.is_connected(telegram_user_id):
        await message.answer(t('strategist.github_not_connected', lang))
        return False, telegram_user_id

    strategy_repo = await github_oauth.get_strategy_repo(telegram_user_id)
    if not strategy_repo:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(
                text=t('strategist.btn_select_repo', lang),
                callback_data="strategy_select_repo",
            )]]
        )
        await message.answer(t('strategist.repo_not_selected', lang), reply_markup=keyboard)
        return False, telegram_user_id

    return True, telegram_user_id


async def _check_strategy_ready_cb(callback: CallbackQuery, lang: str) -> tuple[bool, int]:
    from clients.github_oauth import github_oauth
    telegram_user_id = callback.from_user.id

    if not await github_oauth.is_connected(telegram_user_id):
        await callback.answer(t('strategist.not_connected_alert', lang), show_alert=True)
        return False, telegram_user_id

    strategy_repo = await github_oauth.get_strategy_repo(telegram_user_id)
    if not strategy_repo:
        await callback.answer(t('strategist.repo_not_selected_alert', lang), show_alert=True)
        return False, telegram_user_id

    return True, telegram_user_id


@strategist_router.message(F.text == "/rp")
async def cmd_rp(message: Message):
    intern = await get_intern(message.chat.id)
    lang = _lang(intern)
    ready, user_id = await _check_strategy_ready_msg(message, lang)
    if not ready:
        return

    from clients.github_strategy import github_strategy
    content = await github_strategy.get_week_plan(user_id)
    if not content:
        await message.answer(t('strategist.weekplan_not_found', lang))
        return

    repo_url = await github_strategy.get_strategy_repo_url(user_id)
    await message.answer(_format(content, lang, repo_url), parse_mode="HTML", disable_web_page_preview=True)


@strategist_router.message(F.text == "/plan")
async def cmd_plan(message: Message):
    intern = await get_intern(message.chat.id)
    lang = _lang(intern)
    ready, user_id = await _check_strategy_ready_msg(message, lang)
    if not ready:
        return

    from clients.github_strategy import github_strategy
    content = await github_strategy.get_day_plan(user_id)
    if not content:
        is_monday = datetime.now(MOSCOW_TZ).weekday() == 0
        if is_monday:
            await message.answer(t('strategist.dayplan_not_found_monday', lang))
        else:
            await message.answer(t('strategist.dayplan_not_found', lang))
        return

    repo_url = await github_strategy.get_strategy_repo_url(user_id)
    await message.answer(_format(content, lang, repo_url), parse_mode="HTML", disable_web_page_preview=True)


@strategist_router.message(F.text == "/report")
async def cmd_report(message: Message):
    intern = await get_intern(message.chat.id)
    lang = _lang(intern)
    ready, user_id = await _check_strategy_ready_msg(message, lang)
    if not ready:
        return

    from clients.github_strategy import github_strategy
    content = await github_strategy.get_week_report(user_id)
    if not content:
        await message.answer(t('strategist.weekreport_not_found', lang))
        return

    repo_url = await github_strategy.get_strategy_repo_url(user_id)
    await message.answer(_format(content, lang, repo_url), parse_mode="HTML", disable_web_page_preview=True)


# --- Callback-кнопки из уведомлений стратега ---

@strategist_router.callback_query(F.data == "strat_plan")
async def callback_strat_plan(callback: CallbackQuery):
    intern = await get_intern(callback.from_user.id)
    lang = _lang(intern)
    ready, user_id = await _check_strategy_ready_cb(callback, lang)
    if not ready:
        return
    await callback.answer()

    from clients.github_strategy import github_strategy
    content = await github_strategy.get_day_plan(user_id)
    kb = _nav_keyboard("strat_plan", lang)
    if not content:
        is_monday = datetime.now(MOSCOW_TZ).weekday() == 0
        if is_monday:
            await callback.message.answer(t('strategist.dayplan_not_found_monday', lang), reply_markup=kb)
        else:
            await callback.message.answer(t('strategist.dayplan_not_found', lang), reply_markup=kb)
        return

    repo_url = await github_strategy.get_strategy_repo_url(user_id)
    await callback.message.answer(_format(content, lang, repo_url), parse_mode="HTML",
                                  disable_web_page_preview=True, reply_markup=kb)


@strategist_router.callback_query(F.data == "strat_rp")
async def callback_strat_rp(callback: CallbackQuery):
    intern = await get_intern(callback.from_user.id)
    lang = _lang(intern)
    ready, user_id = await _check_strategy_ready_cb(callback, lang)
    if not ready:
        return
    await callback.answer()

    from clients.github_strategy import github_strategy
    content = await github_strategy.get_week_plan(user_id)
    kb = _nav_keyboard("strat_rp", lang)
    if not content:
        await callback.message.answer(t('strategist.weekplan_not_found', lang), reply_markup=kb)
        return

    repo_url = await github_strategy.get_strategy_repo_url(user_id)
    await callback.message.answer(_format(content, lang, repo_url), parse_mode="HTML",
                                  disable_web_page_preview=True, reply_markup=kb)


@strategist_router.callback_query(F.data == "strat_report")
async def callback_strat_report(callback: CallbackQuery):
    intern = await get_intern(callback.from_user.id)
    lang = _lang(intern)
    ready, user_id = await _check_strategy_ready_cb(callback, lang)
    if not ready:
        return
    await callback.answer()

    from clients.github_strategy import github_strategy
    content = await github_strategy.get_week_report(user_id)
    kb = _nav_keyboard("strat_report", lang)
    if not content:
        await callback.message.answer(t('strategist.weekreport_not_found', lang), reply_markup=kb)
        return

    repo_url = await github_strategy.get_strategy_repo_url(user_id)
    await callback.message.answer(_format(content, lang, repo_url), parse_mode="HTML",
                                  disable_web_page_preview=True, reply_markup=kb)


@strategist_router.callback_query(F.data == "strat_day_close")
async def callback_strat_day_close(callback: CallbackQuery):
    intern = await get_intern(callback.from_user.id)
    lang = _lang(intern)
    await callback.answer()
    await callback.message.answer(
        f"{t('strategist.day_close_instruction', lang)}\n\n"
        "<code>~/Github/DS-strategist-agent/scripts/strategist.sh day-close</code>\n\n"
        f"{t('strategist.day_close_notification', lang)}",
        parse_mode="HTML",
    )


# --- Выбор strategy_repo ---

@strategist_router.callback_query(F.data == "strategy_select_repo")
async def callback_strategy_select_repo(callback: CallbackQuery):
    from clients.github_oauth import github_oauth
    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    if not await github_oauth.is_connected(telegram_user_id):
        await callback.answer(t('strategist.not_connected_alert', lang), show_alert=True)
        return

    await callback.answer()

    repos = await github_oauth.get_repos(telegram_user_id, limit=20)
    if not repos:
        await callback.message.answer(t('strategist.repos_error', lang))
        return

    buttons = []
    for repo in repos:
        buttons.append([InlineKeyboardButton(
            text=repo["name"],
            callback_data=f"strategy_repo:{repo['full_name']}",
        )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.answer(t('strategist.select_repo_title', lang), reply_markup=keyboard)


@strategist_router.callback_query(F.data.startswith("strategy_repo:"))
async def callback_strategy_repo_selected(callback: CallbackQuery):
    from clients.github_oauth import github_oauth
    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)
    repo_full_name = callback.data.split(":", 1)[1]

    await github_oauth.set_strategy_repo(telegram_user_id, repo_full_name)
    await callback.answer(t('strategist.repo_selected', lang), show_alert=True)

    await callback.message.edit_text(
        f"<b>{t('strategist.repo_configured', lang)}</b>\n\n"
        f"{t('github.repo_label', lang)}: <code>{repo_full_name}</code>\n\n"
        f"{t('strategist.available_commands', lang)}",
        parse_mode="HTML",
    )
