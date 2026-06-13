"""Безопасные обёртки над Telegram edit-вызовами (BDR4, WP-7 Block BDR).

Ошибка «message is not modified» возникает при двойном тапе по inline-кнопке:
пользователь нажал дважды → второй edit меняет сообщение на идентичное → Telegram
отвечает TelegramBadRequest. Это безвредно (на экране уже нужный текст), но засоряло
ежедневные алерты бота. safe_edit_message() глушит именно этот случай, пробрасывая
остальные TelegramBadRequest и прочие исключения вызывающему.

Глобальный error-handler (bot.py) и heartbeat (handlers/external_session.py) уже
глушат «not modified» у себя; этот хелпер закрывает высокочастотный путь марафона
(handlers/marathon.py) и пригоден для остальных edit-вызовов при последующей миграции.
"""
import logging
from typing import Awaitable, Callable

from aiogram.exceptions import TelegramBadRequest

logger = logging.getLogger(__name__)


async def safe_edit_message(edit_callable: Callable[..., Awaitable], *args, **kwargs) -> bool:
    """Вызвать edit-метод (callback.message.edit_text / edit_reply_markup /
    bot.edit_message_text), проглотив безвредный «message is not modified».

    Возвращает True, если сообщение реально изменено; False, если правка была no-op
    (идентичный текст/разметка — двойной тап). Прочие TelegramBadRequest и любые
    другие исключения пробрасываются вызывающему без изменений.
    """
    try:
        await edit_callable(*args, **kwargs)
        return True
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return False
        raise
