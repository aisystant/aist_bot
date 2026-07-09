"""
Х2 — понимание сообщества: мини-тест teach-confirm (WP-406 Ф5).

# see DP.SC.170, DP.ROLE.067

Х2 закрывается структурированным мини-тестом из 4 фиксированных вопросов
(карточка WP-406, критерий строки 122). Режим — teach-confirm («дай ориентацию →
подтверди»), БЕЗ fail-state: по инварианту обещания «Онбордер не бросает»
(DP.SC.170). Низкий ответ не «проваливает» человека — кнопка «Подробнее»
доставляет недостающий кусок, дальше тот же шаг. Отметка x2_completed_at
ставится по факту прохождения всех четырёх пунктов (storage.mark_x2_done),
а не по порогу баллов.

Архитектура (консенсус peer-сессии 2026-06-11-16):
  - run_step показывает первый неподтверждённый пункт из X2_TOPICS с кнопками
    «Понятно» / «Подробнее». Набор подтверждённых пунктов — в
    current_context['onboarding']['x2_confirmed'] (переживает redeploy).
  - confirm_topic фиксирует пункт и сразу показывает следующий (или завершает).
  - На 4/4 — mark_x2_done + поздравление + кнопка перехода к выбору курса (Х3),
    если он ещё открыт. Сам Х3 запускается тем же входом Онбордера (handle),
    а не из этого модуля — explicit-триггер, не авто-перехват.

Тексты ориентации — на русском прямо в модуле (как соседние Экран A/B в
handlers/onboarding.py и core/onboarder/x3.py). Вынос в i18n — follow-up.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Четыре пункта понимания сообщества (карточка WP-406, строка 122).
# Ключи — стабильные идентификаторы шага (порядок = порядок показа).
X2_TOPICS = (
    "community",      # (а) что такое IWE-сообщество
    "roles",          # (б) какие роли есть
    "norms",          # (в) нормы коммуникации
    "where_to_ask",   # (г) куда обращаться за помощью
)

_CONFIRMED_KEY = "x2_confirmed"
_INTRO_SHOWN_KEY = "x2_intro_shown"

# WP-406 Ф19: вступление перед первым топиком (порядок «зачем→что»).
# Показывается один раз за прохождение, отдельным сообщением перед первым топиком.
_INTRO_TEXT = (
    "Сейчас 4 коротких пункта — быстрая ориентация по сообществу. Без оценок: "
    "если что-то непонятно, нажми «Подробнее». После — перейдём к выбору направления."
)

# Короткая ориентация по пункту (показывается первой).
_TOPIC_TEXT = {
    "community": (
        "<b>Что это за место</b>\n"
        "Здесь не учат — здесь работают. Ты решаешь свои реальные задачи вместе "
        "с ИИ, а метод осваивается прямо в деле, а не отдельным курсом до начала "
        "работы. Рядом — сообщество людей, которые работают так же."
    ),
    "roles": (
        "<b>Что растёт само, а что решаешь ты</b>\n"
        "Доступ к инструментам открывается по ходу дела — настраивать это "
        "самому не нужно. А куда двигаться содержательно — решаешь ты сам: "
        "если забуксуешь, система заметит и подскажет, но направление "
        "выбираешь ты, это и есть твоя работа."
    ),
    "norms": (
        "<b>Как здесь общаются</b>\n"
        "Просто и по делу. Можно задавать любые вопросы — «глупых» нет. "
        "Помогать другим и делиться опытом приветствуется. Уважение к чужому "
        "времени и темпу — базовое правило."
    ),
    "where_to_ask": (
        "<b>Куда обращаться за помощью</b>\n"
        "Застрял — напиши вопрос прямо в чат, я отвечу из базы знаний. Нужен "
        "живой разговор — есть клуб сообщества. Вопросы по платформе — команда "
        "/help. Вернуться сюда всегда можно командой /start."
    ),
}

# Расширение по кнопке «Подробнее» (второй абзац, потом только «Понятно»).
_TOPIC_MORE = {
    "community": (
        "Работа идёт маленькими регулярными шагами, а не по расписанию занятий. "
        "Бот — один из интерфейсов; позже откроются браузерная среда и полное "
        "рабочее окружение."
    ),
    "roles": (
        "Роли — это функции, а не имена для запоминания. Один помогает понять, "
        "куда двигаться, другой — определяет узкое место, третий — собирает "
        "рабочее окружение. Каждый подключается сам, когда становится нужен."
    ),
    "norms": (
        "Если что-то непонятно в самом сообществе — это нормально и поправимо: "
        "спроси. Здесь ценят честный вопрос выше, чем молчаливое согласие."
    ),
    "where_to_ask": (
        "Коротко: вопрос по теме — в чат; живое обсуждение — клуб; "
        "проблема с ботом или платформой — /help. Ничего не сломаешь — "
        "пробуй смело."
    ),
}


def _next_topic(confirmed: list) -> Optional[str]:
    """Первый пункт X2_TOPICS, ещё не подтверждённый. None — если все подтверждены."""
    for topic in X2_TOPICS:
        if topic not in confirmed:
            return topic
    return None


def _build_keyboard(topic: str, more: bool):
    """Клавиатура шага. more=True — пользователь уже видел «Подробнее», даём только «Понятно»."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    confirm = InlineKeyboardButton(text="Понятно ✓", callback_data=f"x2_confirm:{topic}")
    if more:
        return InlineKeyboardMarkup(inline_keyboard=[[confirm]])
    more_btn = InlineKeyboardButton(text="Подробнее", callback_data=f"x2_more:{topic}")
    return InlineKeyboardMarkup(inline_keyboard=[[confirm, more_btn]])


