"""
Auto-triage для QA feedback (WP-7 F3).

Вызывается при helpful=false или user_comment:
1. LLM classify (Haiku) → category + severity + cluster
2. INSERT feedback_triage
3. IF severity >= high OR has_comment → TG alert

Роль: R7 Триажёр техдолга (Grade 1 auto-classify).
Source-of-truth: DP.AGENT.001 R7, PROCESSES.md §6.
"""

import asyncio
import json
import os
import logging
from typing import Optional

import aiohttp

from db.connection import acquire

logger = logging.getLogger(__name__)

# Промпт для Haiku-классификации
_CLASSIFY_PROMPT = """Classify this user feedback about an educational bot.

Question: {question}
Answer snippet: {answer_snippet}
User comment: {comment}
Feedback type: {feedback_type}

Respond in JSON only:
{{
  "category": "L|C|U|K",
  "severity": "low|medium|high|critical",
  "cluster": "<short_label>",
  "reason": "<one_sentence>"
}}

Categories:
- L (Latency): slow response, timeout, loading
- C (Correctness): wrong answer, bug, error, crash
- U (Usability): confusing UX, missing feature, unclear flow
- K (Knowledge): missing knowledge, bad explanation, wrong topic

Severity:
- low: cosmetic or rare edge case
- medium: affects understanding but workarounds exist
- high: blocks user goal or repeats ≥3 times
- critical: data loss, crash, or security issue"""


