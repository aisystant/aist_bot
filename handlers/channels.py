"""
Ассистент упоминаний в каналах (SC.118).

/channels — настройка мониторинга каналов
channel_post — детекция упоминаний → уведомление / черновик в личку

Изолированный модуль: собственный Router, не влияет на существующие handlers.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from aiogram import Router, F, Bot
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ChatMemberAdministrator, ChatMemberOwner,
)
from aiogram.filters import Command

from db.queries import get_intern
from db.queries.users import is_onboarded, coerce_ui_lang
from db.queries.channels import (
    get_monitors_for_channel,
    get_user_monitors,
    upsert_monitor,
    deactivate_monitor,
    is_mention_logged,
    log_mention,
    find_user_by_username,
    find_user_by_name,
)
from core.mention_detector import detect_mentions, MentionMatch
from i18n import t

logger = logging.getLogger(__name__)

channels_router = Router(name="channels")

# Cooldown: не чаще 1 уведомления в 30 сек на канал (per-user)
_cooldown: dict[tuple[int, int], float] = {}  # (channel_id, chat_id) → timestamp
_COOLDOWN_SEC = 30

# Каналы, для которых уже выполнялся auto-discovery (чтобы не повторять)
_discovered_channels: set[int] = set()


def _is_cooled_down(channel_id: int, chat_id: int) -> bool:
    """Проверить cooldown для пары канал+пользователь."""
    key = (channel_id, chat_id)
    last = _cooldown.get(key, 0)
    now = datetime.now(timezone.utc).timestamp()
    if now - last < _COOLDOWN_SEC:
        return False
    _cooldown[key] = now
    return True


# ═══════════════════════════════════════════════════════════
# /channels — настройка мониторинга
# ═══════════════════════════════════════════════════════════

@channels_router.message(Command("channels"))
async def cmd_channels(message: Message, bot: Bot):
    """Показать список отслеживаемых каналов и управление ими (T2+)."""
    intern = await get_intern(message.chat.id)
    if not intern or not await is_onboarded(intern):
        await message.answer(t('channels.not_onboarded', intern.get('language', 'ru') if intern else 'ru'))
        return

    # Tier gate: T2+ (requires active subscription or trial)
    from core.tier_detector import detect_ui_tier
    from core.tier_config import UITier
    tier = await detect_ui_tier(message.chat.id)
    if tier < UITier.T2_LEARNING:
        lang = intern.get('language', 'ru')
        await message.answer(t('channels.tier_required', lang))
        return

    monitors = await get_user_monitors(message.chat.id)
    lang = intern.get('language', 'ru')

    if not monitors:
        await message.answer(t('channels.no_monitors', lang))
        return

    # Список каналов с кнопками вкл/выкл
    lines = [t('channels.list_header', lang)]
    buttons = []
    for m in monitors:
        status = '🟢' if m['active'] else '🔴'
        admin_badge = ' 👑' if m['is_admin'] else ''
        lines.append(f"{status} <b>{m['channel_title'] or m['channel_id']}</b>{admin_badge}")

        action = 'off' if m['active'] else 'on'
        action_label = '🔴 Выкл' if m['active'] else '🟢 Вкл'
        buttons.append([InlineKeyboardButton(
            text=f"{action_label} {m['channel_title'] or m['channel_id']}",
            callback_data=f"chmon:{action}:{m['channel_id']}",
        )])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await message.answer('\n'.join(lines), reply_markup=kb, parse_mode='HTML')


@channels_router.callback_query(F.data.startswith("chmon:"))
async def cb_channel_monitor(callback: CallbackQuery):
    """Вкл/выкл мониторинг канала."""
    parts = callback.data.split(':')
    if len(parts) != 3:
        await callback.answer("❌")
        return

    action, channel_id_str = parts[1], parts[2]
    try:
        channel_id = int(channel_id_str)
    except ValueError:
        await callback.answer("❌")
        return

    chat_id = callback.from_user.id

    if action == 'off':
        await deactivate_monitor(channel_id, chat_id)
        await callback.answer("🔴 Мониторинг выключен")
    else:
        # Реактивация
        intern = await get_intern(chat_id)
        if intern:
            await upsert_monitor(
                channel_id=channel_id,
                channel_title='',
                user_id=str(intern['user_id']),
                chat_id=chat_id,
            )
        await callback.answer("🟢 Мониторинг включён")

    # Обновить сообщение
    await cmd_channels(callback.message, callback.message.bot)
    try:
        await callback.message.delete()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
# my_chat_member — автоматическая регистрация при добавлении бота в канал
# ═══════════════════════════════════════════════════════════

@channels_router.my_chat_member()
async def on_bot_added_to_channel(update):
    """Когда бота добавляют в канал/группу как админа — предложить мониторинг."""
    new_member = update.new_chat_member
    old_member = update.old_chat_member

    # Бот стал админом?
    is_now_admin = isinstance(new_member, (ChatMemberAdministrator, ChatMemberOwner))
    was_admin = isinstance(old_member, (ChatMemberAdministrator, ChatMemberOwner))

    if not is_now_admin or was_admin:
        return  # Не новое назначение админом

    chat = update.chat
    added_by = update.from_user

    if not added_by:
        return

    # Мониторинг только для владельца (DEVELOPER_CHAT_ID)
    dev_chat_id_str = os.getenv("DEVELOPER_CHAT_ID")
    if dev_chat_id_str and added_by.id != int(dev_chat_id_str):
        return

    # Найти пользователя бота, который добавил
    intern = await get_intern(added_by.id)
    if not intern or not await is_onboarded(intern):
        return

    # Определить, является ли добавивший владельцем/админом
    is_admin = True  # Раз добавил бота — скорее всего админ

    # Автоматически создать монитор
    await upsert_monitor(
        channel_id=chat.id,
        channel_title=chat.title or str(chat.id),
        user_id=str(intern['user_id']),
        chat_id=added_by.id,
        is_admin=is_admin,
    )

    # Уведомить пользователя в личку
    lang = intern.get('language', 'ru')
    try:
        bot = update.bot
        await bot.send_message(
            added_by.id,
            t('channels.auto_registered', lang, channel=chat.title or str(chat.id)),
            parse_mode='HTML',
        )
    except Exception as e:
        logger.warning(f"[SC.118] Failed to notify {added_by.id} about channel registration: {e}")


# ═══════════════════════════════════════════════════════════
# channel_post / message в группах — детекция упоминаний
# ═══════════════════════════════════════════════════════════

@channels_router.channel_post()
async def on_channel_post(message: Message, bot: Bot):
    """Обработка сообщений в каналах — детекция упоминаний."""
    await _process_channel_message(message, bot)


@channels_router.message(F.chat.type.in_({"group", "supergroup"}))
async def on_group_message(message: Message, bot: Bot):
    """Обработка сообщений в группах — детекция упоминаний."""
    # Не реагировать на команды бота
    if message.text and message.text.startswith('/'):
        return
    await _process_channel_message(message, bot)


async def _auto_discover_admins(channel_id: int, channel_title: str, bot: Bot):
    """Автоматически найти админов канала среди пользователей бота и создать мониторы.

    Вызывается при первом сообщении из канала, где нет мониторов.
    Telegram Bot API не даёт список каналов бота, поэтому discovery
    происходит лениво — при первом channel_post.
    """
    from db.connection import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Получить всех пользователей бота с заполненным tg_username
        users = await conn.fetch('''
            SELECT u.id AS user_id, u.telegram_id AS chat_id, u.tg_username, u.name, u.language,
                   s.onboarding_completed
            FROM public.users u
            LEFT JOIN development.user_state s ON s.user_id = u.id
            WHERE s.onboarding_completed = TRUE
              AND u.tg_username IS NOT NULL
              AND u.tg_username != ''
        ''')

    # Auto-discovery только для владельца (DEVELOPER_CHAT_ID)
    dev_chat_id_str = os.getenv("DEVELOPER_CHAT_ID")
    dev_chat_id = int(dev_chat_id_str) if dev_chat_id_str else None

    registered = 0
    for user in users:
        # Пропускать всех кроме владельца
        if dev_chat_id and user['chat_id'] != dev_chat_id:
            continue

        try:
            member = await bot.get_chat_member(channel_id, user['chat_id'])
            if isinstance(member, (ChatMemberAdministrator, ChatMemberOwner)):
                await upsert_monitor(
                    channel_id=channel_id,
                    channel_title=channel_title,
                    user_id=str(user['user_id']),
                    chat_id=user['chat_id'],
                    is_admin=True,
                )
                registered += 1
                logger.info(f"[SC.118] Auto-discovered admin {user['chat_id']} for channel {channel_id}")
        except Exception:
            # Пользователь не в канале или бот не может проверить — пропускаем
            continue

    if registered:
        logger.info(f"[SC.118] Auto-discovered {registered} admins for channel {channel_title} ({channel_id})")

    return registered


async def _process_channel_message(message: Message, bot: Bot):
    """Общая логика обработки сообщений из каналов/групп."""
    if not message.text:
        return

    channel_id = message.chat.id

    # Получить мониторы для этого канала
    monitors = await get_monitors_for_channel(channel_id)
    if not monitors:
        # Автодискавери: первое сообщение из канала — найти админов среди пользователей бота
        if channel_id not in _discovered_channels:
            _discovered_channels.add(channel_id)
            discovered = await _auto_discover_admins(
                channel_id, message.chat.title or str(channel_id), bot
            )
            if discovered:
                monitors = await get_monitors_for_channel(channel_id)
        if not monitors:
            return

    # Детектировать упоминания
    matches = detect_mentions(message, monitors)
    if not matches:
        return

    # Уведомления только владельцу (DEVELOPER_CHAT_ID), остальным — отключены
    dev_chat_id_str = os.getenv("DEVELOPER_CHAT_ID")
    dev_chat_id = int(dev_chat_id_str) if dev_chat_id_str else None

    # Обработать каждое упоминание
    for match in matches:
        # Только владелец получает уведомления
        if dev_chat_id and match.chat_id != dev_chat_id:
            continue

        # Cooldown
        if not _is_cooled_down(channel_id, match.chat_id):
            continue

        # Дедупликация (log-before-send)
        if await is_mention_logged(channel_id, message.message_id, match.chat_id):
            continue

        # Записать лог ДО отправки (idempotent notifications, rule 10.10)
        await log_mention(
            channel_id=channel_id,
            message_id=message.message_id,
            mentioned_chat_id=match.chat_id,
            mention_type=match.mention_type,
            draft_sent=match.is_admin,
        )

        await _send_simple_notification(message, bot, match)


async def _notify_reply_to_bot_user(message: Message, bot: Bot, reply_user_id: int):
    """Уведомить участника бота, если на его сообщение ответили (даже без монитора)."""
    intern = await get_intern(reply_user_id)
    if not intern or not await is_onboarded(intern):
        return

    channel_id = message.chat.id

    # Cooldown + dedup
    if not _is_cooled_down(channel_id, reply_user_id):
        return
    if await is_mention_logged(channel_id, message.message_id, reply_user_id):
        return

    await log_mention(
        channel_id=channel_id,
        message_id=message.message_id,
        mentioned_chat_id=reply_user_id,
        mention_type='reply',
        draft_sent=False,
    )

    lang = intern.get('language', 'ru')
    channel_title = message.chat.title or str(channel_id)
    author = _format_author(message.from_user)
    original_text = (message.reply_to_message.text or '')[:200]
    reply_text = (message.text or '')[:500]

    text = t('channels.reply_notification', lang,
             channel=channel_title,
             original_text=original_text,
             author=author,
             reply_text=reply_text)

    try:
        await bot.send_message(reply_user_id, text, parse_mode='HTML')
    except Exception as e:
        logger.warning(f"[SC.118] Failed to send reply notification to {reply_user_id}: {e}")


async def _send_simple_notification(message: Message, bot: Bot, match: MentionMatch):
    """Отправить простое уведомление об упоминании."""
    lang = coerce_ui_lang(match.monitor.get('language'))  # WP-440: not via get_intern
    channel_title = message.chat.title or str(message.chat.id)
    author = _format_author(message.from_user)
    msg_text = (message.text or '')[:500]

    if match.mention_type == 'reply':
        original_text = ''
        if message.reply_to_message and message.reply_to_message.text:
            original_text = message.reply_to_message.text[:200]
        text = t('channels.reply_notification', lang,
                 channel=channel_title,
                 original_text=original_text,
                 author=author,
                 reply_text=msg_text)
    else:
        text = t('channels.mention_notification', lang,
                 channel=channel_title,
                 author=author,
                 message=msg_text)

    try:
        await bot.send_message(match.chat_id, text, parse_mode='HTML')
    except Exception as e:
        logger.warning(f"[SC.118] Failed to send mention notification to {match.chat_id}: {e}")




def _format_author(user) -> str:
    """Форматировать имя автора сообщения."""
    if not user:
        return 'Аноним'
    parts = []
    if user.first_name:
        parts.append(user.first_name)
    if user.last_name:
        parts.append(user.last_name)
    return ' '.join(parts) if parts else (user.username or 'Аноним')
