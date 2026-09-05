"""Unit-тесты WP-498 Ф12 (05.09, решение пилота): роли Наставник/Диагност/Навигатор
доступны всегда и для всех тиров — платный барьер и T4→Hermes редирект остаются
только для generic-вопроса без распознанной роли (дефолтный Консультант).

Покрывает две точки решения (одна и та же лексика _detect_role, не дублируется):
- states.common.consultation._role_exempt_from_paywall — снятие платного барьера
  в enter() консультации.
- handlers.fallback._is_role_addressed_question — снятие T4→Hermes редиректа,
  тестируется напрямую как реальный код, не копия его логики.
"""

import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "[REDACTED-DATABASE-URL]localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

from states.common.consultation import (  # noqa: E402
    _detect_role,
    _is_paywall_exempt,
    _role_exempt_from_paywall,
)
from handlers.fallback import (  # noqa: E402
    _is_concept_naming_request,
    _is_role_addressed_question,
)


# =============================================================================
# _role_exempt_from_paywall — платный барьер (enter())
# =============================================================================

@pytest.mark.parametrize("question", [
    "я застрял на задании",
    "объясни через понятия IWE",
    "с чего начать?",
    "какая у меня ступень",
])
def test_specialist_question_exempt_from_paywall(question):
    assert _role_exempt_from_paywall(question, None) is not None


def test_generic_question_not_exempt():
    assert _role_exempt_from_paywall("расскажи про мотивацию вообще", None) is None
    assert _role_exempt_from_paywall("", None) is None


@pytest.mark.parametrize("role", ["navigator", "diagnostician"])
def test_force_role_always_exempt_regardless_of_question(role):
    assert _role_exempt_from_paywall("", role) == role
    assert _role_exempt_from_paywall("что угодно, не по теме", role) == role


def test_force_role_unknown_value_ignored():
    # force_role вне (navigator, diagnostician) — не считается специалистом,
    # решение падает обратно на текст вопроса.
    assert _role_exempt_from_paywall("расскажи про мотивацию вообще", "something_else") is None


# =============================================================================
# Обход T4→Hermes — handlers.fallback._is_role_addressed_question (реальный код)
# =============================================================================

@pytest.mark.parametrize("text", [
    "? я застрял на задании",
    "? Наставник, какой гейт мы применили?",
    "? какая у меня ступень",
    "? Навигатор, с чего начать?",
])
def test_role_addressed_question_bypasses_t4_redirect(text):
    assert _is_role_addressed_question(text) is True


def test_generic_question_does_not_bypass_t4_redirect():
    assert _is_role_addressed_question("? расскажи про мотивацию вообще") is False


def test_text_without_question_mark_never_bypasses():
    # T4-редирект для текста без "?" не должен трогаться этой веткой вообще —
    # даже если содержимое похоже на роль (это уже поведение WP-392, не Ф12).
    assert _is_role_addressed_question("Наставник, я застрял") is False


# =============================================================================
# Ф15 (АрхГейт, вариант Д): «назови понятие» без «?» у T4 — в бота, не в Hermes
# handlers.fallback._is_concept_naming_request (реальный код)
# =============================================================================

@pytest.mark.parametrize("text", [
    "объясни через понятия IWE, что мы сейчас сделали",
    "какое понятие IWE мы сейчас применили",
    "какой гейт мы применили в этой работе",   # формулировка чек-листа РП-522
    "назови понятие, которое мы применили",
    "Наставник, какой метод я применил?",       # явный префикс = сам выбрал собеседника
    "Наставник, что нового мы сделали",
    "Наставник, какой гейт сработал",
])
def test_concept_naming_without_question_mark_goes_to_bot(text):
    assert _is_concept_naming_request(text) is True


@pytest.mark.parametrize("text", [
    "Наставник, я застрял",             # роль без якоря — остаётся у Hermes
    "какой гейт блокирует создание репо?",  # вопрос об устройстве, не рефлексия
    "какой метод я применил?",          # расширенный якорь только после «Наставник,»
    "расскажи про мотивацию вообще",
    "",
    # Холодное ревью 05.09 (High): у разработчика T4 «гейт»/«понятие» — обиходные
    # слова вне IWE; без «?»/префикса такие фразы остаются у Hermes.
    "какой гейт сработал: AND или OR, помоги разобраться в этой логической схеме",
    "напомни, какой гейт я применил в CI пайплайне вчера — quality gate или security gate",
    "объясни фотосинтез через понятия, знакомые школьнику",
    "какое понятие имеется в виду в этом английском тексте — object или subject",
])
def test_plain_t4_text_stays_with_hermes(text):
    assert _is_concept_naming_request(text) is False


