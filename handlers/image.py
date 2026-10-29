"""
Окно службы Image в боте (РП-614): /image и /video в личке.

Бот — тонкий клиент: передаёт описание службе и присылает готовый файл. Кого пускать
(список Telegram-ников), дневной лимит и модель решает служба, не бот.

Включается двумя переменными окружения: IMAGE_SERVICE_URL, IMAGE_SERVICE_TOKEN.
Нет любой из них — модуль отдаёт команды дальше (SkipHandler) и ничего не делает.
"""

import asyncio
import os

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from clients.image_service import ImageServiceClient, ImageServiceError, download
from config import get_logger

logger = get_logger(__name__)

image_router = Router(name="image")

KINDS = {"image": "картинку", "video": "видео"}
WAIT_HINT = {"image": "около минуты", "video": "несколько минут"}
USAGE_TEXT = "Напишите описание после команды, например: /image красный робот на столе или /video кот идёт по крыше"
REFUSALS = {
    403: "Эта команда вам пока недоступна.",
    429: "На сегодня лимит исчерпан ({limit} на всех). Завтра снова можно.",
}
SERVICE_DOWN_TEXT = "Служба генерации сейчас недоступна, попробуйте позже."
# Higgsfield может взять плату и за неудачную попытку, поэтому тексты не обещают возврат лимита.
FAILED_TEXT = "Не получилось сгенерировать. Попробуйте ещё раз или переформулируйте."
LINK_TEXT = "Готово, но файл слишком большой для Telegram. Ссылка: {url}"
UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024  # предел загрузки файла ботом в Telegram

# Фоновые ожидания держим ссылками, иначе asyncio может собрать задачу сборщиком мусора.
_pending: set[asyncio.Task] = set()


def _client() -> ImageServiceClient | None:
    url = os.getenv("IMAGE_SERVICE_URL", "")
    token = os.getenv("IMAGE_SERVICE_TOKEN", "")
    return ImageServiceClient(url, token) if url and token else None


async def _send_result(message: Message, kind: str, url: str) -> None:
    """Бот скачивает файл сам: по ссылке Telegram берёт фото только до 5 МБ, видео до 20 МБ.
    Фото крупнее предела фото уходит документом; файл больше 50 МБ — ссылкой."""
    data = await download(url, UPLOAD_LIMIT_BYTES)
    if data is None:
        await message.answer(LINK_TEXT.format(url=url), parse_mode=None)
        return
    name = url.rsplit("/", 1)[-1] or ("image.png" if kind == "image" else "video.mp4")
    file = BufferedInputFile(data, filename=name)
    try:
        if kind == "image":
            await message.answer_photo(file)
        else:
            await message.answer_video(file)
    except TelegramAPIError:
        await message.answer_document(BufferedInputFile(data, filename=name))


async def _deliver(message: Message, client: ImageServiceClient, kind: str, job_id: str) -> None:
    try:
        for url in await client.wait(job_id):
            await _send_result(message, kind, url)
    except (ImageServiceError, TelegramAPIError) as err:
        logger.warning("[image] job=%s delivery failed: %s", job_id, type(err).__name__)
        await message.answer(FAILED_TEXT, parse_mode=None)


async def _generate(message: Message, command: CommandObject, kind: str) -> None:
    client = _client()
    sender = message.from_user
    if client is None or sender is None or message.chat.type != "private":
        raise SkipHandler
    prompt = (command.args or "").strip()
    if not prompt:
        await message.answer(USAGE_TEXT, parse_mode=None)
        return
    try:
        job_id = await client.submit(kind, prompt, sender.id, sender.username)
    except ImageServiceError as err:
        logger.info("[image] submit refused status=%s", err.status)
        text = REFUSALS.get(err.status, SERVICE_DOWN_TEXT).format(limit=err.limit)
        await message.answer(text, parse_mode=None)
        return
    await message.answer(f"Делаю {KINDS[kind]}, это займёт {WAIT_HINT[kind]}.", parse_mode=None)
    task = asyncio.create_task(_deliver(message, client, kind, job_id))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


@image_router.message(Command("image"))
async def cmd_image(message: Message, command: CommandObject):
    await _generate(message, command, "image")


@image_router.message(Command("video"))
async def cmd_video(message: Message, command: CommandObject):
    await _generate(message, command, "video")
