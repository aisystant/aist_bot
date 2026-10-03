"""WP-392 Ф3.1 / WP-392 retirement (05.09): Hermes-роутер.

Внешний Hermes-рантайм (Nous Research) отключён от чата бота (решение пилота
05.09 — см. DS-my-strategy/archive/wp-contexts/WP-392/retirement.md): реальный
трафик был единицы диалогов за 2,5 недели, а РП-262 («бот = тонкий клиент
ядра») сделает саму раздачу T4→Hermes не нужной архитектурно.

Префикс «Гермес»/«hermes» для tier ≥ T3 теперь отвечает `_HERMES_RETIRED_MSG`
вместо вызова gateway-mcp `hermes_chat`. Для tier < T3 префикс по-прежнему
маршрутизирует к Проводнику (DP.SC.169) — это ЛОКАЛЬНЫЙ Haiku-помощник по
онбордингу, не внешний Hermes, и его отключение не входит в это решение.

Вне scope этого файла — не трогать: `handlers/byok.py` (свой ключ для Hermes)
и `/agent hermes` в `handlers/external_session.py` (движок VS Code-моста) —
отдельные, менее используемые возможности, решение по ним не принято.

Имя «Гермес»/«Hermes» зарезервировано за продуктом Nous Research. Наши собственные
артефакты (Claude-сессии, Session Memory Injector и т.д.) так НЕ называются.
"""

import logging
import re

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from config.settings import CLAUDE_MODEL_HAIKU
from db.queries import get_intern
from helpers.typing_indicator import keep_typing

logger = logging.getLogger(__name__)

hermes_router = Router(name="hermes")

_HERMES_PREFIXES = ("гермес", "hermes")
_HERMES_PREFIXES_RE = re.compile(r"^(гермес|hermes)[,:\s]+", re.IGNORECASE)
_TIER_REQUIRED = 3
_UNAVAILABLE_TIER_MSG = "Функция недоступна на твоём тире"
# РП7 BOT-HERMES1: имя рантайма наружу не показываем. Два разных отказа —
# «нет токена» (нужен повторный вход, ждать бесполезно) и «рантайм упал»
# (нейтральное «попробуй позже»). Используется VS Code-мостом (external_session.py),
# где Hermes-исполнитель отключением из этого файла не затронут.
_SERVICE_DOWN_MSG = "Помощник сейчас недоступен. Попробуй позже."
_RECONNECT_MSG = (
    "Похоже, доступ к платформе прервался: сессия входа истекла. "
    "Нажми кнопку ниже и снова войди в аккаунт Aisystant, потом повтори сообщение."
)
_RECONNECT_BTN = "🔗 Войти заново"
_CONDUCTOR_UNAVAILABLE_MSG = "Проводник временно недоступен. Напиши /setup — там весь путь."
_CONDUCTOR_MAX_TOKENS = 500
_HERMES_RETIRED_MSG = (
    "Гермес как отдельный помощник отключён. Просто напиши свой вопрос обычным "
    "сообщением (или начни с «?») — ответит Наставник."
)

_TIER_LABELS = {
    0: "T0 (анонимный)",
    1: "T1 (аккаунт подключён, без подписки)",
    2: "T2 (подписка активна)",
    3: "T3 (браузерная среда IWE)",
    4: "T4 (полное окружение с VS Code)",
}


def _conductor_system_prompt(tier_num: int) -> str:
    tier_label = _TIER_LABELS.get(tier_num, f"T{tier_num}")
    return (
        "Ты Проводник — помощник по подключению к платформе Aisystant.\n\n"
        f"Текущий уровень пользователя: {tier_label}\n\n"
        "Путь оснащения: T0 (без аккаунта) → T1 (аккаунт подключён) → "
        "T2 (подписка «Инженерия интеллекта») → T3 (браузерная среда IWE) → "
        "T4 (полное окружение с VS Code).\n\n"
        "СТРОГИЙ SCOPE — отвечай ТОЛЬКО на вопросы:\n"
        "1. Что означает каждый тир и что он даёт\n"
        "2. Как перейти на следующий тир (какие действия нужны)\n"
        "3. Какие команды и функции доступны на текущем тире\n"
        "4. Общий обзор платформы: марафон, диагностика, личная база знаний\n\n"
        "Если вопрос вне scope: ответь «Я помогаю с подключением к платформе. "
        "Для других вопросов напиши «?» и свой вопрос — ответит Наставник.\"\n\n"
        "Отвечай по-русски, кратко (3-5 предложений). Будь дружелюбным и конкретным."
    )


def _is_hermes_message(message: Message) -> bool:
    """Текстовое сообщение с префиксом «Гермес»/«hermes» (не из канала/группы)."""
    if message.chat.type in ("channel", "group", "supergroup"):
        return False
    text = (message.text or "").lower().lstrip()
    return text.startswith(_HERMES_PREFIXES)


# Онбординг-релевантные запросы: только на них Проводник предлагает кнопку
# «Освоиться» (вход Онбордера). На частные вопросы («какой день марафона»)
# кнопку НЕ навязываем — Гермес остаётся Q&A, а не push (консенсус 2026-06-11-20).
_ONBOARDING_QUERY_KEYWORDS = (
    "начать", "с чего", "как устроен", "ориентац", "первый шаг",
    "как тут", "что здесь", "что тут", "куда идти", "освоит", "с нуля",
)


def _is_onboarding_query(text: str) -> bool:
    """Вопрос про вход в сообщество (а не частность) — по ключевым словам."""
    low = (text or "").lower()
    return any(kw in low for kw in _ONBOARDING_QUERY_KEYWORDS)