def test_question_mark_path_keeps_the_wider_anchor_list():
    # Сужение якорей касается только пути без «?»; «?»-вопрос (участник сам
    # адресовал бота) по-прежнему распознаёт весь список Ф11.
    assert _is_role_addressed_question("? объясни фотосинтез через понятия") is True


def test_question_mark_text_is_not_double_handled():
    # «?»-путь решает _is_role_addressed_question; этот предикат его не дублирует.
    assert _is_concept_naming_request("? объясни через понятия IWE") is False
    assert _is_role_addressed_question("? объясни через понятия IWE") is True


# =============================================================================
# Холодное ревью 05.09: узкие фразы больше не совпадают с обычными вопросами
# =============================================================================

@pytest.mark.parametrize("question", [
    "итоги матча вчера",
    "почему это важно для экономики",
    "какой смысл в этой жизни",
    "нет времени на прогулку",
    "не хватает времени на сон",
    "у меня не получается связаться с поддержкой",
])
def test_generic_life_questions_no_longer_leak_free_access(question):
    # Раньше эти фразы совпадали как подстрока с лексикой Навигатора/Наставника
    # и открывали бесплатный доступ любому без подписки — сужение паттернов
    # (05.09, холодное ревью) закрывает эту дыру, не трогая явные обращения
    # к роли и специфичные учебные формулировки.
    assert _detect_role(question) is None
    assert _role_exempt_from_paywall(question, None) is None


@pytest.mark.parametrize("question", [
    "не хватает времени учиться",
    "нет времени учиться",
    "итоги недели",
    "подведи итоги",
    "зачем это учить",
])
def test_scoped_learning_phrases_still_route_to_mentor(question):
    # Узкие, явно учебные формулировки продолжают распознаваться — сужение
    # не сломало основной сценарий. Ф14: без явного имени — к mentor.
    assert _detect_role(question) == "mentor"


@pytest.mark.parametrize("question", [
    "куда пойти поужинать вечером",
    "что выбрать на подарок жене",
    "какой курс валют сегодня",
    "протестируй мой новый скрипт на баги",
    "нужна диагностика двигателя автомобиля",
])
def test_everyday_questions_from_verification_no_longer_leak_free_access(question):
    # Верификация 05.09 нашла эти конкретные бытовые вопросы ловящимися по
    # ошибке вторым проходом сужения — закрыто здесь.
    assert _detect_role(question) is None
    assert _role_exempt_from_paywall(question, None) is None


@pytest.mark.parametrize("question", [
    "как спланировать неделю ремонта в квартире",
    "план на неделю по диете",
    "как у меня дела со здоровьем после болезни",
    "мой прогресс за неделю в спортзале",
    "сколько времени уделять сну каждый день",
])
def test_round_two_verification_findings_no_longer_leak_free_access(question):
    # Второй проход верификации 05.09 нашёл эти фразы (SS.4/SS.5) без учебного
    # якоря — закрыто третьим проходом сужения.
    assert _detect_role(question) is None


@pytest.mark.parametrize("question", [
    "как спланировать неделю учёбы",
    "план на неделю учёбы",
    "как у меня дела с учёбой",
    "мой прогресс за неделю в учёбе",
    "сколько времени уделять учёбе",
])
def test_scoped_rhythm_phrases_still_route_to_mentor(question):
    assert _detect_role(question) == "mentor"


# =============================================================================
# _is_paywall_exempt — липкая сессия (Ф13, находка Fable-ревью)
# =============================================================================

def test_sticky_free_role_exempts_followup_without_role_lexicon():
    # "да, на втором задании" — не содержит ролевой лексики, но сессия уже
    # была открыта как бесплатная роль на первом сообщении.
    session_ctx = {"active_free_role": "mentor"}
    assert _is_paywall_exempt(None, session_ctx) is True


def test_fresh_session_without_sticky_flag_not_exempt():
    assert _is_paywall_exempt(None, {}) is False


def test_fresh_role_detection_exempt_regardless_of_sticky_flag():
    assert _is_paywall_exempt("navigator", {}) is True


def test_mentor_stuck_lexicon_residual_risk_accepted():
    # "застрял"/"нет мотивации" — ядро лексики Наставника ещё с июля (WP-498
    # вариант B), сознательно НЕ сужается: и без объекта задания ("застрял в
    # пробке") они всё ещё матчат — это принятый пилотом остаточный риск
    # (WP-498.md, находка верификации 05.09), не регрессия.
    assert _detect_role("застрял в пробке по дороге домой") == "mentor"


