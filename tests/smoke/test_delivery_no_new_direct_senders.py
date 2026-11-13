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
Охват владельца: любой (`bot`, `self.bot`, `Bot(...)`, `_bot_instance`,
`bot_instance`, `cfg.client` …) — РП-568 Ф16: прежний фильтр по имени `bot`
пропускал 15 функций, в т.ч. весь `oauth_server.py`. Документированный предел:
`getattr(x, "send_message")` не ловится. `message.answer` — отдельная волна
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
    "copy_message", "forward_message", "send_invoice",
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
    # Transport wrapper (Bot subclass): its send_message is the transport itself.
    "core/safe_bot.py",
})


def _iter_py_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        yield rel


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
            if not (isinstance(func, ast.Attribute) and func.attr in CONTROLLED_SEND_METHODS):
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


# ── allowlist (РП-568 Ф16 шаг 1а): three sections, each entry with its reason ──
# REPLY_SENDERS — answer inside the handling of the user's own action in the bot
#   (command, button, message), to the same chat, with no external trigger. Stays
#   in the bot for good.
# OPS_SENDERS — service channels (developer chat, mentor channel, analysed group),
#   not notifications to a user about themself.
# INITIATIVE_PENDING — initiative sends to users (scheduler, webhooks, OAuth
#   callbacks, payments, commands about another person) not yet moved to
#   core.notification_service.enqueue. It may only shrink: the count is pinned
#   below and the test fails when it grows.

REPLY_SENDERS: frozenset[tuple[str, str]] = frozenset({
    ("core/dispatcher.py", "Dispatcher.route_command"),  # command routing in the user's own chat
    ("core/onboarder/x2.py", "_finish_x2"),  # onboarding step driven by the user's message
    ("core/onboarder/x2.py", "_show_topic"),  # onboarding step driven by the user's message
    ("core/onboarder/x2.py", "run_step"),  # onboarding step driven by the user's message
    ("core/onboarder/x3.py", "_show_x3_offer"),  # onboarding offer inside the same flow
    ("engines/feed/handlers.py", "show_topic_selection_direct"),  # answer to a feed button
    ("handlers/chat_analyst.py", "_run_self_action"),  # answer to /erase_me or /resume_me of the presser
    ("handlers/mentorship.py", "_tell_mentor"),  # usage error back to the mentor who ran the command
    ("states/base.py", "BaseState.send"),  # state machine reply in the active chat
    ("states/feed/topics.py", "FeedTopicsState._accept_topics"),  # answer to topic selection
})

OPS_SENDERS: frozenset[tuple[str, str]] = frozenset({
    ("core/autofix.py", "send_fix_proposal"),  # developer chat
    ("core/feedback_triage.py", "_send_alert"),  # developer chat
    ("core/health_check.py", "run_l3_health_check"),  # developer chat
    ("core/operator_alerts.py", "_send_operator_alert"),  # fixed-recipient operator transport for database/outbox failures; cannot depend on the queue it reports on (WP-562/WP-567)
    ("core/scheduler.py", "_check_marathon_missed_checkins"),  # mentor channel
    ("core/scheduler.py", "_check_retry_storm"),  # developer chat
    ("core/scheduler.py", "_drain_delivery_queue.deliver"),  # the delivery transport itself
    ("core/scheduler.py", "_refresh_subscribers_snapshot"),  # developer chat
    ("core/scheduler.py", "_watch_delivery_queue"),  # developer chat; the queue watcher cannot depend on the queue
    ("core/scheduler.py", "send_feedback_daily_digest"),  # developer chat
    ("core/scheduler.py", "send_feedback_weekly_digest"),  # developer chat
    ("db/connection.py", "_send_schema_alert"),  # developer chat (schema drift, REQUIRED_FOR_MONEY)
    ("handlers/chat_analyst.py", "_begin_collection"),  # archiving notice pinned in the analysed group and the owner (WP-612 Ф5)
    ("handlers/chat_analyst.py", "_forward"),  # chat archive forwarding, not a notification
    ("states/utilities/feedback.py", "FeedbackState._notify_developer_red"),  # developer chat
})

