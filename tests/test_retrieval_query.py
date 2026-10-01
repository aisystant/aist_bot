"""Unit-тесты РП-498 Ф17: запрос предпоиска знаний для короткой реплики в живом диалоге.

Предпоиск знаний (collect_pre_search и P3-вставка в handle_question_with_tools) искал по тексту реплики
как есть: на «да» после предложения бота «опишем эту роль?» поиск возвращал случайные фрагменты, и модель
получала их как «результаты по запросу пользователя». Теперь короткая реплика ищется вместе с прошлым вопросом.
"""

import os
import sys
from unittest.mock import patch

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

from engines.shared import question_handler as qh  # noqa: E402
from engines.shared.question_handler import build_retrieval_query  # noqa: E402

PREVIOUS_QUESTION = "Опиши, чем занимается роль «Наставник» в программе личного развития"
OFFER = "Хочешь — опишем, что конкретно делает эта роль сейчас?"


def dialog(*pairs: tuple[str, str], reply: str) -> list[dict]:
    """История диалога в виде messages для модели; последним идёт сама реплика."""
    messages = []
    for question, answer in pairs:
        messages += [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
    return messages + [{"role": "user", "content": reply}]


# =============================================================================
# build_retrieval_query
# =============================================================================

@pytest.mark.parametrize("history", [None, []])
def test_without_history_the_query_is_the_question(history):
    assert build_retrieval_query("да", history) == "да"


def test_long_reply_is_searched_as_is():
    reply = "расскажи подробнее про второй пункт из твоего ответа"
    messages = dialog((PREVIOUS_QUESTION, "Ответ. " + OFFER), reply=reply)

    assert build_retrieval_query(reply, messages) == reply


def test_short_reply_is_searched_together_with_the_previous_question():
    messages = dialog((PREVIOUS_QUESTION, "Ответ. " + OFFER), reply="да")

    assert build_retrieval_query("да", messages) == f"{PREVIOUS_QUESTION} да"


def test_earlier_short_replies_are_skipped_back_to_the_last_substantive_question():
    messages = dialog((PREVIOUS_QUESTION, "Ответ. " + OFFER), ("да", "Продолжение. Ещё?"), reply="а ещё?")

    assert build_retrieval_query("а ещё?", messages) == f"{PREVIOUS_QUESTION} а ещё?"


def test_short_reply_without_any_substantive_question_stays_as_is():
    messages = dialog(("да", "Хорошо"), reply="ок")

    assert build_retrieval_query("ок", messages) == "ок"


def test_previous_question_is_cut_to_300_characters():
    long_question = "слово " * 200
    messages = dialog((long_question, "Ответ"), reply="да")

    assert build_retrieval_query("да", messages) == f"{long_question[:300]} да"


def test_messages_with_non_text_content_are_ignored():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": PREVIOUS_QUESTION}]},
        {"role": "assistant", "content": "Ответ"},
        {"role": "user", "content": "да"},
    ]

    assert build_retrieval_query("да", messages) == "да"


# =============================================================================
# Подключение: оба предпоиска получают собранный запрос
# =============================================================================

class StopHere(BaseException):
    """Прерывает handle_question_with_tools после предпоиска, минуя его `except Exception`."""


async def run_until_presearch(question: str, messages: list[dict] | None) -> dict:
    """Запускает обработчик до P3-поиска; возвращает запросы, которые получили оба предпоиска."""
    seen: dict = {}

    async def fake_assemble(**kwargs):
        seen["context_pipeline"] = kwargs["question"]
        return {}

    async def fake_search(query, *args, **kwargs):
        seen["p3"] = query
        raise StopHere()

    with patch("engines.shared.context_pipeline.assemble_context", new=fake_assemble), \
         patch.object(qh.gateway_mcp, "knowledge_search", new=fake_search):
        with pytest.raises(StopHere):
            await qh.handle_question_with_tools(
                question=question, intern={"chat_id": 1, "language": "ru"}, tier=1, conversation_messages=messages,
            )
    return seen


@pytest.mark.asyncio
async def test_both_presearches_use_the_dialog_query_for_a_short_reply():
    messages = dialog((PREVIOUS_QUESTION, "Ответ. " + OFFER), reply="да")

    seen = await run_until_presearch("да", messages)

    assert seen == {"context_pipeline": f"{PREVIOUS_QUESTION} да", "p3": f"{PREVIOUS_QUESTION} да"}


@pytest.mark.asyncio
async def test_both_presearches_keep_a_long_question_as_is():
    question = "расскажи подробнее про второй пункт из твоего ответа"
    messages = dialog((PREVIOUS_QUESTION, "Ответ. " + OFFER), reply=question)

    seen = await run_until_presearch(question, messages)

    assert seen == {"context_pipeline": question, "p3": question}


@pytest.mark.asyncio
async def test_both_presearches_keep_the_question_without_history():
    seen = await run_until_presearch("да", None)

    assert seen == {"context_pipeline": "да", "p3": "да"}
