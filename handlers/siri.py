"""WP-384: /siri - personal notes key and setup steps for the «Заметка в IWE» shortcut.

The shortcut dictates a note on the iPhone (speech is recognised by iOS), sends the
text to POST /api/notes with this key, and speaks back where the note went.
The key is valid only for notes (service iwe_notes_shortcut), separate from the
browser extension key (/iwe). `/siri new` replaces the key; the old one stops working.
"""

from __future__ import annotations

import html
import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from config.settings import WEBHOOK_URL
from core.voice_access import voice_enabled_for
from db.queries.iwe_extension import NOTES_SHORTCUT_SERVICE, generate_extension_token, get_extension_token
from helpers.dual_write import resolve_ory_id_from_chat

logger = logging.getLogger(__name__)

siri_router = Router(name="siri")

_ROTATE_ARGS = {"new", "новый"}
SHORTCUT_NAME = "Заметка в IWE"


def notes_endpoint() -> str:
    base = (WEBHOOK_URL or "").rstrip("/")
    return f"{base}/api/notes"


def setup_text(token: str, endpoint: str) -> str:
    """Step-by-step shortcut setup for someone doing it for the first time."""
    key = html.escape(token)
    url = html.escape(endpoint)
    return (
        "🎙 <b>Заметки голосом через Siri</b>\n\n"
        f"Скажете «Привет, Siri, {SHORTCUT_NAME}», продиктуете мысль, и Siri ответит, куда записала. "
        "Заметка ляжет туда же, куда заметки с точкой.\n\n"
        "<b>Как настроить (один раз, 3 минуты):</b>\n"
        "1. На айфоне откройте приложение «Команды», внизу вкладка «Команды», справа вверху «+».\n"
        "2. Нажмите «Добавить действие», найдите «Продиктовать текст» и добавьте. Язык: русский.\n"
        "3. Добавьте действие «Получить содержимое URL». В поле адреса вставьте:\n"
        f"<code>{url}</code>\n"
        "   Нажмите стрелку у действия: Метод — POST; Заголовки — «Добавить новый заголовок», "
        "ключ <code>X-IWE-Api-Key</code>, значение — ключ ниже; Текст запроса — JSON, "
        "«Добавить новое поле» → Текст, ключ <code>text</code>, значение — «Продиктованный текст».\n"
        "4. Добавьте «Получить значение из словаря»: ключ <code>say</code>.\n"
        "5. Добавьте «Произнести текст» (подставится «Значение словаря»).\n"
        f"6. Вверху нажмите имя команды и назовите её «{SHORTCUT_NAME}». Готово.\n\n"
        "<b>Ваш ключ:</b>\n"
        f"<code>{key}</code>\n\n"
        "Ключ годится только для записи заметок. Не пересылайте это сообщение. "
        "Если ключ попал к другому человеку, отправьте /siri new: старый перестанет работать."
    )


@siri_router.message(Command("siri"))
async def cmd_siri(message: Message, command: CommandObject) -> None:
    """Issue (or show) the personal notes key and the shortcut setup steps."""
    if message.from_user is None:
        return
    chat_id = message.from_user.id
    if not voice_enabled_for(chat_id):
        await message.answer("Заметки через Siri пока доступны не всем. Мы сообщим, когда откроем их.")
        return
    ory_id = await resolve_ory_id_from_chat(chat_id)
    if not ory_id:
        await message.answer("Сначала привяжи аккаунт Aisystant — команда /link")
        return

    rotate = (command.args or "").strip().lower() in _ROTATE_ARGS
    token = None if rotate else await get_extension_token(ory_id, NOTES_SHORTCUT_SERVICE)
    if token is None:
        token = await generate_extension_token(ory_id, NOTES_SHORTCUT_SERVICE, scope="notes")
    if not token:
        logger.error("[siri] notes key not issued for chat_id=%s", chat_id)
        await message.answer("Не удалось создать ключ. Напишите в поддержку: /support")
        return
    await message.answer(setup_text(token, notes_endpoint()), parse_mode="HTML")
