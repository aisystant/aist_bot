"""
L2 Auto-Fix: обнаружение ошибок → диагноз Claude → TG подтверждение → PR.

Pipeline (WP-45 Phase 3):
1. detect: query error_logs for L2 errors (count >= 3, last 15 min)
2. diagnose: Claude Sonnet analyzes error + source code from GitHub
3. propose: send TG message with diagnosis + ArchGate + inline buttons
4. apply: on ✅ → create branch fix/<key> → update file → create PR
5. reject: on ❌ → mark rejected

Safety: max 3 files/fix, protected files, max 3 proposals/cycle, always PR.
"""

import base64
import json
import re
from typing import Optional

import aiohttp
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import (
    get_logger,
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL_SONNET,
    GITHUB_BOT_PAT,
    AUTOFIX_REPO,
    AUTOFIX_BRANCH_BASE,
    AUTOFIX_BOT_DIR,
    AUTOFIX_MAX_FILES,
    AUTOFIX_MAX_PROPOSALS,
    AUTOFIX_PROTECTED,
)
from db.queries.autofix import (
    get_l2_fixable_errors,
    create_pending_fix,
    get_pending_fix,
    update_fix_status,
)

logger = get_logger(__name__)

# GitHub API base
_GH_API = "https://api.github.com"
_GH_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# Claude diagnosis system prompt
_DIAGNOSIS_SYSTEM = """You are an expert Python developer analyzing errors in an aiogram 3.x Telegram bot.

Your task:
1. Analyze the error (traceback, message, category)
2. Read the relevant source code
3. Propose a MINIMAL fix (smallest possible change)
4. Evaluate the fix quality using ArchGate (6 dimensions: 1-10 each)

RULES:
- Fix MUST be minimal: change only what's necessary
- Never change function signatures
- Never remove existing error handling
- Never touch imports unless adding a new one
- The fix must be backward-compatible
- If you're not confident, set confidence to "low"

Return ONLY valid JSON (no markdown, no explanation outside JSON):
{
  "diagnosis": "What's wrong and why (1-3 sentences, Russian)",
  "file_path": "relative/path/to/file.py",
  "original_code": "exact lines to replace (copy from source, 1-10 lines)",
  "fixed_code": "replacement lines (same indent)",
  "diff_summary": "one-line summary of change",
  "confidence": "high|medium|low",
  "archgate": {
    "evolvability": 8,
    "scalability": 9,
    "learnability": 9,
    "generativity": 7,
    "speed": 9,
    "modernity": 8
  },
  "archgate_score": 8.3,
  "archgate_weak": "generativity — fix is narrow (expected for bugfix)"
}"""


def extract_files_from_traceback(traceback_text: str) -> list[str]:
    """Extract bot source file paths from Python traceback.

    Pattern: File "/app/aist_bot_newarchitecture/<path>", line N
    Railway container path prefix: /app/

    Returns relative paths within bot dir (e.g., "core/helpers.py").
    Filters protected files, deduplicates, max AUTOFIX_MAX_FILES.
    """
    if not traceback_text:
        return []

    pattern = r'File "(?:/app/)?(?:aist_bot_newarchitecture/)?([^"]+\.py)", line (\d+)'
    matches = re.findall(pattern, traceback_text)

    seen = set()
    result = []
    for path, _line in matches:
        # Skip protected files and external packages
        if path in seen or path in AUTOFIX_PROTECTED:
            continue
        if path.startswith(("site-packages/", "/", "lib/")):
            continue
        seen.add(path)
        result.append(path)

    return result[:AUTOFIX_MAX_FILES]


