"""WP-7 OrphanRecovery-TargetBot-Ignored — recover_orphan_finalizations должна
сверять target_bot из meta сессии с флейвором текущего процесса.

Регрессия (найдено в РП-501, 26.07.2026): recover_orphan_finalizations запускается
независимо в КАЖДОМ из двух ботов (prod/pilot), сканирует одни и те же
SESSION-*.md со status: completed и раньше финализировала через любой bot-объект,
первым увидевший сироту — без проверки target_bot. Живой инцидент: сессия с
target_bot: prod была финализирована пилот-ботом, потому что его процесс
перезапустился первым и раньше пересканировал.
"""

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

import handlers.external_session as external_session  # noqa: E402


def _meta(status="completed", target_bot="prod", extra=""):
    return (
        f"---\n"
        f"status: {status}\n"
        f"target_bot: {target_bot}\n"
        f"tg_chat_id: 12345\n"
        f"created_at: 2026-08-01\n"
        f"{extra}"
        f"---\n"
    )


@pytest.mark.asyncio
async def test_foreign_target_bot_is_skipped():
    """target_bot=pilot, текущий процесс prod — сирота не забирается, ждёт своего бота."""
    with patch.object(external_session, "_BOT_FLAVOR", "prod"), \
         patch.object(external_session, "_PILOT_PAT", "token"), \
         patch.object(external_session, "_PILOT_REPO", "org/repo"), \
         patch.object(external_session, "_gh_list_dir", new=AsyncMock(return_value=[
             {"type": "file", "name": "SESSION-20260801-000000-abcdef.md",
              "path": "inbox/agent/sessions/SESSION-20260801-000000-abcdef.md"},
         ])), \
         patch.object(external_session, "_gh_get_file",
                      new=AsyncMock(return_value=(_meta(target_bot="pilot"), "sha"))), \
         patch.object(external_session, "_spawn") as mock_spawn:
        await external_session.recover_orphan_finalizations(bot=AsyncMock())

    mock_spawn.assert_not_called()


@pytest.mark.asyncio
async def test_matching_target_bot_is_recovered():
    """target_bot=prod, текущий процесс prod — сирота финализируется этим процессом."""
    with patch.object(external_session, "_BOT_FLAVOR", "prod"), \
         patch.object(external_session, "_PILOT_PAT", "token"), \
         patch.object(external_session, "_PILOT_REPO", "org/repo"), \
         patch.object(external_session, "_gh_list_dir", new=AsyncMock(return_value=[
             {"type": "file", "name": "SESSION-20260801-000000-abcdef.md",
              "path": "inbox/agent/sessions/SESSION-20260801-000000-abcdef.md"},
         ])), \
         patch.object(external_session, "_gh_get_file",
                      new=AsyncMock(return_value=(_meta(target_bot="prod"), "sha"))), \
         patch.object(external_session, "_finalize_session", new=AsyncMock()), \
         patch.object(external_session, "_spawn") as mock_spawn:
        await external_session.recover_orphan_finalizations(bot=AsyncMock())

    mock_spawn.assert_called_once()


@pytest.mark.asyncio
async def test_missing_target_bot_field_is_not_blocked():
    """Pre-cutover meta без поля target_bot — не блокируем навечно, обрабатываем как раньше."""
    meta_without_target_bot = (
        "---\nstatus: completed\ntg_chat_id: 12345\ncreated_at: 2026-08-01\n---\n"
    )
    with patch.object(external_session, "_BOT_FLAVOR", "prod"), \
         patch.object(external_session, "_PILOT_PAT", "token"), \
         patch.object(external_session, "_PILOT_REPO", "org/repo"), \
         patch.object(external_session, "_gh_list_dir", new=AsyncMock(return_value=[
             {"type": "file", "name": "SESSION-20260801-000000-abcdef.md",
              "path": "inbox/agent/sessions/SESSION-20260801-000000-abcdef.md"},
         ])), \
         patch.object(external_session, "_gh_get_file",
                      new=AsyncMock(return_value=(meta_without_target_bot, "sha"))), \
         patch.object(external_session, "_finalize_session", new=AsyncMock()), \
         patch.object(external_session, "_spawn") as mock_spawn:
        await external_session.recover_orphan_finalizations(bot=AsyncMock())

    mock_spawn.assert_called_once()
