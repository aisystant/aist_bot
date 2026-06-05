"""WP-349 Ф29: BYOK — управление пользовательскими LLM-ключами.

Команды (T4):
  /byok          — список ключей
  /byok add <provider> <key>   — добавить ключ
  /byok remove <key_id>        — отозвать ключ
  /byok help                   — справка

Доступ: только T4 (проверяется через gateway tier).
"""

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from clients import gateway_mcp

logger = logging.getLogger(__name__)

byok_router = Router(name="byok")

_PROVIDERS = ("anthropic", "openai", "google", "deepseek")

_PROVIDER_DISPLAY = {
    "anthropic": "Anthropic (Claude)",
    "openai": "OpenAI (GPT)",
    "google": "Google (Gemini)",
    "deepseek": "DeepSeek",
}

_HELP_TEXT = (
    "<b>Управление LLM-ключами (BYOK)</b>\n\n"
    "Доступные команды:\n"
    "  /byok — список сохранённых ключей\n"
    "  /byok add &lt;провайдер&gt; &lt;ключ&gt; — добавить ключ\n"
    "  /byok remove &lt;key_id&gt; — отозвать ключ\n"
    "  /byok help — эта справка\n\n"
    "Провайдеры: anthropic | openai | google | deepseek\n\n"
    "Пример:\n"
    "<code>/byok add anthropic sk-ant-api03-...</code>"
)


@byok_router.message(Command("byok"))
async def cmd_byok(message: Message) -> None:
    """Главный хендлер /byok — диспетчер по субкоманде."""
    chat_id = message.chat.id
    args = (message.text or "").split(maxsplit=2)
    sub = args[1].lower() if len(args) > 1 else "list"

    if sub == "help":
        await message.answer(_HELP_TEXT, parse_mode="HTML")
        return

    if sub == "add":
        await _handle_add(message, chat_id, args)
        return

    if sub == "remove":
        await _handle_remove(message, chat_id, args)
        return

    # По умолчанию — list
    await _handle_list(message, chat_id)


async def _handle_list(message: Message, chat_id: int) -> None:
    keys = await gateway_mcp.list_llm_keys(chat_id)
    if keys is None:
        await message.answer(
            "Не удалось получить список ключей. Убедись, что ты авторизован (T4).",
            parse_mode="HTML",
        )
        return

    if not keys:
        await message.answer(
            "У тебя пока нет сохранённых LLM-ключей.\n\n"
            "Добавить: <code>/byok add anthropic sk-ant-api03-...</code>",
            parse_mode="HTML",
        )
        return

    lines = ["<b>Твои LLM-ключи:</b>\n"]
    for k in keys:
        provider = k.get("provider", "?")
        masked = k.get("masked_key", "****")
        label = k.get("label") or ""
        key_id = k.get("key_id", "")
        default_mark = " ✓" if k.get("is_default") else ""
        label_part = f" ({label})" if label else ""
        lines.append(
            f"• {_PROVIDER_DISPLAY.get(provider, provider)}{default_mark}{label_part}\n"
            f"  <code>{masked}</code>\n"
            f"  ID: <code>{key_id}</code>"
        )

    lines.append("\nОтозвать: <code>/byok remove &lt;key_id&gt;</code>")
    await message.answer("\n".join(lines), parse_mode="HTML")


async def _handle_add(message: Message, chat_id: int, args: list) -> None:
    # /byok add <provider> <key>
    if len(args) < 4:
        # Попробуем разобрать как /byok add <provider> <key> (args split maxsplit=2 → max 3 parts)
        # args[2] может содержать "<provider> <key>"
        if len(args) == 3:
            parts = args[2].split(maxsplit=1)
            if len(parts) == 2:
                provider, api_key = parts[0].lower(), parts[1]
            else:
                await message.answer(
                    "Формат: <code>/byok add &lt;провайдер&gt; &lt;ключ&gt;</code>\n"
                    f"Провайдеры: {' | '.join(_PROVIDERS)}",
                    parse_mode="HTML",
                )
                return
        else:
            await message.answer(
                "Формат: <code>/byok add &lt;провайдер&gt; &lt;ключ&gt;</code>\n"
                f"Провайдеры: {' | '.join(_PROVIDERS)}",
                parse_mode="HTML",
            )
            return
    else:
        provider, api_key = args[2].lower(), args[3]

    if provider not in _PROVIDERS:
        await message.answer(
            f"Неизвестный провайдер: <code>{provider}</code>\n"
            f"Доступные: {' | '.join(_PROVIDERS)}",
            parse_mode="HTML",
        )
        return

    result = await gateway_mcp.grant_llm_key(chat_id, provider=provider, api_key=api_key, is_default=True)
    if not result:
        await message.answer(
            "Не удалось сохранить ключ. Проверь формат ключа и уровень доступа (T4).",
            parse_mode="HTML",
        )
        return

    if result.get("error"):
        await message.answer(
            f"Ошибка: {result['error']}",
            parse_mode="HTML",
        )
        return

    masked = result.get("masked_key", "****")
    provider_display = _PROVIDER_DISPLAY.get(provider, provider)
    await message.answer(
        f"Ключ {provider_display} сохранён.\n"
        f"Маска: <code>{masked}</code>\n\n"
        "Теперь Hermes будет использовать твой ключ вместо платформенного.",
        parse_mode="HTML",
    )


async def _handle_remove(message: Message, chat_id: int, args: list) -> None:
    # /byok remove <key_id>
    key_id = args[2].strip() if len(args) > 2 else ""
    if not key_id:
        await message.answer(
            "Формат: <code>/byok remove &lt;key_id&gt;</code>\n"
            "Список ключей с ID: /byok",
            parse_mode="HTML",
        )
        return

    result = await gateway_mcp.revoke_llm_key(chat_id, key_id=key_id)
    if not result:
        await message.answer(
            "Не удалось отозвать ключ. Проверь ID и уровень доступа (T4).",
            parse_mode="HTML",
        )
        return

    if result.get("error"):
        await message.answer(f"Ошибка: {result['error']}", parse_mode="HTML")
        return

    provider = result.get("provider", "?")
    provider_display = _PROVIDER_DISPLAY.get(provider, provider)
    fallback_note = (
        "\n\nХерmes вернётся к платформенному ключу."
        if result.get("fallback_to_platform")
        else ""
    )
    await message.answer(
        f"Ключ {provider_display} отозван.{fallback_note}",
        parse_mode="HTML",
    )
