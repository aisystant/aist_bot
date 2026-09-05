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
    _role_exempt_from_paywall,
)
from handlers.fallback import _is_role_addressed_question  # noqa: E402


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
def test_scoped_learning_phrases_still_route_to_navigator(question):
    # Узкие, явно учебные формулировки продолжают распознаваться — сужение
    # не сломало основной сценарий.
    assert _detect_role(question) == "navigator"


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
def test_scoped_rhythm_phrases_still_route_to_navigator(question):
    assert _detect_role(question) == "navigator"


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
def test_scoped_diagnostician_phrases_still_route(question):
    assert _detect_role(question) == "diagnostician"