async def fetch_github_file(file_path: str) -> Optional[tuple[str, str]]:
    """Fetch file content and SHA from GitHub Contents API.

    Uses GITHUB_BOT_PAT (bot-level PAT, not user OAuth).
    Returns (content, sha) or None.
    """
    full_path = f"{AUTOFIX_BOT_DIR}/{file_path}"
    url = f"{_GH_API}/repos/{AUTOFIX_REPO}/contents/{full_path}"
    headers = {**_GH_HEADERS, "Authorization": f"Bearer {GITHUB_BOT_PAT}"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=headers,
                params={"ref": AUTOFIX_BRANCH_BASE},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content = base64.b64decode(data["content"]).decode("utf-8")
                    return (content, data["sha"])
                else:
                    error = await resp.text()
                    logger.warning(f"[AutoFix] GitHub GET {full_path}: {resp.status}")
                    return None
    except Exception as e:
        logger.error(f"[AutoFix] fetch_github_file error: {e}")
        return None


async def diagnose_with_claude(
    error: dict, file_contents: dict[str, str]
) -> Optional[dict]:
    """Ask Claude Sonnet to diagnose error and propose fix.

    Returns diagnosis dict or None if diagnosis failed / low confidence.
    """
    # Build file context
    files_section = ""
    for fp, content in file_contents.items():
        files_section += f"\n--- {fp} ---\n{content}\n"

    context_str = json.dumps(error.get("context") or {}, ensure_ascii=False)

    user_prompt = f"""ERROR DETAILS:
- Category: {error.get('category', 'unknown')}
- Severity: {error.get('severity', '?')}
- Logger: {error.get('logger_name', '?')}
- Message: {error.get('message', '')[:500]}
- Occurrences: {error.get('occurrence_count', 0)}
- RUNBOOK action: {error.get('suggested_action', 'none')}

TRACEBACK:
{(error.get('traceback') or '')[:2000]}

CONTEXT:
{context_str}

SOURCE CODE:
{files_section}

Analyze this error and propose a minimal fix. Return JSON only."""

    payload = {
        "model": CLAUDE_MODEL_SONNET,
        "max_tokens": 2000,
        "system": _DIAGNOSIS_SYSTEM,
        "messages": [{"role": "user", "content": user_prompt}],
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "content-type": "application/json",
                    "anthropic-version": "2023-06-01",
                },
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"[AutoFix] Claude API {resp.status}: {error_text[:200]}")
                    return None
                data = await resp.json()
    except Exception as e:
        logger.error(f"[AutoFix] Claude API error: {e}")
        return None

    # Parse response
    text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            text += block["text"]

    # Extract JSON from response (handle possible markdown wrapping)
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        logger.error(f"[AutoFix] Failed to parse Claude response as JSON")
        return None

    # Validate required fields
    required = {"diagnosis", "file_path", "original_code", "fixed_code", "confidence"}
    if not required.issubset(result.keys()):
        logger.error(f"[AutoFix] Missing fields in diagnosis: {required - result.keys()}")
        return None

    # Filter low confidence and low ArchGate
    if result.get("confidence") == "low":
        logger.info(f"[AutoFix] Skipping low-confidence diagnosis for {error.get('error_key')}")
        return None

    archgate_score = result.get("archgate_score", 0)
    if archgate_score < 8:
        logger.info(f"[AutoFix] Skipping low ArchGate ({archgate_score}) for {error.get('error_key')}")
        return None

    return result


def _format_proposal_message(error: dict, diagnosis: dict) -> str:
    """Format TG message for fix proposal (HTML)."""
    ag = diagnosis.get("archgate", {})
    ag_line = (
        f"  \u042d{ag.get('evolvability', '?')} "
        f"\u041c{ag.get('scalability', '?')} "
        f"\u041e{ag.get('learnability', '?')} "
        f"\u0413{ag.get('generativity', '?')} "
        f"\u0421{ag.get('speed', '?')} "
        f"\u0421\u043e{ag.get('modernity', '?')}"
    )

    diff_summary = diagnosis.get("diff_summary", "")[:100]
    diag_text = diagnosis.get("diagnosis", "")[:500]
    original = (diagnosis.get("original_code") or "")[:300]
    fixed = (diagnosis.get("fixed_code") or "")[:300]

    msg = (
        f"\U0001f527 <b>L2 Auto-Fix Proposal</b>\n\n"
        f"<b>Error:</b> [{error.get('category', '?')}/L2] "
        f"{(error.get('message') or '')[:80]}\n"
        f"<b>Count:</b> {error.get('occurrence_count', 0)}x\n"
        f"<b>Logger:</b> {error.get('logger_name', '?')}\n\n"
        f"<b>\u0414\u0438\u0430\u0433\u043d\u043e\u0437:</b>\n{diag_text}\n\n"
        f"<b>Fix:</b> {diagnosis.get('file_path', '?')}\n"
        f"<pre>{diff_summary}</pre>\n\n"
        f"<b>\u0411\u044b\u043b\u043e:</b>\n<pre>{original}</pre>\n"
        f"<b>\u0421\u0442\u0430\u043b\u043e:</b>\n<pre>{fixed}</pre>\n\n"
        f"<b>ArchGate:</b> {diagnosis.get('archgate_score', '?')}/10\n"
        f"{ag_line}\n"
    )

    weak = diagnosis.get("archgate_weak", "")
    if weak:
        msg += f"\u26a0\ufe0f {weak}\n"

    msg += f"\n\U0001f50d <b>\u0423\u0432\u0435\u0440\u0435\u043d\u043d\u043e\u0441\u0442\u044c:</b> {diagnosis.get('confidence', '?')}"

    return msg


