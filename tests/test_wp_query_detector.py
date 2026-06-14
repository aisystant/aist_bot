"""Unit-тесты детектора «про мои РП» и парсера реестра (WP-411 Ф7).

Покрывают четыре категории из критериев приёмки:
Strong / Weak / Negation / PERSONAL_SIGNALS, плюс парсер активных строк.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engines.shared.wp_query_detector import is_wp_query, parse_active_registry


# --- Strong: личное местоимение в ключевой фразе → всегда True ---
@pytest.mark.parametrize("q", [
    "какие мои рп ты знаешь?",
    "покажи мой реестр",
    "мои рабочие продукты сейчас",
    "мои задачи покажи",
    "мой план работ на месяц",
    "загляни в wp-registry",
    "реестр рп открой",
])
def test_strong_patterns_detected(q):
    assert is_wp_query(q) is True


def test_strong_my_wp_explicit():
    assert is_wp_query("Расскажи мои РП") is True
    assert is_wp_query("сколько у меня мои рп") is True


# --- Weak: слово-РП + маркер списка → True ТОЛЬКО при личном сигнале ---
def test_weak_with_personal_signal_true():
    assert is_wp_query("мои активные рп") is True
    assert is_wp_query("сколько у меня активных рп") is True
    assert is_wp_query("расскажи мои активные рп") is True  # «расскажи» НЕ негация
    assert is_wp_query("список моих текущих рп") is True


def test_weak_without_personal_signal_false():
    # «активные рп» без местоимения — может быть вопрос о системе, не личный
    assert is_wp_query("какие активные рп бывают") is False
    assert is_wp_query("список рп в системе") is False


# --- Negation: тематический вопрос о системе РП → False ---
@pytest.mark.parametrize("q", [
    "что такое рп",
    "что такое wp",
    "как работают рп в iwe",
    "как устроена система рп",
    "объясни систему рп",
    "зачем нужны рп",
])
def test_thematic_negations_false(q):
    assert is_wp_query(q) is False


def test_strong_overrides_negation():
    # сильный личный сигнал перевешивает тематическую фразу
    assert is_wp_query("что такое мой реестр рп") is True


# --- Ложные срабатывания на словах-омонимах ---
def test_no_false_positive_on_substrings():
    assert is_wp_query("перпендикуляр это что") is False   # «рп» как подстрока
    assert is_wp_query("корпус автомобиля") is False
    assert is_wp_query("привет, как дела?") is False
    assert is_wp_query("") is False


# --- Парсер реестра ---
_SAMPLE_REGISTRY = """\
# WP-REGISTRY

| Статус | Расшифровка |
|--------|-------------|
| ✅ | done |

| # | P | Название | статус | путь | бюджет |
|---|---|----------|--------|------|--------|
| 419 | P3 | **Описание метода кодирования** | ⏳ | PACK-x | 16h |
| 418 | P2 | **Доставщик сообщений** | 🔄 | DS-my-strategy/inbox/WP-418.md | Ф0-Ф3 done |
| ~~416~~ | ~~P3~~ | ~~Welcome bonus эмиттер~~ | ❌ | ~~path~~ | ~~cancelled~~ |
| ~~414~~ | ~~P4~~ | ~~Мультипликатор~~ | ✅ | ~~path~~ | ~~2h~~ |
"""


def test_parse_active_registry_keeps_only_active():
    out = parse_active_registry(_SAMPLE_REGISTRY)
    assert "Активных рабочих продуктов: 2" in out
    assert "РП419 (P3, ⏳): Описание метода кодирования" in out
    assert "РП418 (P2, 🔄): Доставщик сообщений" in out


def test_parse_active_registry_drops_done_and_cancelled():
    out = parse_active_registry(_SAMPLE_REGISTRY)
    assert "416" not in out          # cancelled
    assert "414" not in out          # done
    assert "Мультипликатор" not in out


def test_parse_active_registry_strips_markdown_bold():
    out = parse_active_registry(_SAMPLE_REGISTRY)
    assert "**" not in out
    assert "~~" not in out


def test_parse_empty_registry_returns_empty_string():
    assert parse_active_registry("# WP-REGISTRY\n\nнет таблицы") == ""
    assert parse_active_registry("") == ""


def test_parse_respects_max_rows():
    rows = "\n".join(
        f"| {400 + i} | P1 | **РП {i}** | 🔄 | path | 1h |" for i in range(10)
    )
    md = "| # | P | Название | статус | путь | бюджет |\n|---|---|---|---|---|---|\n" + rows
    out = parse_active_registry(md, max_rows=3)
    # заголовок считает все активные, но в тело попадают только max_rows строк
    assert "Активных рабочих продуктов: 10" in out
    assert out.count("РП4") == 3


# --- strict=True: Strong-only режим для T4-full (WP-411 Ф7) ---
@pytest.mark.parametrize("q", [
    "какие мои рп ты знаешь?",
    "покажи мой реестр",
    "что такое мой реестр рп",   # strong перевешивает даже в strict
])
def test_strict_keeps_strong_patterns(q):
    assert is_wp_query(q, strict=True) is True


@pytest.mark.parametrize("q", [
    "мои активные рп",            # weak+personal → True без strict, False в strict
    "сколько у меня активных рп",
    "список моих текущих рп",
    "расскажи мои активные рп",
])
def test_strict_drops_weak_patterns(q):
    # без strict — личный запрос; в strict (путь Гермеса) — НЕ перехватываем,
    # чтобы не оборвать co-thinking-сессию на пограничной формулировке.
    assert is_wp_query(q) is True
    assert is_wp_query(q, strict=True) is False


def test_strict_default_is_false():
    # дефолт сохраняет прежнее поведение пути консультанта (T1-T3)
    assert is_wp_query("мои активные рп") is True
