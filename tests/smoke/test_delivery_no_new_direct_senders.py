"""Сторож «нет новых прямых отправителей» (WP-418, пир-сессия
2026-08-01-01-wp418-direct-send-guard, тик 21 цикла WP-502).

Глобальный инвариант Доставщика: весь proactive-отправляющий код — только
через core.notification_service.enqueue. Прямой вызов контролируемых
`bot.send_*` разрешён ТОЛЬКО в функциях из ALLOWED_DIRECT_SENDERS
(pre-existing инвентарь волн 2-3 + сам слой доставки). Прецедент дрейфа:
коммит 70ba16b добавил `_notify_external_client_available` прямым пушем мимо
слоя — сканер волны 1 (только мигрированные функции) его пропустил.

Реализация — AST (не regex): строки/docstring/комментарии не матчатся,
методы классов и вложенные функции получают квалифицированные имена.
`send_chat_action` (typing indicator) — не доставка, вне контроля.
Охват владельца: `bot`, `self.bot`, `Bot(...)`. Документированный предел:
`b = bot; b.send_...` и `getattr(bot, ...)` не ловятся (полный data-flow
избыточен для smoke-сторожа). `message.answer` — отдельная волна
(может быть обходом; callback.answer — ACK/toast, отдельный класс).
"""
import ast
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Контролируемые методы доставки контента (пополнение — осознанной правкой).
CONTROLLED_SEND_METHODS = frozenset({
    "send_message", "send_photo", "send_video", "send_document",
    "send_audio", "send_voice", "send_animation", "send_sticker",
    "send_location", "send_venue", "send_contact", "send_poll",
    "send_dice", "send_media_group",
})

# Закрытый список исключений из скана (всё остальное от корня проекта сканируется).
EXCLUDED_DIRS = frozenset({
    "tests", "__pycache__", ".venv", "venv", "node_modules", ".git",
    "migrations", "archive",
})

# Файлы самого слоя доставки — они и есть легальная точка отправки.
DELIVERY_LAYER_FILES = frozenset({
    "core/notification_service.py",
    "core/nudge_delivery.py",
})


def _iter_py_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        yield rel


def _owner_is_bot(node: ast.AST) -> bool:
    """Значение-владелец атрибута — bot / self.bot / Bot(...)."""
    if isinstance(node, ast.Name) and node.id == "bot":
        return True
    if isinstance(node, ast.Attribute) and node.attr == "bot":
        return True  # self.bot, x.bot
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Bot":
        return True
    return False


def scan_direct_senders(root: Path) -> set[tuple[str, str]]:
    """Множество (rel_file, qualified_func) с прямыми bot.send_* вызовами."""
    hits: set[tuple[str, str]] = set()
    for rel in _iter_py_files(root):
        if str(rel) in DELIVERY_LAYER_FILES:
            continue
        tree = ast.parse((root / rel).read_text(encoding="utf-8", errors="replace"))
        # Стек квалифицированных имён: Class.method / outer.inner / <module>
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute)
                    and func.attr in CONTROLLED_SEND_METHODS
                    and _owner_is_bot(func.value)):
                continue
            hits.add((str(rel), _qualified_owner(tree, node)))
    return hits


def _qualified_owner(tree: ast.AST, call: ast.Call) -> str:
    """Квалифицированное имя функции, содержащей вызов (Class.method/outer.inner/<module>)."""
    best = None
    best_span = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if (hasattr(node, "lineno") and hasattr(node, "end_lineno")
                    and node.lineno <= call.lineno <= (node.end_lineno or node.lineno)):
                span = (node.end_lineno or node.lineno) - node.lineno
                if best_span is None or span < best_span:
                    best = node
                    best_span = span
    if best is None:
        return "<module>"
    # Родительский класс/функция — вторым проходом для квалификации
    parts = [best.name]
    for node in ast.walk(tree):
        if node is best:
            continue
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (hasattr(node, "lineno") and hasattr(node, "end_lineno")
                    and node.lineno <= best.lineno <= (node.end_lineno or node.lineno)
                    and node.col_offset < best.col_offset):
                span = (node.end_lineno or node.lineno) - node.lineno
                if span > best_span:
                    parts.insert(0, node.name)
                    best_span = span
    return ".".join(parts)