async def send_fix_proposal(
    bot: Bot, dev_chat_id: str, error: dict, diagnosis: dict
) -> Optional[int]:
    """Send fix proposal to developer via TG. Returns pending_fix_id."""
    # Create DB record first (without tg_message_id)
    fix_id = await create_pending_fix(
        error_log_id=error["id"],
        error_key=error["error_key"],
        diagnosis=diagnosis.get("diagnosis", ""),
        archgate_eval=json.dumps(diagnosis.get("archgate", {}), ensure_ascii=False),
        proposed_diff=json.dumps({
            "file_path": diagnosis["file_path"],
            "original_code": diagnosis["original_code"],
            "fixed_code": diagnosis["fixed_code"],
        }, ensure_ascii=False),
        file_path=diagnosis["file_path"],
    )

    if not fix_id:
        return None

    # Build message and keyboard
    msg_text = _format_proposal_message(error, diagnosis)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="\u2705 \u041f\u0440\u0438\u043c\u0435\u043d\u0438\u0442\u044c",
                callback_data=f"autofix_approve_{fix_id}",
            ),
            InlineKeyboardButton(
                text="\u274c \u041e\u0442\u043a\u043b\u043e\u043d\u0438\u0442\u044c",
                callback_data=f"autofix_reject_{fix_id}",
            ),
        ]
    ])

    try:
        sent = await bot.send_message(
            int(dev_chat_id),
            msg_text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        # Update tg_message_id
        from db.connection import acquire
        async with await acquire() as conn:
            await conn.execute(
                "UPDATE pending_fixes SET tg_message_id = $1 WHERE id = $2",
                sent.message_id, fix_id,
            )
        logger.info(f"[AutoFix] Proposal #{fix_id} sent for error {error['error_key']}")
        return fix_id
    except Exception as e:
        logger.error(f"[AutoFix] Failed to send TG message: {e}")
        await update_fix_status(fix_id, "failed")
        return None


async def apply_fix(fix_id: int) -> Optional[str]:
    """Apply approved fix: create branch, update file, create PR.

    Returns PR URL or None on failure.
    """
    fix = await get_pending_fix(fix_id)
    if not fix or fix["status"] != "pending":
        logger.warning(f"[AutoFix] Fix #{fix_id} not found or not pending")
        return None

    await update_fix_status(fix_id, "approved")

    try:
        diff_data = json.loads(fix["proposed_diff"])
    except (json.JSONDecodeError, TypeError):
        logger.error(f"[AutoFix] Invalid diff data for fix #{fix_id}")
        await update_fix_status(fix_id, "failed")
        return None

    file_path = diff_data.get("file_path", fix["file_path"])
    original_code = diff_data.get("original_code", "")
    fixed_code = diff_data.get("fixed_code", "")

    if not original_code or not fixed_code:
        await update_fix_status(fix_id, "failed")
        return None

    headers = {**_GH_HEADERS, "Authorization": f"Bearer {GITHUB_BOT_PAT}"}
    branch_name = f"fix/{fix['error_key'][:40]}"

    try:
        async with aiohttp.ClientSession() as session:
            # 1. Get base branch SHA
            async with session.get(
                f"{_GH_API}/repos/{AUTOFIX_REPO}/git/ref/heads/{AUTOFIX_BRANCH_BASE}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.error(f"[AutoFix] Failed to get base branch: {resp.status}")
                    await update_fix_status(fix_id, "failed")
                    return None
                base_sha = (await resp.json())["object"]["sha"]

            # 2. Create fix branch
            async with session.post(
                f"{_GH_API}/repos/{AUTOFIX_REPO}/git/refs",
                headers=headers,
                json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 201):
                    error_text = await resp.text()
                    # Branch may already exist — try to continue
                    if "Reference already exists" not in error_text:
                        logger.error(f"[AutoFix] Failed to create branch: {resp.status}")
                        await update_fix_status(fix_id, "failed")
                        return None

            # 3. Fetch current file content + SHA
            full_path = f"{AUTOFIX_BOT_DIR}/{file_path}"
            async with session.get(
                f"{_GH_API}/repos/{AUTOFIX_REPO}/contents/{full_path}",
                headers=headers,
                params={"ref": branch_name},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.error(f"[AutoFix] Failed to fetch file: {resp.status}")
                    await update_fix_status(fix_id, "failed")
                    return None
                file_data = await resp.json()
                current_content = base64.b64decode(file_data["content"]).decode("utf-8")
                file_sha = file_data["sha"]

            # 4. Apply fix (string replacement)
            if original_code not in current_content:
                logger.error(f"[AutoFix] Original code not found in {file_path}")
                await update_fix_status(fix_id, "failed")
                return None

            updated_content = current_content.replace(original_code, fixed_code, 1)

            # 5. Commit updated file
            async with session.put(
                f"{_GH_API}/repos/{AUTOFIX_REPO}/contents/{full_path}",
                headers=headers,
                json={
                    "message": f"fix({fix.get('error_key', 'unknown')[:30]}): {fix['diagnosis'][:50]}",
                    "content": base64.b64encode(updated_content.encode("utf-8")).decode("ascii"),
                    "sha": file_sha,
                    "branch": branch_name,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 201):
                    error_text = await resp.text()
                    logger.error(f"[AutoFix] Failed to commit: {resp.status} {error_text[:200]}")
                    await update_fix_status(fix_id, "failed")
                    return None

            # 6. Create PR
            pr_body = (
                f"## L2 Auto-Fix\n\n"
                f"**Error:** `{fix.get('error_key', '')}`\n"
                f"**Category:** {fix.get('file_path', '')}\n"
                f"**Diagnosis:** {fix['diagnosis']}\n\n"
                f"**ArchGate:** {fix['archgate_eval']}\n\n"
                f"> Generated by Aist Bot L2 Auto-Fixer (WP-45)"
            )

            async with session.post(
                f"{_GH_API}/repos/{AUTOFIX_REPO}/pulls",
                headers=headers,
                json={
                    "title": f"fix: {fix['diagnosis'][:60]}",
                    "body": pr_body,
                    "head": branch_name,
                    "base": AUTOFIX_BRANCH_BASE,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (200, 201):
                    pr_data = await resp.json()
                    pr_url = pr_data["html_url"]
                    await update_fix_status(fix_id, "applied", pr_url=pr_url, branch_name=branch_name)
                    logger.info(f"[AutoFix] PR created: {pr_url}")
                    return pr_url
                else:
                    error_text = await resp.text()
                    logger.error(f"[AutoFix] Failed to create PR: {resp.status} {error_text[:200]}")
                    await update_fix_status(fix_id, "failed")
                    return None

    except Exception as e:
        logger.error(f"[AutoFix] apply_fix exception: {e}")
        await update_fix_status(fix_id, "failed")
        return None


async def reject_fix(fix_id: int) -> None:
    """Mark fix as rejected."""
    await update_fix_status(fix_id, "rejected")
    logger.info(f"[AutoFix] Fix #{fix_id} rejected")


async def run_autofix_cycle(bot: Bot, dev_chat_id: str) -> int:
    """Full L2 auto-fix cycle. Called from scheduler every 15 min.

    Returns number of proposals sent.
    """
    if not GITHUB_BOT_PAT:
        return 0

    errors = await get_l2_fixable_errors(
        minutes=15, min_count=3, limit=AUTOFIX_MAX_PROPOSALS
    )

    if not errors:
        return 0

    proposals_sent = 0

    for error in errors:
        if proposals_sent >= AUTOFIX_MAX_PROPOSALS:
            break

        # Extract file paths from traceback
        file_paths = extract_files_from_traceback(error.get("traceback") or "")
        if not file_paths:
            logger.info(
                f"[AutoFix] No fixable files in traceback for {error['error_key']}"
            )
            continue

        # Fetch file contents from GitHub
        file_contents = {}
        for fp in file_paths[:2]:  # limit to 2 files for context window
            result = await fetch_github_file(fp)
            if result:
                file_contents[fp] = result[0]

        if not file_contents:
            continue

        # Diagnose with Claude
        diagnosis = await diagnose_with_claude(error, file_contents)
        if not diagnosis:
            continue

        # Verify proposed file is accessible
        target_file = diagnosis.get("file_path", "")
        if target_file in AUTOFIX_PROTECTED:
            logger.info(f"[AutoFix] Claude proposed fix for protected file {target_file}, skipping")
            continue

        # Send proposal to TG
        fix_id = await send_fix_proposal(bot, dev_chat_id, error, diagnosis)
        if fix_id:
            proposals_sent += 1

    if proposals_sent > 0:
        logger.info(f"[AutoFix] Cycle complete: {proposals_sent} proposals sent")

    return proposals_sent
