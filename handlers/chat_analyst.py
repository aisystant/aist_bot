"""
Клиентская сторона «Аналитика чата» (РП-612): бот передаёт сообщения закрытых чатов
сервису и по команде владельца запрашивает дайджест. Допуск, границы сбора, повторы
и сроки хранения решает сервис (chat-analyst-service/CONTRACT.md), не бот.

- my_chat_member  — бот стал админом: уведомить участников, закрепить, зарегистрировать
                    чат; бота убрали или сняли права: снять регистрацию
- сообщения групп — передача в сервис, только пока сервис подтверждает, что сбор включён
- /chat_digest    — admin-команда (только CHAT_ANALYST_OWNER_ID, ответ только в личку)

Хендлеры по окончании всегда отдают событие дальше (SkipHandler): после них работает
детекция упоминаний каналов (SC.118), она ловит любое групповое сообщение.

Включается четырьмя переменными окружения: CHAT_ANALYST_URL, CHAT_ANALYST_TOKEN,
CHAT_ANALYST_CHATS (id разрешённых чатов через запятую), CHAT_ANALYST_TERMS_URL
(публичные условия для уведомления). Нет любой из них — модуль ничего не делает.
"""

import asyncio
import os
import time
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import ChatMemberAdministrator, ChatMemberOwner, ChatMemberUpdated, Message

from clients.chat_analyst import ChatAnalystClient, ChatAnalystError
from config import get_logger

logger = get_logger(__name__)

chat_analyst_router = Router(name="chat_analyst")

GROUP_TYPES = {"group", "supergroup"}
STATUS_RECHECK_SECONDS = 60.0
DEFAULT_DIGEST_DAYS = 7
MAX_DIGEST_DAYS = 90  # срок хранения сырых сообщений в сервисе
TELEGRAM_TEXT_LIMIT = 4096

NOTICE_TEXT = (
    "Сообщения этого чата архивируются для сводок наставника. Сводка может быть по потоку "
    "в целом и по отдельному участнику; наставник видит ваши сообщения под вашим именем. "
    "Условия: {terms_url}\n"
    "Продолжение участия в чате = согласие."
)
USAGE_TEXT = "Формат: /chat_digest <id чата> [дней, по умолчанию 7] [user=<id участника>]"
DIGEST_ERRORS = {
    403: "Вы не администратор этого чата (проверка идёт в реальном времени).",
    404: "Чат не зарегистрирован в сервисе.",
    409: "Сбор по чату остановлен (бота убрали из чата).",
    413: "Окно слишком большое: уменьшите число дней.",
}
SERVICE_DOWN_TEXT = "Сервис недоступен или не ответил. Повторите позже."


@dataclass(frozen=True)
class Config:
    client: ChatAnalystClient
    chats: frozenset[int]
    terms_url: str
    owner_id: int


def _config() -> Config | None:
    url = os.getenv("CHAT_ANALYST_URL", "")
    token = os.getenv("CHAT_ANALYST_TOKEN", "")
    chats = os.getenv("CHAT_ANALYST_CHATS", "")
    terms_url = os.getenv("CHAT_ANALYST_TERMS_URL", "")
    try:
        owner_id = int(os.getenv("CHAT_ANALYST_OWNER_ID", "0"))
        allowed = frozenset(int(part) for part in chats.split(",") if part.strip())
    except ValueError:
        logger.error("[chat_analyst] config ignored: CHAT_ANALYST_OWNER_ID or CHAT_ANALYST_CHATS is not numeric")
        return None
    if not (url and token and chats and terms_url and owner_id):
        return None
    return Config(ChatAnalystClient(url, token), allowed, terms_url, owner_id)


# Состояние процесса, не базы: после перезапуска пусто и восстанавливается запросом
# статуса у сервиса при первом сообщении чата.
_active: set[int] = set()
_suspended: set[int] = set()  # бота разжаловали, а снять регистрацию не удалось: сервис ещё говорит active
_status_checked_at: dict[int, float] = {}
_background: set[asyncio.Task] = set()


def _is_admin(member) -> bool:
    return isinstance(member, (ChatMemberAdministrator, ChatMemberOwner))


def _can_pin(member, chat_type: str) -> bool:
    # В обычной группе (не супергруппе) поле у администратора не заполняется: закреплять может любой админ.
    if isinstance(member, ChatMemberOwner) or chat_type == "group":
        return True
    return getattr(member, "can_pin_messages", None) is True


