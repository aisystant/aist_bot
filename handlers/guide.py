"""
Гид — персональный помощник в обучении (WP-79).

Переименованная точка входа к Digital Twin с новым UX:
- T2: приглашение подключить ЦД (показать ценность)
- T3: дашборд (профиль, цели, ступени)
- T4: полноценный компаньон

Команда: /guide (кнопка «🧭 Гид»)
Делегирует в clients.digital_twin для работы с данными.
"""

import logging

from aiogram import Router, F
from helpers.typing_indicator import keep_typing
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.filters import Command

from db.queries import get_intern
from core.tier_config import UITier
from core.tier_detector import detect_ui_tier
from i18n import t

logger = logging.getLogger(__name__)

guide_router = Router(name="guide")


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


@guide_router.message(Command("guide"))
async def cmd_guide(message: Message):
    """Команда /guide — точка входа в Гид."""
    chat_id = message.chat.id
    intern = await get_intern(chat_id)
    lang = _lang(intern)
    tier = await detect_ui_tier(chat_id)

    # T2: приглашение подключить ЦД
    if tier <= UITier.T2_LEARNING:
        from clients.gateway_mcp import gateway_mcp
        is_connected = gateway_mcp.is_connected(chat_id)

        if not is_connected:
            from core.access import access_layer
            if not await access_layer.has_gateway_access(chat_id):
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=t('aisystant_sub.btn_subscribe', lang), callback_data="aisystant_subscribe")]
                ])
                await message.answer(t('twin.br_paywall', lang), parse_mode="Markdown", reply_markup=keyboard)
                return
            from clients.ory_oauth import ory_oauth
            auth_url, _state = await ory_oauth.get_authorization_url(chat_id)
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('guide.btn_connect_dt', lang), url=auth_url)],
            ])
            await message.answer(
                f"{t('guide.title', lang)}\n\n{t('guide.desc_t2', lang)}",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            return

    # T3+: показываем профиль ЦД
    await _show_profile(message, chat_id, lang)


async def _show_profile(message: Message, chat_id: int, lang: str):
    """Показать профиль Digital Twin через Гид."""
    from clients.gateway_mcp import gateway_mcp
    from handlers.twin import _profile_text

    intern = await get_intern(chat_id)

    if not gateway_mcp.is_connected(chat_id):
        # Проверяем persistent flag
        try:
            from db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    'SELECT dt_connected_at FROM public.users WHERE telegram_id = $1', chat_id,
                )
                if row and row['dt_connected_at'] is not None:
                    await message.answer(t('twin.reconnect_needed', lang), parse_mode="Markdown")
                    return
        except Exception:
            pass

        # Не подключён — проверяем подписку, затем показываем приглашение
        from core.access import access_layer
        if not await access_layer.has_gateway_access(chat_id):
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('aisystant_sub.btn_subscribe', lang), callback_data="aisystant_subscribe")]
            ])
            await message.answer(t('twin.br_paywall', lang), parse_mode="Markdown", reply_markup=keyboard)
            return
        from clients.ory_oauth import ory_oauth
        auth_url, _state = await ory_oauth.get_authorization_url(chat_id)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t('guide.btn_connect_dt', lang), url=auth_url)],
        ])
        await message.answer(
            f"{t('guide.title', lang)}\n\n{t('guide.desc_t2', lang)}",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return

    # Consent gate (WP-7 Ф-DT-Consent-Pipeline): без opt-in сборщик не наполняет
    # двойника — вместо вечно пустой карточки ведём на экран согласия.
    # Fail-open: сбой проверки не блокирует показ профиля.
    try:
        from db.queries.consent import get_consent
        from helpers.dual_write import resolve_ory_id_from_chat
        ory_id = await resolve_ory_id_from_chat(chat_id)
        if ory_id:
            consent = await get_consent(ory_id)
            if not consent or not consent["opt_in"]:
                from handlers.consent import show_consent_optin
                await show_consent_optin(message, chat_id=chat_id)
                return
    except Exception as e:
        logger.error(f"[guide] consent-gap check failed for {chat_id}: {e}")

    await message.answer(t('guide.loading', lang))
    async with keep_typing(message):
        profile, reason = await gateway_mcp.get_user_profile_ex(chat_id)
    if profile is None:
        # Согласие есть, но двойник ещё не собран (наполнится ночным сборщиком) —
        # это не сбой, отличаем от реальной недоступности шлюза.
        key = 'guide.collecting' if reason == 'empty' else 'guide.unavailable'
        await message.answer(t(key, lang))
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('guide.btn_update', lang), callback_data="twin_profile")],
        [InlineKeyboardButton(text=t('guide.btn_degrees', lang), callback_data="twin_degrees")],
        [InlineKeyboardButton(text=t('guide.btn_settings', lang), callback_data="twin_disconnect")],
    ])

    # Используем profile_text из twin.py, но с заголовком Гида
    text = _profile_text(profile, lang, intern=intern)
    # Заменяем заголовок ЦД на Гид
    old_title = t('twin.profile_title', lang)
    text = text.replace(f"*{old_title}*", t('guide.profile_header', lang), 1)

    await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)