# ── allowlist: pre-existing инвентарь (сгенерирован 2026-08-01, см. пир-сессию) ──
# Категории: (D) слой доставки в scheduler.py · (W2) инвентарь волны 2
# (oauth/tier/health) · (S) абстракции/обёртки состояний · (P) прочее
# pre-existing — кандидаты волн 2-3. Новая запись сюда — только с причиной.
ALLOWED_DIRECT_SENDERS: frozenset[tuple[str, str]] = frozenset({
    # (D) слой доставки в scheduler.py · (W2) инвентарь волны 2 (oauth/tier/health)
    # · (S) абстракции/обёртки состояний · (P) прочее pre-existing (волны 2-3).
    # Новая запись сюда — только с причиной в комментарии.
("core/autofix.py", "send_fix_proposal"),
("core/dispatcher.py", "Dispatcher.route_command"),
("core/feedback_triage.py", "_send_alert"),
("core/health_check.py", "run_l3_health_check"),
("core/onboarder/x2.py", "_finish_x2"),
("core/onboarder/x2.py", "_show_topic"),
("core/onboarder/x2.py", "run_step"),
("core/onboarder/x3.py", "_show_x3_offer"),
("core/scheduler.py", "_check_marathon_missed_checkins"),
("core/scheduler.py", "_check_marathon_split_delivery"),
("core/scheduler.py", "_check_retry_storm"),
# WP-330 Ф13: direct send moved under the lock/cooldown wrapper; delivery
# migration remains separate from the rate-limit correctness fix.
("core/scheduler.py", "_discourse_check_comments_unlocked"),
("core/scheduler.py", "_discourse_scheduled_publish"),
("core/scheduler.py", "_drain_delivery_queue.deliver"),
("core/scheduler.py", "_notify_retry_exhausted"),
("core/scheduler.py", "_process_marathon_queue"),
("core/scheduler.py", "_refresh_subscribers_snapshot"),
("core/scheduler.py", "_send_marathon_weekly_digest"),
("core/scheduler.py", "_send_slot_daily_prompt"),
    # WP-502: direct send moved under _publisher_scan_lock wrapper (same
    # pattern as _discourse_check_comments_unlocked above); _smart_publisher_scan
    # itself now only delegates and holds no direct bot.send_* call.
("core/scheduler.py", "_smart_publisher_scan_unlocked"),
("core/scheduler.py", "_watch_delivery_queue"),
("core/scheduler.py", "pre_generate_feed_digest"),
("core/scheduler.py", "scheduled_check"),
("core/scheduler.py", "send_event_notifications"),
("core/scheduler.py", "send_feedback_daily_digest"),
("core/scheduler.py", "send_feedback_weekly_digest"),
("core/scheduler.py", "send_milestone_notifications"),
("core/scheduler.py", "send_reminder"),
("core/scheduler.py", "send_user_reminder"),
("core/tier_detector.py", "_notify_downgrade"),
("core/tier_detector.py", "_notify_external_client_available"),
("core/unstick.py", "recover_user"),
("db/connection.py", "_verify_schema._alert"),
("engines/feed/handlers.py", "show_topic_selection_direct"),
("engines/tailor/bot_adapter.py", "BotTailorAdapter.deliver"),
("engines/tailor/delivery.py", "notify_tailor_lesson"),
("handlers/channels.py", "_notify_reply_to_bot_user"),
("handlers/channels.py", "_send_simple_notification"),
("handlers/channels.py", "on_bot_added_to_channel"),
("handlers/dev.py", "cmd_broadcast_reconnect"),
("handlers/external_session.py", "_heartbeat_soft_fail"),
("handlers/external_session.py", "_safe_send"),
("handlers/legacy/learning.py", "send_practice_topic"),
("handlers/legacy/learning.py", "send_theory_topic"),
("handlers/legacy/learning.py", "send_topic"),
("handlers/showcase.py", "_send_seminar_access"),
("handlers/tier_upgrade.py", "nudge_post_diagnosis_s0"),
("handlers/tier_upgrade.py", "nudge_post_diagnosis_sN"),
("handlers/tier_upgrade.py", "nudge_subscription_cta"),
("handlers/tier_upgrade.py", "nudge_tier_suggest_t2"),
("handlers/workshop.py", "_send_invite_by_count"),
("states/base.py", "BaseState.send"),
("states/feed/topics.py", "FeedTopicsState._accept_topics"),
("states/utilities/feedback.py", "FeedbackState._notify_developer_red"),
})


