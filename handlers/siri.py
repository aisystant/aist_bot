"""WP-384: /siri - personal notes key and setup steps for the «Заметка в IWE» shortcut.

The shortcut dictates a note on the iPhone (speech is recognised by iOS), sends the
text to POST /api/notes with this key, and speaks back where the note went.
The key is valid only for notes (service iwe_notes_shortcut), separate from the
browser extension key (/iwe). The server keeps only a fingerprint of the key, so it is
shown once: `/siri new` replaces it (the old one stops working), `/siri off` switches it off.
"""

from __future__ import annotations

import html
import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from config.settings import WEBHOOK_URL
from core.voice_access import voice_enabled_for
from db.queries.iwe_extension import has_notes_key, issue_notes_key, revoke_notes_key
from helpers.dual_write import resolve_ory_id_from_chat

logger = logging.getLogger(__name__)

siri_router = Router(name="siri")

_ROTATE_ARGS = {"new", "новый"}
_OFF_ARGS = {"off", "выкл", "отключить"}
SHORTCUT_NAME = "Заметка в IWE"


def notes_endpoint() -> str:
    base = (WEBHOOK_URL or "").rstrip("/")
    return f"{base}/api/notes"


def _key_block(token: str | None) -> str:
    if token:
        return (
            "<b>Ваш ключ (показан один раз):</b>\n"
            f"<code>{html.escape(token)}</code>\n\n"
            "Вставьте его в команду сейчас, потом посмотреть его нельзя, и удалите это сообщение. "
            "Потеряли ключ или он попал к другому человеку: /siri new выдаст новый, старый перестанет работать."
        )
    return (
        "Ключ уже выдан и больше не показывается. Нужен новый (старый перестанет работать): /siri new. "
        "Отключить заметки через Siri совсем: /siri off."
    )


def setup_text(token: str | None, endpoint: str) -> str:
    """Step-by-step shortcut setup for someone doing it for the first time; token=None when already issued."""
    url = html.escape(endpoint)
    return (
        "🎙 <b>Заметки голосом через Siri</b>\n\n"
        f"Скажете «Привет, Siri, {SHORTCUT_NAME}», продиктуете мысль, и Siri ответит, куда записала. "
        "Заметка ляжет туда же, куда заметки с точкой, с пометкой «Siri».\n\n"
        "<b>Как настроить (один раз, 3 минуты):</b>\n"
        "1. На айфоне откройте приложение «Команды», внизу вкладка «Команды», справа вверху «+».\n"
        "2. Нажмите «Добавить действие», найдите «Продиктовать текст» и добавьте. Язык: русский.\n"
        "3. Добавьте действие «Получить содержимое URL». В поле адреса вставьте:\n"
        f"<code>{url}</code>\n"
        "   Нажмите стрелку у действия: Метод - POST; Заголовки - «Добавить новый заголовок», "
        "ключ <code>X-IWE-Api-Key</code>, значение - ваш ключ; Текст запроса - JSON, "
        "«Добавить новое поле» → Текст, ключ <code>text</code>, значение - «Продиктованный текст».\n"
        "4. Добавьте «Получить значение из словаря»: ключ <code>say</code>.\n"
        "5. Добавьте «Произнести текст» (подставится «Значение словаря»).\n"
        f"6. Вверху нажмите имя команды и назовите её «{SHORTCUT_NAME}». Готово.\n\n"
        f"{_key_block(token)}"
    )


@siri_router.message(Command("siri"))
async def cmd_siri(message: Message, command: CommandObject) -> None:
    """Issue the personal notes key (once) and show the shortcut setup steps."""
    if message.from_user is None:
        return
    chat_id = message.from_user.id
    if not voice_enabled_for(chat_id):
        await message.answer(
            "Голосовые заметки и заметки через Siri пока в закрытой проверке.\n\n"
            f"Ваш номер в Telegram: <code>{chat_id}</code>. По нему вас могут включить в проверку.",
            parse_mode="HTML",
        )
        return
    ory_id = await resolve_ory_id_from_chat(chat_id)
    if not ory_id:
        await message.answer("Сначала привяжи аккаунт Aisystant — команда /link")
        return

    arg = (command.args or "").strip().lower()
    if arg in _OFF_ARGS:
        revoked = await revoke_notes_key(ory_id)
        await message.answer(
            "Заметки через Siri отключены, ключ больше не работает. Новый ключ: /siri new."
            if revoked else "Активного ключа нет. Выдать ключ: /siri."
        )
        return

    if arg in _ROTATE_ARGS or not await has_notes_key(ory_id):
        token = await issue_notes_key(ory_id)
    else:
        token = None
    await message.answer(setup_text(token, notes_endpoint()), parse_mode="HTML")
