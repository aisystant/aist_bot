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
REFUSED_FILE_TEXT = "Готово, но Telegram не принял файл. Ссылка: {url}"
UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024  # предел загрузки файла ботом в Telegram
PHOTO_LIMIT_BYTES = 10 * 1024 * 1024  # предел загрузки фото; по ссылке Telegram берёт фото только до 5 МБ

# Фоновые ожидания держим ссылками, иначе asyncio может собрать задачу сборщиком мусора.
_pending: set[asyncio.Task] = set()


def _client() -> ImageServiceClient | None:
    url = os.getenv("IMAGE_SERVICE_URL", "")
    token = os.getenv("IMAGE_SERVICE_TOKEN", "")
    return ImageServiceClient(url, token) if url and token else None


def _file_name(url: str) -> str:
    return url.rsplit("/", 1)[-1] or "file"


async def _send_image(message: Message, url: str) -> None:
    """Картинка видна сразу как фото (Telegram сам сжимает оригинал); оригинал следом файлом.
    Лёгкую копию Higgsfield (WebP) Telegram как фото отвергает (29.10), поэтому фото — из оригинала."""
    original = await download(url, UPLOAD_LIMIT_BYTES)
    if original is None:
        await message.answer(LINK_TEXT.format(url=url), parse_mode=None)
        return
    if len(original) <= PHOTO_LIMIT_BYTES:
        try:
            await message.answer_photo(BufferedInputFile(original, filename=_file_name(url)))
        except TelegramAPIError as err:
            logger.warning("[image] photo refused, sending file only: %s", err.message)
    await message.answer_document(BufferedInputFile(original, filename=_file_name(url)))


async def _send_video(message: Message, url: str) -> None:
    """Бот скачивает видео сам: по ссылке Telegram берёт только до 20 МБ; больше 50 МБ — ссылкой."""
    data = await download(url, UPLOAD_LIMIT_BYTES)
    if data is None:
        await message.answer(LINK_TEXT.format(url=url), parse_mode=None)
        return
    try:
        await message.answer_video(BufferedInputFile(data, filename=_file_name(url)))
    except TelegramAPIError:
        await message.answer_document(BufferedInputFile(data, filename=_file_name(url)))


async def _deliver(message: Message, client: ImageServiceClient, kind: str, job_id: str) -> None:
    try:
        urls = await client.wait(job_id)
    except ImageServiceError as err:
        logger.warning("[image] job=%s failed: status=%s %s", job_id, err.status, err.detail)
        await message.answer(FAILED_TEXT, parse_mode=None)
        return
    for url in urls:
        try:
            if kind == "image":
                await _send_image(message, url)
            else:
                await _send_video(message, url)
        except TelegramAPIError as err:
            # Generation succeeded and is paid for: hand over the link instead of a failure text.
            logger.warning("[image] job=%s delivery refused by Telegram: %s", job_id, err.message)
            await message.answer(REFUSED_FILE_TEXT.format(url=url), parse_mode=None)


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