INITIATIVE_PENDING: frozenset[tuple[str, str]] = frozenset({
    ("clients/digital_twin.py", "DigitalTwinClient.refresh_expiring_tokens"),
    ("core/scheduler.py", "_check_marathon_split_delivery"),
    ("core/scheduler.py", "_discourse_check_comments_unlocked"),
    ("core/scheduler.py", "_discourse_scheduled_publish"),
    ("core/scheduler.py", "_notify_retry_exhausted"),
    ("core/scheduler.py", "_process_marathon_queue"),
    ("core/scheduler.py", "_send_marathon_weekly_digest"),
    ("core/scheduler.py", "_send_mentorship_disclaimer"),
    ("core/scheduler.py", "_smart_publisher_scan_unlocked"),
    ("core/scheduler.py", "pre_generate_feed_digest"),
    ("core/scheduler.py", "scheduled_check"),
    ("core/scheduler.py", "send_event_notifications"),
    ("core/scheduler.py", "send_milestone_notifications"),
    ("core/tier_detector.py", "_notify_downgrade"),
    ("core/tier_detector.py", "_notify_external_client_available"),
    ("core/unstick.py", "recover_user"),
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
    ("handlers/mentorship.py", "cmd_mentor_invite"),
    ("handlers/showcase.py", "_send_seminar_access"),
    ("handlers/workshop.py", "_send_direct_masterskaya_invite"),
    ("handlers/workshop.py", "_send_invite_by_count"),
    ("oauth_server.py", "_ory_already_linked_response"),
    ("oauth_server.py", "_ory_guard_response"),
    ("oauth_server.py", "chatwoot_webhook_handler"),
    ("oauth_server.py", "github_app_callback_handler"),
    ("oauth_server.py", "github_callback_handler"),
    ("oauth_server.py", "google_calendar_callback_handler"),
    ("oauth_server.py", "internal_notify_handler"),
    ("oauth_server.py", "internal_notify_handler._send"),
    ("oauth_server.py", "linear_callback_handler"),
    ("oauth_server.py", "ory_callback_handler"),
    ("oauth_server.py", "twin_callback_handler"),
    ("oauth_server.py", "wakatime_callback_handler"),
})

# Ratchet: lower this number in the same commit that moves a sender to the queue.
INITIATIVE_PENDING_CEILING = 43

ALLOWED_DIRECT_SENDERS = REPLY_SENDERS | OPS_SENDERS | INITIATIVE_PENDING


def test_allowlist_sections_are_disjoint():
    assert not (REPLY_SENDERS & OPS_SENDERS)
    assert not (REPLY_SENDERS & INITIATIVE_PENDING)
    assert not (OPS_SENDERS & INITIATIVE_PENDING)


def test_initiative_pending_only_shrinks():
    assert len(INITIATIVE_PENDING) <= INITIATIVE_PENDING_CEILING, (
        "INITIATIVE_PENDING grew: a new initiative sender must go through "
        "core.notification_service.enqueue, not into the allowlist"
    )


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


def test_scanner_catches_any_owner_and_forwarding(tmp_path):
    src = (
        "async def via_instance():\n"
        "    await _bot_instance.send_message(1, 'x')\n"
        "async def via_client():\n"
        "    await cfg.client.copy_message(1, 2, 3)\n"
    )
    hits = _scan_source(tmp_path, src)
    assert ("probe.py", "via_instance") in hits
    assert ("probe.py", "via_client") in hits


if __name__ == "__main__":  # генератор allowlist (одноразовый, не тест)
    for f, fn in sorted(scan_direct_senders(PROJECT_ROOT)):
        print(f'    ("{f}", "{fn}"),')
    sys.exit(0)
