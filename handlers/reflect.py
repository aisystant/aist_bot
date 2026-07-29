"""
Рефлексия — коммит дневной рефлексии в personal-guide (WP-309 Ф3).

Команды:
- /reflect — создать history/YYYY-MM/YYYY-MM-DD-reflection.md с шаблоном
- /reflect <текст> — создать файл с указанным текстом

Записывает через GitHub App installation token (clients.github_app).
"""

import logging
import re
from datetime import datetime, timezone, timedelta

from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import Command

from db.queries import get_intern
from db.queries.github_app import get_app_installation
from clients.github_app import write_file
from i18n import t

logger = logging.getLogger(__name__)

reflect_router = Router(name="reflect")


# Reflection template (source: ~/.claude/skills/personal-guide-render/templates/reflection-template.md)
_REFLECTION_TEMPLATE = """# Рефлексия — {date}

> Пять минут на пять вопросов. Не нужно отвечать «правильно». Нужно — честно.

## 1. Что сделал сегодня

Три факта. Без оценки.

-
-
-

## 2. Что не сделал

Что собирался, но не получилось. Одна-две вещи.

-

## 3. Что узнал

Новое, неожиданное, или подтвердилось что-то важное.

-

## 4. Зачем это

Как сегодняшнее связано с моей программой развития. Одно предложение.

-

## 5. Что завтра

Одна важная вещь. Не список.

-

---

*Пилот: {github_user}*

*Этот файл читается Портным при следующем `/personal-guide-render`.*
"""


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


def _moscow_today() -> str:
    """Сегодняшняя дата в московском времени (YYYY-MM-DD)."""
    return datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%d")


def _parse_date(text: str) -> str | None:
    """Попытка распарсить YYYY-MM-DD из начала строки."""
    if not text:
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2})(\s|$)", text)
    if m:
        return m.group(1)
    return None


@reflect_router.message(Command("reflect"))
async def cmd_reflect(message: Message):
    """Команда /reflect — создать или записать рефлексию в personal-guide."""
    chat_id = message.chat.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)

    # 1. Проверяем, подключён ли GitHub App
    installation = await get_app_installation(chat_id)
    if not installation:
        await message.answer(
            "📚 *Личное руководство ещё не подключено*\n\n"
            "Набери команду /connect_guide, чтобы создать репо "
            "и получить возможность писать рефлексии.",
            parse_mode="Markdown",
        )
        return

    # WP-309 B3: consent gate — не писать рефлексии без opt-in.
    from helpers.dual_write import resolve_ory_id_from_chat
    from db.queries.consent import get_consent
    account_id = await resolve_ory_id_from_chat(chat_id)
    if account_id:
        consent = await get_consent(account_id)
        if not consent or not consent["opt_in"]:
            await message.answer(
                "⚠️ Для записи рефлексий в персональное руководство требуется согласие на обработку персональных данных.\n\n"
                "Набери команду <b>/consent opt-in</b>, затем нажми «✅ Согласен, включить». "
                "После этого повтори <b>/reflect</b>.",
                parse_mode="HTML",
            )
            return

    repo = installation.get("app_repo_full_name")
    installation_id = installation.get("app_installation_id")
    github_user = installation.get("github_username") or "пилот"

    if not repo or not installation_id:
        await message.answer(t('errors.processing_error', lang))
        logger.error("[reflect] installation exists but repo=%s installation_id=%s", repo, installation_id)
        return

    # 2. Парсим аргументы
    text = message.text or ""
    parts = text.strip().split(maxsplit=1)
    arg = parts[1] if len(parts) > 1 else ""

    date_str = _parse_date(arg)
    if date_str:
        content = arg[len(date_str):].strip()
    else:
        date_str = _moscow_today()
        content = arg.strip()

    path = f"history/{date_str[:7]}/{date_str}-reflection.md"

    # 3. Формируем содержимое
    if content:
        file_content = f"# Рефлексия — {date_str}\n\n{content}\n"
        commit_msg = f"reflect: {date_str} — запись из бота"
        reply_text = (
            f"✅ *Рефлексия записана*\n\n"
            f"Файл: `{path}`\n"
            f"Репо: `{repo}`"
        )
    else:
        file_content = _REFLECTION_TEMPLATE.format(date=date_str, github_user=github_user)
        commit_msg = f"reflect: {date_str} — шаблон рефлексии"
        reply_text = (
            f"📝 *Шаблон рефлексии создан*\n\n"
            f"Файл: `{path}`\n"
            f"Репо: `{repo}`\n\n"
            f"Ответь на 5 вопросов прямо в файле (через VS Code, браузер или следующим сообщением `/reflect ТВОЙ ТЕКСТ`)."
        )

    # 4. Пишем через GitHub App
    result = await write_file(
        installation_id=int(installation_id),
        repo_full_name=repo,
        path=path,
        content=file_content,
        message=commit_msg,
    )

    if result.success:
        await message.answer(reply_text, parse_mode="Markdown")
        logger.info("[reflect] wrote %s/%s for chat_id=%d", repo, path, chat_id)
    else:
        logger.error("[reflect] write failed for chat_id=%d: %s", chat_id, result.error)
        await message.answer(
            "⚠️ *Не удалось записать рефлексию*\n\n"
            f"Ошибка: `{result.error or 'unknown'}`\n"
            "Проверь, что репо существует и GitHub App установлен.",
            parse_mode="Markdown",
        )
