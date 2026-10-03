"""
find_potok_chat_link() — сопоставление code -> chatLink в get_user_courses() (WP-5).

Инцидент-триггер: оплата INTERNSHIP-потока (программы/резидентуры/семинара)
не вызывает вебхук у Aisystant, поэтому доставка приглашения в чат делается
поллингом (core.scheduler._check_pending_internship_payments) с поиском
свежего chatLink по коду потока среди курсов пользователя.
"""

from clients.aisystant import find_potok_chat_link


def _course(code, chat_link, name):
    return {"potok": {"code": code, "chatLink": chat_link, "courseName": name}}


def test_finds_matching_code():
    courses = [
        _course("S1-2026.3-T", None, "S1. Системное саморазвитие"),
        _course("SE-2026.7-T", "https://t.me/+wGvGHUD2v0M1MTAy", "Семинар: ИИ рабочая среда 2.0"),
    ]
    link, name = find_potok_chat_link(courses, "SE-2026.7-T")
    assert link == "https://t.me/+wGvGHUD2v0M1MTAy"
    assert name == "Семинар: ИИ рабочая среда 2.0"


def test_code_not_found_returns_none_none():
    courses = [_course("S1-2026.3-T", None, "S1. Системное саморазвитие")]
    link, name = find_potok_chat_link(courses, "UNKNOWN-CODE")
    assert link is None
    assert name is None


def test_matching_potok_without_chat_link_returns_none_link_but_real_name():
    courses = [_course("TEH-1", None, "Вся серия FPF + SPF (5 семинаров)")]
    link, name = find_potok_chat_link(courses, "TEH-1")
    assert link is None
    assert name == "Вся серия FPF + SPF (5 семинаров)"


def test_empty_courses_list():
    assert find_potok_chat_link([], "ANY") == (None, None)
