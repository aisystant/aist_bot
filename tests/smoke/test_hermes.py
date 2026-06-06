"""Smoke-тесты: hermes_router (WP-392 Ф3.1).

«Гермес»/«hermes» → внешний Hermes-рантайм (Nous Research) через hermes_chat для T3+.
Для T1/T2 — Проводник (Haiku, DP.SC.169, WP-349 Ф33).
Роутер выделен из fallback и регистрируется ДО external_session/fallback.
"""

from handlers.hermes import _is_hermes_message
from tests.smoke.factories import text_message


# ─── Фильтр ───

def test_filter_matches_hermes_prefix():
    for txt in ("Гермес, привет", "гермес статус", "Hermes hi", "  Гермес"):
        msg = text_message(txt, chat_id=1).message
        assert _is_hermes_message(msg) is True


def test_filter_rejects_non_hermes():
    for txt in ("привет", "/start", "что по WP-392", "Герметичность"):
        # «Герметичность» начинается с «гермет», не «гермес» → False
        msg = text_message(txt, chat_id=1).message
        assert _is_hermes_message(msg) is (txt.lower().startswith(("гермес", "hermes")))