# --- my_chat_member -------------------------------------------------------

@chat_analyst_router.my_chat_member()
async def on_bot_membership(update: ChatMemberUpdated):
    try:
        cfg = _config()
        if cfg and update.chat.type in GROUP_TYPES and update.chat.id in cfg.chats:
            await _handle_membership(cfg, update)
    except Exception:  # noqa: BLE001 - чужой хендлер (SC.118) должен получить событие в любом случае
        logger.exception("[chat_analyst] membership handler failed chat=%s", update.chat.id)
    raise SkipHandler


async def _handle_membership(cfg: Config, update: ChatMemberUpdated) -> None:
    chat_id = update.chat.id
    if _is_admin(update.new_chat_member):
        if not _is_admin(update.old_chat_member):
            await _start_collection(cfg, update)
    elif _is_admin(update.old_chat_member) or chat_id in _active:
        _active.discard(chat_id)
        _suspended.add(chat_id)
        try:
            await cfg.client.unregister(chat_id)
            _suspended.discard(chat_id)
        except ChatAnalystError as err:
            if err.status == 404:
                _suspended.discard(chat_id)
            else:
                logger.error("[chat_analyst] unregister failed chat=%s status=%s", chat_id, err.status)


async def _start_collection(cfg: Config, update: ChatMemberUpdated) -> None:
    if update.from_user is None or update.from_user.id != cfg.owner_id:
        return
    await _begin_collection(cfg, update.bot, update.chat, update.new_chat_member, update.date.timestamp())


async def _begin_collection(cfg: Config, bot, chat, member, added_at: float) -> None:
    """Уведомить чат, закрепить уведомление и зарегистрировать чат в сервисе.
    Общий путь для события «бот стал админом» и команды /chat_collect."""
    if chat.username:
        await bot.send_message(cfg.owner_id, f"Чат «{chat.title}» публичный: сбор не включён (только закрытые чаты).")
        return
    if not _can_pin(member, chat.type):
        await bot.send_message(
            cfg.owner_id,
            f"Сбор по чату «{chat.title}» не включён: дайте боту право закреплять сообщения.",
        )
        return
    try:
        state = (await cfg.client.status(chat.id)).get("state")
    except ChatAnalystError:
        await bot.send_message(cfg.owner_id, f"Сбор по чату «{chat.title}» не включён: сервис недоступен.")
        return
    try:
        if state == "active":  # бота вернули, а снятие регистрации пропущено: начинаем новую эпоху
            await cfg.client.unregister(chat.id)
        notice = await bot.send_message(chat.id, NOTICE_TEXT.format(terms_url=cfg.terms_url))
        await bot.pin_chat_message(chat.id, notice.message_id)
        notified_at = time.time()
        await cfg.client.register(chat.id, added_at, notified_at)
    except (TelegramAPIError, ChatAnalystError) as err:
        logger.error("[chat_analyst] start failed chat=%s error=%s", chat.id, type(err).__name__)
        await bot.send_message(cfg.owner_id, f"Сбор по чату «{chat.title}» не включён: {type(err).__name__}.")
        return
    _suspended.discard(chat.id)
    _active.add(chat.id)
    await bot.send_message(cfg.owner_id, f"Сбор по чату «{chat.title}» включён, участники уведомлены.")


# --- /chat_collect --------------------------------------------------------

COLLECT_USAGE_TEXT = "Формат: /chat_collect <id чата>. Для чата, где бот уже администратор."


@chat_analyst_router.message(Command("chat_collect"))
async def cmd_chat_collect(message: Message):
    """Начать сбор в чате, где бот стал админом до включения функции: события my_chat_member не будет."""
    cfg = _config()
    sender = message.from_user
    if cfg is None or sender is None or message.chat.type != "private" or sender.id != cfg.owner_id:
        raise SkipHandler
    parts = (message.text or "").split()
    try:
        chat_id = int(parts[1]) if len(parts) == 2 else None
    except ValueError:
        chat_id = None
    if chat_id is None:
        await message.answer(COLLECT_USAGE_TEXT, parse_mode=None)
        return
    if chat_id not in cfg.chats:
        await message.answer("Этого чата нет в списке разрешённых для сбора.", parse_mode=None)
        return
    bot = message.bot
    try:
        chat = await bot.get_chat(chat_id)
        member = await bot.get_chat_member(chat_id, (await bot.me()).id)
        state = (await cfg.client.status(chat_id)).get("state")
    except (TelegramAPIError, ChatAnalystError) as err:
        logger.warning("[chat_analyst] collect precheck failed chat=%s error=%s", chat_id, type(err).__name__)
        await message.answer(f"Не удалось проверить чат: {type(err).__name__}.", parse_mode=None)
        return
    if chat.type not in GROUP_TYPES or not _is_admin(member):
        await message.answer("Бот не администратор этой группы: сбор не включён.", parse_mode=None)
        return
    if state == "active":
        _active.add(chat_id)
        await message.answer("Сбор по этому чату уже идёт.", parse_mode=None)
        return
    await _begin_collection(cfg, bot, chat, member, time.time())