async def _classify_with_haiku(
    question: str, answer_snippet: str,
    comment: Optional[str], feedback_type: str
) -> dict:
    """Classify feedback using Haiku. Returns dict with category/severity/cluster/reason."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("[FeedbackTriage] No ANTHROPIC_API_KEY, using defaults")
        return {"category": "unknown", "severity": "medium", "cluster": "unclassified", "reason": "No API key"}

    prompt = _CLASSIFY_PROMPT.format(
        question=question[:200],
        answer_snippet=(answer_snippet or "")[:200],
        comment=comment or "(no comment)",
        feedback_type=feedback_type,
    )

    from config import CLAUDE_MODEL_HAIKU
    payload = {
        "model": CLAUDE_MODEL_HAIKU,
        "max_tokens": 200,
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"[FeedbackTriage] Haiku API {resp.status}")
                    return {"category": "unknown", "severity": "medium", "cluster": "api_error", "reason": f"HTTP {resp.status}"}
                data = await resp.json()
                text = data["content"][0]["text"]
                # Parse JSON from response (may have markdown fences)
                text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                result = json.loads(text)
                # Validate
                valid_cats = {"L", "C", "U", "K"}
                valid_sevs = {"low", "medium", "high", "critical"}
                if result.get("category") not in valid_cats:
                    result["category"] = "unknown"
                if result.get("severity") not in valid_sevs:
                    result["severity"] = "medium"
                return result
    except Exception as e:
        logger.error(f"[FeedbackTriage] classify error: {e}")
        return {"category": "unknown", "severity": "medium", "cluster": "classify_error", "reason": str(e)[:100]}


async def _save_triage(
    qa_id: int, chat_id: int, question: str, answer_snippet: Optional[str],
    classification: dict, has_comment: bool, user_comment: Optional[str]
) -> Optional[int]:
    """Save triage result to DB. Returns triage ID or None on conflict."""
    async with await acquire() as conn:
        try:
            row = await conn.fetchrow("""
                INSERT INTO feedback_triage
                    (qa_id, chat_id, question, answer_snippet,
                     category, severity, cluster, reason,
                     has_comment, user_comment)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (qa_id) DO UPDATE SET
                    category = EXCLUDED.category,
                    severity = EXCLUDED.severity,
                    cluster = EXCLUDED.cluster,
                    reason = EXCLUDED.reason,
                    has_comment = EXCLUDED.has_comment,
                    user_comment = COALESCE(EXCLUDED.user_comment, feedback_triage.user_comment)
                RETURNING id
            """,
                qa_id, chat_id, question[:500], (answer_snippet or "")[:300],
                classification.get("category", "unknown"),
                classification.get("severity", "medium"),
                classification.get("cluster", ""),
                classification.get("reason", ""),
                has_comment, user_comment,
            )
            return row["id"] if row else None
        except Exception as e:
            logger.error(f"[FeedbackTriage] save error: {e}")
            return None


async def _send_alert(qa_id: int, chat_id: int, question: str,
                      classification: dict, user_comment: Optional[str]):
    """Send TG alert to developer for high-severity feedback."""
    dev_chat_id = os.getenv("DEVELOPER_CHAT_ID")
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not dev_chat_id or not bot_token:
        return

    sev = classification.get("severity", "?")
    cat = classification.get("category", "?")
    cluster = classification.get("cluster", "")
    reason = classification.get("reason", "")

    sev_emoji = {"critical": "\U0001f534", "high": "\U0001f7e0", "medium": "\U0001f7e1", "low": "\U0001f7e2"}.get(sev, "\u26aa")

    lines = [
        f"{sev_emoji} <b>Feedback Alert</b> [{sev.upper()}]",
        f"<b>Cat:</b> {cat} | <b>Cluster:</b> {cluster}",
        f"<b>Q:</b> {question[:100]}",
    ]
    if user_comment:
        lines.append(f"\u270f\ufe0f <b>Comment:</b> {user_comment[:200]}")
    if reason:
        lines.append(f"\U0001f4a1 {reason}")
    lines.append(f"<code>qa_id={qa_id} chat={chat_id}</code>")

    text = "\n".join(lines)

    try:
        from aiogram import Bot
        bot = Bot(token=bot_token)
        try:
            await bot.send_message(int(dev_chat_id), text, parse_mode="HTML")
            logger.info(f"[FeedbackTriage] Alert sent: qa_id={qa_id} sev={sev}")
            # Mark as notified
            async with await acquire() as conn:
                await conn.execute(
                    "UPDATE feedback_triage SET notified_at = NOW() WHERE qa_id = $1",
                    qa_id,
                )
        finally:
            await bot.session.close()
    except Exception as e:
        logger.error(f"[FeedbackTriage] Alert send error: {e}")


async def triage_feedback(qa_id: int, feedback_type: str = "not_helpful"):
    """Main entry point: classify + save + alert.

    Args:
        qa_id: ID from qa_history
        feedback_type: "not_helpful" or "comment"

    Called fire-and-forget from callbacks.py (helpful=false) and
    consultation.py (user_comment saved).
    """
    # Fetch QA data
    from db.queries.qa import get_qa_by_id
    qa = await get_qa_by_id(qa_id)
    if not qa:
        logger.warning(f"[FeedbackTriage] qa_id={qa_id} not found")
        return

    question = qa.get("question", "")
    answer = qa.get("answer", "")
    chat_id = qa.get("chat_id", 0)

    # Check for existing comment
    async with await acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_comment FROM qa_history WHERE id = $1", qa_id
        )
    user_comment = row["user_comment"] if row and row["user_comment"] else None
    has_comment = bool(user_comment)

    # Classify with Haiku
    classification = await _classify_with_haiku(
        question, answer[:300], user_comment, feedback_type
    )

    # Save to DB
    triage_id = await _save_triage(
        qa_id, chat_id, question, answer[:300],
        classification, has_comment, user_comment,
    )

    severity = classification.get("severity", "low")
    logger.info(
        f"[FeedbackTriage] qa_id={qa_id} → {classification.get('category')}/{severity}"
        f" cluster={classification.get('cluster')}"
    )

    # Alert if high/critical OR has user comment
    if severity in ("high", "critical") or has_comment:
        await _send_alert(qa_id, chat_id, question, classification, user_comment)