@pytest.mark.parametrize("question", [
    "протестируй меня",
    "нужна диагностика двигателя, определи мою ступень усталости",
    "тестирование ступени",
])
def test_scoped_diagnostician_phrases_still_route_to_mentor(question):
    assert _detect_role(question) == "mentor"


# =============================================================================
# T4-follow-up больше не уходит в Hermes (Ф13, находка Fable-ревью)
# =============================================================================

def test_consultation_state_registered_as_expecting_reply():
    # Без этого follow-up внутри активной консультации на T4-аккаунте уходил
    # в Hermes вместо ответа роли — handlers/external_session.py:_sm_is_expecting_reply
    # проверяет ровно этот словарь, тот же таймаут, что и SESSION_TIMEOUT_SEC
    # консультации (5 мин).
    from config.settings import SM_EXPECTING_REPLY_STATES
    from states.common.consultation import SESSION_TIMEOUT_SEC

    assert "common.consultation" in SM_EXPECTING_REPLY_STATES
    assert SM_EXPECTING_REPLY_STATES["common.consultation"] == SESSION_TIMEOUT_SEC // 60


# =============================================================================
# Интеграционный тест enter() (Ф13, второй раунд Fable-ревью): /navigator без
# сразу заданного вопроса, затем follow-up без ролевой лексики — БЕЗ мока
# логики _is_paywall_exempt, реальный ConsultationState.enter() дважды подряд.
# =============================================================================

@pytest.mark.asyncio
async def test_bare_navigator_command_then_followup_stays_paywall_exempt():
    from unittest.mock import AsyncMock, MagicMock, patch

    from states.common.consultation import ConsultationState

    state = ConsultationState(bot=MagicMock(), db=MagicMock(), llm=MagicMock(), i18n=MagicMock())
    fake_db: dict = {}  # chat_id -> current_context, эмулирует Postgres

    async def fake_save(chat_id, ctx):
        fake_db[chat_id] = dict(ctx)

    chat_id = 999001

    with patch.object(state, "send", new=AsyncMock()), \
         patch.object(state, "_save_session_context", new=AsyncMock(side_effect=fake_save)), \
         patch("core.access.access_layer.has_access", new=AsyncMock(return_value=False)) as mock_has_access:

        # Шаг 1: /navigator без вопроса — т.к. force_role снимает paywall,
        # has_access вообще не должен вызываться.
        user1 = {"chat_id": chat_id, "current_context": None}
        result1 = await state.enter(user1, context={"force_role": "navigator"})
        assert result1 is None  # остаёмся в стейте, ждём вопрос
        mock_has_access.assert_not_called()
        assert fake_db.get(chat_id, {}).get("active_free_role") == "navigator", (
            "После /navigator без вопроса флаг active_free_role должен быть "
            f"сохранён в БД. Реально сохранено: {fake_db.get(chat_id)}"
        )

        # Шаг 2: follow-up без ролевой лексики, как реальный caller —
        # user перезагружен из "БД" (fake_db), force_role в context НЕ передан
        # (handle() его никогда не прокидывает).
        user2 = {"chat_id": chat_id, "current_context": fake_db[chat_id]}
        result2 = await state.enter(user2, context={"question": "да, второй вариант мне подходит больше"})

    # Если баг не закрыт — has_access вызовется и вернёт False → "done" (paywall).
    assert result2 != "done", (
        "Follow-up после голого /navigator упал на paywall — sticky-флаг "
        "active_free_role был потерян между вызовами enter()."
    )
    assert mock_has_access.call_count == 0, (
        "has_access не должен вызываться для follow-up внутри бесплатной сессии роли."
    )


# =============================================================================
# mentor.md покрывает алгоритмы Навигатора/Диагноста (Ф14, находка ревью)
# =============================================================================

def test_mentor_prompt_covers_navigator_start_algorithm():
    from engines.shared.consultation_tools import load_role_prompt

    prompt = load_role_prompt("mentor")
    assert "стартовую точку" in prompt
    assert "малый проверяемый эксперимент" in prompt


def test_mentor_prompt_covers_diagnostician_formal_algorithm():
    from engines.shared.consultation_tools import load_role_prompt

    prompt = load_role_prompt("mentor")
    assert "ежедневный/почти ежедневный слот" in prompt
    assert "Состояния (ресурс)" in prompt
    assert "Практикующий" in prompt and "Систематический" in prompt