# --- сообщения групп ------------------------------------------------------

@chat_analyst_router.message(F.chat.type.in_(GROUP_TYPES))
async def on_group_message(message: Message):
    try:
        cfg = _config()
        text = (message.text or message.caption or "")
        sender = message.from_user
        if (
            cfg and message.chat.id in cfg.chats and sender and not sender.is_bot
            and text and not text.startswith("/")
        ):
            task = asyncio.create_task(
                _forward(cfg, message.chat.id, message.message_id, sender.id, text, message.date.timestamp())
            )
            _background.add(task)
            task.add_done_callback(_background.discard)
    except Exception:  # noqa: BLE001
        logger.exception("[chat_analyst] message handler failed chat=%s", message.chat.id)
    raise SkipHandler


async def _forward(cfg: Config, chat_id: int, message_id: int, sender_id: int, text: str, sent_at: float) -> None:
    if not await _is_collecting(cfg, chat_id):
        return
    try:
        await cfg.client.send_message(chat_id, message_id, sender_id, text, sent_at)
    except ChatAnalystError as err:
        if err.status == 404 or (err.status == 409 and err.detail == "ChatFrozen"):
            _active.discard(chat_id)
        elif err.status != 409:  # 409: сообщение раньше границы сбора, дубль — ожидаемо
            logger.warning("[chat_analyst] message dropped chat=%s status=%s", chat_id, err.status)


async def _is_collecting(cfg: Config, chat_id: int) -> bool:
    if chat_id in _suspended:
        return False
    if chat_id in _active:
        return True
    now = time.monotonic()
    if now - _status_checked_at.get(chat_id, float("-inf")) < STATUS_RECHECK_SECONDS:
        return False
    _status_checked_at[chat_id] = now
    try:
        state = (await cfg.client.status(chat_id)).get("state")
    except ChatAnalystError:
        return False
    if state == "active":
        _active.add(chat_id)
    return state == "active"


# --- /chat_digest ---------------------------------------------------------

def _parse_digest_args(text: str) -> tuple[int, int, int | None] | None:
    chat_id: int | None = None
    days = DEFAULT_DIGEST_DAYS
    participant: int | None = None
    for part in text.split()[1:]:
        try:
            if part.startswith("user="):
                participant = int(part[5:])
            elif chat_id is None:
                chat_id = int(part)
            else:
                days = int(part)
        except ValueError:
            return None
    if chat_id is None or not 1 <= days <= MAX_DIGEST_DAYS:
        return None
    return chat_id, days, participant


@chat_analyst_router.message(Command("chat_digest"))
async def cmd_chat_digest(message: Message):
    cfg = _config()
    sender = message.from_user
    if cfg is None or sender is None or message.chat.type != "private" or sender.id != cfg.owner_id:
        raise SkipHandler
    parsed = _parse_digest_args(message.text or "")
    if parsed is None:
        await message.answer(USAGE_TEXT, parse_mode=None)
        return
    chat_id, days, participant = parsed
    try:
        digest = await cfg.client.digest(chat_id, sender.id, time.time() - days * 86400, participant)
    except ChatAnalystError as err:
        logger.warning("[chat_analyst] digest refused chat=%s status=%s", chat_id, err.status)
        await message.answer(DIGEST_ERRORS.get(err.status, SERVICE_DOWN_TEXT), parse_mode=None)
        return
    for start in range(0, len(digest), TELEGRAM_TEXT_LIMIT):
        await message.answer(digest[start:start + TELEGRAM_TEXT_LIMIT], parse_mode=None)