def test_no_new_direct_senders():
    actual = scan_direct_senders(PROJECT_ROOT)
    new = actual - ALLOWED_DIRECT_SENDERS
    stale = ALLOWED_DIRECT_SENDERS - actual
    messages = []
    if new:
        messages.append(
            "НОВЫЕ прямые отправители (миграция на Доставщик — "
            "core.notification_service.enqueue, либо осознанное дополнение "
            "allowlist с причиной):\n" + "\n".join(f"  {f}: {fn}" for f, fn in sorted(new)))
    if stale:
        messages.append(
            "УСТАРЕВШИЕ записи allowlist (функции удалены/переименованы — "
            "обнови allowlist, иначе имя свободно для незаметного возврата):\n"
            + "\n".join(f"  {f}: {fn}" for f, fn in sorted(stale)))
    assert not messages, "\n\n".join(messages)


# ── fixture-тесты самого сканера ──

def _scan_source(tmp_path: Path, src: str) -> set[tuple[str, str]]:
    (tmp_path / "probe.py").write_text(src, encoding="utf-8")
    return scan_direct_senders(tmp_path)


def test_scanner_ignores_comments_and_strings(tmp_path):
    src = (
        "# await bot.send_message(chat_id, 'x')\n"
        'DOC = """bot.send_message(chat_id, text)"""\n'
        "async def ok():\n    pass\n"
    )
    assert _scan_source(tmp_path, src) == set()


def test_scanner_catches_method_and_nested(tmp_path):
    src = (
        "class S:\n"
        "    async def go(self):\n"
        "        await self.bot.send_message(1, 'x')\n"
        "def outer():\n"
        "    def inner():\n"
        "        bot.send_photo(1, 'p')\n"
        "    return inner\n"
    )
    hits = _scan_source(tmp_path, src)
    assert ("probe.py", "S.go") in hits
    assert ("probe.py", "outer.inner") in hits


def test_scanner_catches_module_level_and_multiline(tmp_path):
    src = (
        "bot.send_message(\n    1, 'x'\n)\n"
        "async def ok():\n    pass\n"
    )
    assert ("probe.py", "<module>") in _scan_source(tmp_path, src)


def test_scanner_ignores_chat_action_and_non_bot(tmp_path):
    src = (
        "async def ok():\n"
        "    await bot.send_chat_action(chat_id=1, action='typing')\n"
        "    await message.answer('x')\n"
    )
    assert _scan_source(tmp_path, src) == set()


def test_scanner_catches_bot_constructor(tmp_path):
    src = (
        "async def ok():\n"
        "    await Bot(token).send_message(1, 'x')\n"
    )
    assert ("probe.py", "ok") in _scan_source(tmp_path, src)


if __name__ == "__main__":  # генератор allowlist (одноразовый, не тест)
    for f, fn in sorted(scan_direct_senders(PROJECT_ROOT)):
        print(f'    ("{f}", "{fn}"),')
    sys.exit(0)
