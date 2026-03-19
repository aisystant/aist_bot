"""
Детектор упоминаний в каналах (SC.118).

Проверяет сообщение на наличие упоминаний отслеживаемых пользователей:
1. @username — entity типа mention
2. reply на сообщение пользователя
3. Имя пользователя в тексте
"""

import re
from dataclasses import dataclass
from typing import Optional

from aiogram.types import Message

from config import get_logger

logger = get_logger(__name__)


@dataclass
class MentionMatch:
    """Результат детекции упоминания."""
    chat_id: int
    mention_type: str  # 'username' | 'reply' | 'name'
    monitor: dict  # Данные из channel_monitors
    is_admin: bool  # Владелец/админ → черновик, иначе → уведомление


def detect_mentions(message: Message, monitors: list[dict]) -> list[MentionMatch]:
    """Проверить сообщение на упоминания пользователей из monitors.

    Args:
        message: Сообщение из канала/группы
        monitors: Список активных мониторов для этого канала

    Returns:
        Список найденных упоминаний (без дубликатов по chat_id)
    """
    if not monitors or not message.text:
        return []

    matches: dict[int, MentionMatch] = {}  # chat_id → match (первый wins)
    text_lower = message.text.lower()

    # Собираем @usernames из entities
    mentioned_usernames: set[str] = set()
    if message.entities:
        for entity in message.entities:
            if entity.type == 'mention':
                # @username — извлекаем без @
                username = message.text[entity.offset + 1:entity.offset + entity.length]
                mentioned_usernames.add(username.lower())
            elif entity.type == 'text_mention' and entity.user:
                # text_mention — упоминание без @username (по user_id)
                for monitor in monitors:
                    if monitor['chat_id'] == entity.user.id and monitor['track_username']:
                        if monitor['chat_id'] not in matches:
                            matches[monitor['chat_id']] = MentionMatch(
                                chat_id=monitor['chat_id'],
                                mention_type='username',
                                monitor=monitor,
                                is_admin=monitor.get('is_admin', False),
                            )

    for monitor in monitors:
        chat_id = monitor['chat_id']
        if chat_id in matches:
            continue

        # 1. @username match
        if monitor['track_username'] and monitor.get('tg_username'):
            if monitor['tg_username'].lower() in mentioned_usernames:
                matches[chat_id] = MentionMatch(
                    chat_id=chat_id,
                    mention_type='username',
                    monitor=monitor,
                    is_admin=monitor.get('is_admin', False),
                )
                continue

        # 2. Reply match
        if monitor['track_reply'] and message.reply_to_message:
            reply_from = message.reply_to_message.from_user
            if reply_from and reply_from.id == chat_id:
                matches[chat_id] = MentionMatch(
                    chat_id=chat_id,
                    mention_type='reply',
                    monitor=monitor,
                    is_admin=monitor.get('is_admin', False),
                )
                continue

        # 3. Name match (whole word, case-insensitive)
        if monitor['track_name'] and monitor.get('name'):
            name = monitor['name'].strip()
            if len(name) >= 3:  # Минимум 3 символа чтобы избежать ложных срабатываний
                # Экранируем спецсимволы regex и ищем как целое слово
                pattern = r'\b' + re.escape(name.lower()) + r'\b'
                if re.search(pattern, text_lower):
                    matches[chat_id] = MentionMatch(
                        chat_id=chat_id,
                        mention_type='name',
                        monitor=monitor,
                        is_admin=monitor.get('is_admin', False),
                    )

    return list(matches.values())