async def _send_reconnect_prompt(message: Message) -> None:
    """Сессия входа истекла (нет Ory-токена) → предложить повторный вход.

    Ждать бесполезно: пропавший токен сам не вернётся. Шлём deep-link
    повторной авторизации Aisystant с кнопкой — тем же приёмом, что twin.py /
    guide.py. Имя рантайма пользователю не показываем.
    """
    from clients.ory_oauth import ory_oauth
    auth_url, _state = await ory_oauth.get_authorization_url(message.chat.id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_RECONNECT_BTN, url=auth_url)],
    ])
    await message.answer(_RECONNECT_MSG, reply_markup=keyboard)


async def _send_unavailable(message: Message, placeholder: Message | None, chat_id: int) -> None:
    """Объяснить, почему помощник не ответил, не называя рантайм.

    Нет Ory-токена (вход отвалился) → предложить повторный вход (ждать
    бесполезно). Токен есть, но рантайм/шлюз упал → нейтральное «попробуй позже».
    """
    if placeholder is not None:
        try:
            await placeholder.delete()
        except Exception:
            logger.debug("[hermes] placeholder delete failed for chat %s", chat_id)
    from clients.gateway_mcp import gateway_mcp
    if not gateway_mcp.is_connected(chat_id):
        await _send_reconnect_prompt(message)
    else:
        await message.answer(_SERVICE_DOWN_MSG)


@hermes_router.message(_is_hermes_message)
async def on_hermes(message: Message, state: FSMContext) -> None:
    """«Гермес, <текст>»: tier < T3 → Проводник (Haiku, локальный). tier ≥ T3 →
    отдельный ИИ-помощник отключён (05.09), отвечаем `_HERMES_RETIRED_MSG`.

    WP-392 Ф3.1: префикс «Гермес» имеет абсолютный приоритет.
    SkipHandler ТОЛЬКО если marathon SM ждёт ответ (не ломать марафон).
    Не-онбордированным показываем отказ tier — не пропускаем в fallback.
    """
    if message.from_user is None:  # per-user; skip anonymous posts
        return
    chat_id = message.chat.id
    text = message.text or ""

    intern = await get_intern(chat_id)

    # Не перехватывать у SM, ожидающей ответ (марафон, фиксация дайджеста и др.)
    from handlers.external_session import _sm_is_expecting_reply
    from states.feed.digest import FeedDigestState
    if await _sm_is_expecting_reply(chat_id) or FeedDigestState.is_waiting_fixation(chat_id):
        logger.info("[hermes] SM or feed expecting reply for chat %s — skipping", chat_id)
        raise SkipHandler

    if not intern or not intern.get("onboarding_completed"):
        await message.answer(_UNAVAILABLE_TIER_MSG)
        return

    # Используем detect_ui_tier для актуального тира, а не устаревшее поле из БД.
    # get_intern читает public.users.tier, которое обновляется только при переходах тира
    # через detect_ui_tier. Если тир менялся (подключили GitHub/ЦД) между деплоями,
    # DB-поле может быть stale — тогда Проводник неверно отказывает T4-пользователям.
    from core.tier_detector import detect_ui_tier
    tier = await detect_ui_tier(chat_id)
    logger.info(
        "[hermes] chat_id=%s tier=%s required=%s",
        chat_id, tier, _TIER_REQUIRED,
    )

    if tier >= _TIER_REQUIRED:
        await message.answer(_HERMES_RETIRED_MSG)
        return

    # DP.SC.169 deprecated (WP-406 Ф7) → поглощён Онбордером (DP.SC.170).
    # Гибрид (WP-406 Ф5): живой Haiku-ответ Проводника на вопрос + кнопка
    # «Освоиться» (вход Онбордера) только на онбординг-релевантные запросы.
    # Этот путь — локальный Haiku-вызов, не внешний Hermes; отключением не затронут.
    query = _HERMES_PREFIXES_RE.sub("", text).strip() or text
    from clients.claude import claude
    try:
        async with keep_typing(message):
            response = await claude.generate(
                system_prompt=_conductor_system_prompt(tier),
                user_prompt=query,
                max_tokens=_CONDUCTOR_MAX_TOKENS,
                model=CLAUDE_MODEL_HAIKU,
                allow_partial=True,
            )
    except Exception:
        logger.exception("[hermes:conductor] Haiku call failed for chat %s", chat_id)
        response = None
    await message.answer(
        f"Проводник: {response}" if response else _CONDUCTOR_UNAVAILABLE_MSG,
        parse_mode="Markdown",
    )
    await _maybe_offer_onboarder_after_conductor(message, chat_id, query)


async def _maybe_offer_onboarder_after_conductor(message: Message, chat_id: int, query: str) -> None:
    """После ответа Проводника (tier<T3) предложить вход Онбордера.

    Двойной гейт: (1) вопрос про вход в сообщество (_is_onboarding_query), иначе
    Гермес остаётся Q&A без push; (2) есть открытый разрыв Х2/Х3 и не на cooldown
    (offer.should_offer). Отрисовка кнопки из общего payload — без дублирования
    с onboarding.py.
    """
    if not _is_onboarding_query(query):
        return
    from core.onboarder import offer
    try:
        if not await offer.should_offer(chat_id):
            return
    except Exception as e:
        logger.warning("[hermes] should_offer check failed for %s: %s", chat_id, e)
        return
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    payload = offer.offer_payload()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=payload["button_text"], callback_data=payload["callback_data"]),
    ]])
    await message.answer(payload["text"], reply_markup=kb)
    try:
        await offer.mark_offered(chat_id)
    except Exception as e:
        logger.warning("[hermes] failed to record offer timestamp for %s: %s", chat_id, e)
