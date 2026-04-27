"""
Smoke: Discourse ownership-check для /club connect.

WP-7 / DC1+DC2: бот не должен подключать чужой блог-категорию к Telegram-юзеру.
Проверка через group_permissions Discourse — личная группа `user-N` владеет категорией.
"""

import pytest

from handlers.discourse import _category_owner_groups, _user_is_category_owner


# ── Owner groups extraction ───────────────────────────────────


def test_owner_groups_user_2():
    cat = {
        "id": 37,
        "group_permissions": [
            {"permission_type": 2, "group_name": "everyone"},
            {"permission_type": 1, "group_name": "user-2"},
        ],
    }
    assert _category_owner_groups(cat) == ["user-2"]


def test_owner_groups_excludes_everyone_full():
    # Корнер: даже если everyone имеет permission_type=1 — не считаем владельцем
    cat = {
        "group_permissions": [
            {"permission_type": 1, "group_name": "everyone"},
        ],
    }
    assert _category_owner_groups(cat) == []


def test_owner_groups_no_perms():
    assert _category_owner_groups({}) == []
    assert _category_owner_groups({"group_permissions": None}) == []
    assert _category_owner_groups({"group_permissions": []}) == []


def test_owner_groups_multiple_full_access():
    cat = {
        "group_permissions": [
            {"permission_type": 1, "group_name": "user-2"},
            {"permission_type": 1, "group_name": "moderators"},
        ],
    }
    assert set(_category_owner_groups(cat)) == {"user-2", "moderators"}


# ── User-category ownership ───────────────────────────────────


CAT_TSEREN = {
    "id": 37,
    "name": "Tseren Tserenov",
    "group_permissions": [
        {"permission_type": 2, "group_name": "everyone"},
        {"permission_type": 1, "group_name": "user-2"},
    ],
}

USER_TSEREN = {
    "id": 4,
    "username": "tseren-tserenov",
    "groups": [
        {"name": "trust_level_0"},
        {"name": "trust_level_1"},
        {"name": "user-2"},
        {"name": "students"},
    ],
}

USER_ANDREY = {
    "id": 2370,
    "username": "andrei-akatov",
    "groups": [
        {"name": "trust_level_0"},
        {"name": "trust_level_1"},
        {"name": "students"},
        {"name": "user-2896"},
    ],
}


def test_owner_match_tseren_owns_category_37():
    # Tseren в группе user-2 → владеет категорией с group_permissions user-2
    assert _user_is_category_owner(USER_TSEREN, CAT_TSEREN) is True


def test_owner_mismatch_andrey_not_owner():
    # Андрей в user-2896, не в user-2 → не владелец категории 37
    assert _user_is_category_owner(USER_ANDREY, CAT_TSEREN) is False


def test_owner_no_owner_group_in_category():
    # Категория без явного владельца (только everyone) — никто не владеет
    cat_common = {
        "group_permissions": [
            {"permission_type": 2, "group_name": "everyone"},
        ],
    }
    assert _user_is_category_owner(USER_TSEREN, cat_common) is False


def test_owner_user_with_no_groups():
    user_empty = {"id": 999, "username": "ghost", "groups": []}
    assert _user_is_category_owner(user_empty, CAT_TSEREN) is False


def test_owner_user_groups_none():
    # user.groups = None (corner case Discourse API)
    user_none = {"id": 999, "username": "ghost", "groups": None}
    assert _user_is_category_owner(user_none, CAT_TSEREN) is False