async def _show_topic(bot, chat_id: int, topic: str, more: bool = False) -> None:
    """Показать ориентацию по пункту (или расширение по «Подробнее»)."""
    idx = X2_TOPICS.index(topic) + 1
    body = _TOPIC_MORE[topic] if more else _TOPIC_TEXT[topic]
    text = f"({idx}/{len(X2_TOPICS)}) {body}"
    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=_build_keyboard(topic, more))


async def run_step(intern: dict, message) -> None:
    """Показать следующий неподтверждённый пункт Х2 (или завершить мини-тест).

    Точка входа из Онбордера (handle) и из callback'ов подтверждения.
    """
    from core.onboarder import storage

    chat_id = intern.get("chat_id") or message.chat.id
    ctx = await storage.get_onboarding_context(chat_id)
    confirmed = ctx.get(_CONFIRMED_KEY) or []
    topic = _next_topic(confirmed)
    if topic is None:
        await _finish_x2(message.bot, chat_id)
        return
    if not confirmed and not ctx.get(_INTRO_SHOWN_KEY):
        # Флаг сохраняется ДО отправки: при сбое после save пользователь просто
        # не увидит интро один раз, а не увидит его дважды при повторном run_step.
        await storage.save_onboarding_context(chat_id, {_INTRO_SHOWN_KEY: True})
        await message.bot.send_message(chat_id, _INTRO_TEXT)
    await _show_topic(message.bot, chat_id, topic, more=False)


async def confirm_topic(intern: dict, message, topic: str) -> None:
    """Зафиксировать подтверждённый пункт и показать следующий шаг."""
    from core.onboarder import storage

    if topic not in X2_TOPICS:
        logger.warning("[x2] confirm of unknown topic %r — ignored", topic)
        return
    chat_id = intern.get("chat_id") or message.chat.id
    ctx = await storage.get_onboarding_context(chat_id)
    confirmed = list(ctx.get(_CONFIRMED_KEY) or [])
    if topic not in confirmed:
        confirmed.append(topic)
        await storage.save_onboarding_context(chat_id, {_CONFIRMED_KEY: confirmed})
    await run_step(intern, message)


async def show_more(intern: dict, message, topic: str) -> None:
    """Показать расширенную ориентацию по пункту (кнопка «Подробнее»)."""
    if topic not in X2_TOPICS:
        logger.warning("[x2] more of unknown topic %r — ignored", topic)
        return
    chat_id = intern.get("chat_id") or message.chat.id
    await _show_topic(message.bot, chat_id, topic, more=True)


async def _finish_x2(bot, chat_id: int) -> None:
    """Все 4 пункта подтверждены: поставить отметку + предложить следующий шаг (Х3).

    Защита от повторного клика по устаревшей кнопке «Понятно» последнего пункта:
    если Х2 уже закрыт — поздравление не дублируем (кнопки старых шагов остаются
    кликабельными, см. правило 10.7.4 о stale inline-кнопках).
    """
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from core.onboarder import storage

    status = await storage.get_status(chat_id)
    if status["x2_done"]:
        return
    _x3_done_before = status["x3_done"]
    await storage.mark_x2_done(chat_id)

    # WP-406 Ф17 PR-2: x2_completed event (fire after mark to ensure DB write succeeded)
    from db.queries.events import log_event
    from db.queries.users import get_intern as _get_intern
    _intern = await _get_intern(chat_id)
    _lang = (_intern.get("language", "ru") or "ru") if _intern else "ru"
    _onb_ctx = await storage.get_onboarding_context(chat_id)
    _entry_type = _onb_ctx.get("entry_type", "direct")
    await log_event(chat_id, "x2_completed", {
        "entry_type": _entry_type,
        "lang": _lang,
        "path": "confirm",
    })

    # WP-406 Ф18: Первокурсник достигнут = Х2 и Х3 оба закрыты. Симметричный лог
    # для обратного порядка — handlers/onboarding.py:on_x3_confirm.
    if _x3_done_before:
        await log_event(chat_id, "onboarding_completed", {
            "entry_type": _entry_type,
            "lang": _lang,
            "closed_by": "x2",
        })

    status = await storage.get_status(chat_id)
    text = (
        "🎉 <b>Отлично! Теперь ты понимаешь, как устроено сообщество.</b>\n"
        "Это половина пути Первокурсника."
    )
    if not status["x3_done"]:
        text += " Осталось понять, с чего тебе начать работать."
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="➡️ Узнать, с чего начать", callback_data="onboarder_start"),
        ]])
        await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
    else:
        await bot.send_message(chat_id, text, parse_mode="HTML")
