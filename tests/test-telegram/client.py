"""
Telegram Test Client - обёртка над Telethon для E2E тестирования.

Позволяет:
- Отправлять сообщения боту
- Ждать ответа с таймаутом
- Нажимать inline-кнопки
- Проверять текст и клавиатуры
"""

import asyncio
import os
from typing import Optional, List, Callable
from dataclasses import dataclass

from telethon import TelegramClient
from telethon.tl.types import (
    Message,
    ReplyInlineMarkup,
    KeyboardButtonCallback,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telethon.tl.custom import Button


@dataclass
class BotResponse:
    """Ответ бота"""
    text: str
    message: Message
    inline_buttons: List[List[dict]] = None
    reply_buttons: List[List[str]] = None

    def has_text(self, substring: str) -> bool:
        """Проверяет, содержит ли ответ подстроку"""
        return substring.lower() in self.text.lower()

    def has_button(self, text: str) -> bool:
        """Проверяет наличие кнопки с текстом"""
        if self.inline_buttons:
            for row in self.inline_buttons:
                for btn in row:
                    if text.lower() in btn.get('text', '').lower():
                        return True
        if self.reply_buttons:
            for row in self.reply_buttons:
                for btn_text in row:
                    if text.lower() in btn_text.lower():
                        return True
        return False

    def get_button_data(self, text: str) -> Optional[bytes]:
        """Получает callback_data кнопки по тексту"""
        if self.inline_buttons:
            for row in self.inline_buttons:
                for btn in row:
                    if text.lower() in btn.get('text', '').lower():
                        return btn.get('data')
        return None


class BotTestClient:
    """Клиент для E2E тестирования Telegram бота"""

    def __init__(
        self,
        api_id: int = None,
        api_hash: str = None,
        session_name: str = "test_session",
        bot_username: str = None,
    ):
        self.api_id = api_id or int(os.getenv("TEST_API_ID", "0"))
        self.api_hash = api_hash or os.getenv("TEST_API_HASH", "")
        self.session_name = session_name
        self.bot_username = bot_username or os.getenv("TEST_BOT_USERNAME", "")

        self.client: Optional[TelegramClient] = None
        self.bot_entity = None

    async def start(self):
        """Запускает клиент и находит бота"""
        self.client = TelegramClient(
            self.session_name,
            self.api_id,
            self.api_hash,
        )
        await self.client.start()

        # Находим бота
        self.bot_entity = await self.client.get_entity(self.bot_username)
        return self

    async def stop(self):
        """Останавливает клиент"""
        if self.client:
            await self.client.disconnect()

    async def send(self, text: str) -> Message:
        """Отправляет сообщение боту"""
        return await self.client.send_message(self.bot_entity, text)

    async def send_command(self, command: str) -> Message:
        """Отправляет команду боту (добавляет / если нужно)"""
        if not command.startswith('/'):
            command = f'/{command}'
        return await self.client.send_message(self.bot_entity, command)

    async def wait_response(
        self,
        timeout: float = 10.0,
        count: int = 1,
        filter_fn: Callable[[Message], bool] = None,
    ) -> List[BotResponse]:
        """
        Ждёт ответ(ы) от бота.

        Args:
            timeout: максимальное время ожидания в секундах
            count: количество ожидаемых сообщений
            filter_fn: функция фильтрации сообщений

        Returns:
            Список BotResponse
        """
        responses = []
        loop = asyncio.get_running_loop()
        start_time = loop.time()

        while len(responses) < count:
            elapsed = loop.time() - start_time
            if elapsed >= timeout:
                break

            # Получаем новые сообщения
            messages = await self.client.get_messages(
                self.bot_entity,
                limit=count * 2,  # Берём с запасом
            )

            for msg in messages:
                if msg.out:  # Пропускаем свои сообщения
                    continue

                if filter_fn and not filter_fn(msg):
                    continue

                response = self._parse_message(msg)
                if response not in responses:
                    responses.append(response)

                if len(responses) >= count:
                    break

            if len(responses) < count:
                await asyncio.sleep(0.5)

        return responses

    async def wait_for_text(
        self,
        substring: str,
        timeout: float = 10.0,
    ) -> Optional[BotResponse]:
        """Ждёт сообщение, содержащее подстроку"""
        responses = await self.wait_response(
            timeout=timeout,
            count=1,
            filter_fn=lambda m: substring.lower() in (m.text or '').lower()
        )
        return responses[0] if responses else None

    async def click_button(
        self,
        response: BotResponse,
        button_text: str,
    ) -> bool:
        """Нажимает inline-кнопку в сообщении"""
        if not response.inline_buttons:
            return False

        for row in response.inline_buttons:
            for btn in row:
                if button_text.lower() in btn.get('text', '').lower():
                    # Нажимаем кнопку через Telethon
                    await response.message.click(data=btn.get('data'))
                    return True
        return False

    async def send_and_wait(
        self,
        text: str,
        timeout: float = 10.0,
        wait_count: int = 1,
    ) -> List[BotResponse]:
        """Отправляет сообщение и ждёт ответ(ы)"""
        await self.send(text)
        await asyncio.sleep(0.3)  # Небольшая пауза
        return await self.wait_response(timeout=timeout, count=wait_count)

    async def command_and_wait(
        self,
        command: str,
        timeout: float = 10.0,
        wait_count: int = 1,
    ) -> List[BotResponse]:
        """Отправляет команду и ждёт ответ(ы)"""
        await self.send_command(command)
        await asyncio.sleep(0.3)
        return await self.wait_response(timeout=timeout, count=wait_count)

    def _parse_message(self, msg: Message) -> BotResponse:
        """Парсит сообщение в BotResponse"""
        inline_buttons = None
        reply_buttons = None

        # Парсим inline-кнопки
        if msg.reply_markup and isinstance(msg.reply_markup, ReplyInlineMarkup):
            inline_buttons = []
            for row in msg.reply_markup.rows:
                btn_row = []
                for btn in row.buttons:
                    if isinstance(btn, KeyboardButtonCallback):
                        btn_row.append({
                            'text': btn.text,
                            'data': btn.data,
                        })
                    else:
                        btn_row.append({'text': getattr(btn, 'text', str(btn))})
                inline_buttons.append(btn_row)

        # Парсим reply-кнопки
        if msg.reply_markup and isinstance(msg.reply_markup, ReplyKeyboardMarkup):
            reply_buttons = []
            for row in msg.reply_markup.rows:
                btn_row = []
                for btn in row.buttons:
                    if isinstance(btn, KeyboardButton):
                        btn_row.append(btn.text)
                reply_buttons.append(btn_row)

        return BotResponse(
            text=msg.text or '',
            message=msg,
            inline_buttons=inline_buttons,
            reply_buttons=reply_buttons,
        )

    async def clear_chat(self):
        """Очищает историю чата с ботом (для чистого старта тестов)"""
        messages = await self.client.get_messages(self.bot_entity, limit=100)
        if messages:
            await self.client.delete_messages(self.bot_entity, messages)


# Контекстный менеджер для удобства
class BotTest:
    """Контекстный менеджер для E2E тестов"""

    def __init__(self, **kwargs):
        self.client = BotTestClient(**kwargs)

    async def __aenter__(self) -> BotTestClient:
        await self.client.start()
        return self.client

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.stop()
