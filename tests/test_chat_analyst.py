"""Клиент и хендлеры «Аналитика чата» (РП-612).

Клиент проверяется на настоящем HTTP-сервере aiohttp (путь, заголовок, тело,
повторы); хендлеры — с подменой клиента сервиса и бота, без сети и Telegram.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import ChatMemberAdministrator, ChatMemberLeft, ChatMemberMember
from aiohttp import web

from clients import chat_analyst as client_module
from clients.chat_analyst import ChatAnalystClient, ChatAnalystError
from handlers import chat_analyst as h

OWNER = 111
CHAT = -100500
TERMS = "https://example.test/terms"
SENT = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


# --- клиент на настоящем сервере ------------------------------------------

class FakeService:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict | None, str]] = []
        self.replies: list[tuple[int, dict]] = []

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else None
        self.requests.append((request.method, request.path, body, request.headers.get("Authorization", "")))
        status, payload = self.replies.pop(0) if self.replies else (202, {})
        return web.json_response(payload, status=status)


@pytest.fixture
async def service(aiohttp_unused=None):
    fake = FakeService()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    fake.client = ChatAnalystClient(f"http://127.0.0.1:{port}/", "tok")
    yield fake
    await runner.cleanup()


async def test_client_sends_path_token_and_body(service):
    await service.client.register(CHAT, 1.5, 2.5)
    await service.client.send_message(CHAT, 7, 42, "hi", 3.5)

    assert service.requests == [
        ("POST", f"/chats/{CHAT}/register", {"added_at": 1.5, "notified_at": 2.5}, "Bearer tok"),
        ("POST", f"/chats/{CHAT}/messages",
         {"version": 1, "message_id": 7, "sender_id": 42, "text": "hi", "sent_at": 3.5}, "Bearer tok"),
    ]


async def test_register_retries_on_503_then_succeeds(service, monkeypatch):
    monkeypatch.setattr(client_module, "CONTROL_RETRY_DELAY_SECONDS", 0)
    service.replies = [(503, {"detail": "storage unavailable"}), (202, {})]

    await service.client.register(CHAT, 1.0, 2.0)

    assert len(service.requests) == 2


async def test_register_does_not_retry_a_409(service, monkeypatch):
    monkeypatch.setattr(client_module, "CONTROL_RETRY_DELAY_SECONDS", 0)
    service.replies = [(409, {"detail": "RegistrationConflict"})]

    with pytest.raises(ChatAnalystError) as caught:
        await service.client.register(CHAT, 1.0, 2.0)

    assert (caught.value.status, caught.value.detail) == (409, "RegistrationConflict")
    assert len(service.requests) == 1


async def test_message_is_a_single_attempt(service):
    service.replies = [(503, {"detail": "storage unavailable"})]

    with pytest.raises(ChatAnalystError) as caught:
        await service.client.send_message(CHAT, 1, 2, "x", 3.0)

    assert caught.value.status == 503
    assert len(service.requests) == 1


async def test_digest_passes_participant_only_when_given(service):
    service.replies = [(200, {"digest": "A"}), (200, {"digest": "B"})]

    first = await service.client.digest(CHAT, OWNER, 10.0)
    second = await service.client.digest(CHAT, OWNER, 10.0, participant_id=5)

    assert (first, second) == ("A", "B")
    assert service.requests[0][2] == {"requesting_admin_id": OWNER, "since": 10.0}
    assert service.requests[1][2] == {"requesting_admin_id": OWNER, "since": 10.0, "participant_id": 5}


async def test_unreachable_service_is_status_zero():
    client = ChatAnalystClient("http://127.0.0.1:1", "tok")

    with pytest.raises(ChatAnalystError) as caught:
        await client.status(CHAT)

    assert caught.value.status == 0


# --- хендлеры -------------------------------------------------------------

class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.state = "active"
        self.fail: dict[str, ChatAnalystError] = {}
        self.digest_text = "digest text"

    async def _do(self, name: str, *args):
        self.calls.append((name, *args))
        if name in self.fail:
            raise self.fail[name]

    async def register(self, chat_id, added_at, notified_at):
        await self._do("register", chat_id, added_at, notified_at)

    async def unregister(self, chat_id):
        await self._do("unregister", chat_id)

    async def status(self, chat_id):
        await self._do("status", chat_id)
        return {"state": self.state}

    async def send_message(self, chat_id, message_id, sender_id, text, sent_at):
        await self._do("send_message", chat_id, message_id, sender_id, text, sent_at)

    async def digest(self, chat_id, admin_id, since, participant_id=None):
        await self._do("digest", chat_id, admin_id, participant_id)
        return self.digest_text


class FakeBot:
    def __init__(self, order: list) -> None:
        self.order = order
        self.sent: list[tuple[int, str]] = []
        self.fail_pin = False

    async def send_message(self, chat_id, text):
        self.order.append(("send", chat_id))
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=900)

    async def pin_chat_message(self, chat_id, message_id):
        self.order.append(("pin", chat_id))
        if self.fail_pin:
            from aiogram.exceptions import TelegramAPIError
            raise TelegramAPIError(method=None, message="no rights")


@pytest.fixture
def fake(monkeypatch):
    client = FakeClient()
    cfg = h.Config(client, frozenset({CHAT}), TERMS, OWNER)
    monkeypatch.setattr(h, "_config", lambda: cfg)
    for state in (h._active, h._suspended, h._status_checked_at):
        state.clear()
    yield client
    for state in (h._active, h._suspended, h._status_checked_at):
        state.clear()


def admin(can_pin: bool = True):
    return ChatMemberAdministrator.model_construct(can_pin_messages=can_pin)


def membership(*, new, old, adder=OWNER, username=None, chat_type="supergroup", chat_id=CHAT):
    order: list = []
    bot = FakeBot(order)
    update = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=chat_type, title="Поток", username=username),
        from_user=SimpleNamespace(id=adder),
        old_chat_member=old, new_chat_member=new, date=SENT, bot=bot,
    )
    return update, bot, order


def chat_steps(order):
    """Шаги, адресованные самому чату (личные сообщения владельцу не считаются)."""
    return [step for step in order if step[1] == CHAT]


def group_message(text="привет", *, chat_id=CHAT, sender_id=42, is_bot=False, caption=None):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        from_user=SimpleNamespace(id=sender_id, is_bot=is_bot),
        text=text, caption=caption, message_id=77, date=SENT,
    )


async def deliver_group_message(message):
    with pytest.raises(SkipHandler):
        await h.on_group_message(message)
    await asyncio.gather(*h._background)


async def test_message_of_an_active_chat_is_forwarded(fake):
    await deliver_group_message(group_message("привет"))

    assert ("send_message", CHAT, 77, 42, "привет", SENT.timestamp()) in fake.calls


async def test_message_is_not_forwarded_while_the_service_says_collection_is_off(fake):
    fake.state = "unknown"

    await deliver_group_message(group_message())
    await deliver_group_message(group_message())  # в пределах минуты статус не запрашивается снова

    assert [c[0] for c in fake.calls] == ["status"]


@pytest.mark.parametrize("message", [
    group_message(chat_id=-1),                  # чат не в списке разрешённых
    group_message("/chat_digest -100500"),      # команда боту, не содержимое чата
    group_message(is_bot=True),                 # сообщение бота
    group_message(text=None),                   # нет текста
])
async def test_messages_that_must_not_be_collected_make_no_service_call(fake, message):
    await deliver_group_message(message)

    assert fake.calls == []


async def test_caption_is_collected_as_text(fake):
    await deliver_group_message(group_message(text=None, caption="подпись"))

    assert any(c[0] == "send_message" and c[4] == "подпись" for c in fake.calls)


async def test_frozen_answer_stops_further_forwarding(fake):
    h._active.add(CHAT)
    fake.fail["send_message"] = ChatAnalystError(409, "ChatFrozen")

    await deliver_group_message(group_message())

    assert CHAT not in h._active


async def test_service_outage_drops_the_message_and_keeps_collecting(fake):
    h._active.add(CHAT)
    fake.fail["send_message"] = ChatAnalystError(0, "TimeoutError")

    await deliver_group_message(group_message())

    assert CHAT in h._active


async def test_bot_made_admin_notifies_pins_then_registers_in_that_order(fake):
    fake.state = "unknown"
    update, bot, order = membership(new=admin(), old=ChatMemberLeft.model_construct())

    with pytest.raises(SkipHandler):
        await h.on_bot_membership(update)

    assert [step for step in order if step[1] == CHAT] == [("send", CHAT), ("pin", CHAT)]
    assert order[-1] == ("send", OWNER)  # владельцу сообщаем о включении после регистрации
    notice = bot.sent[0][1]
    assert TERMS in notice and "по отдельному участнику" in notice
    register = next(c for c in fake.calls if c[0] == "register")
    assert register[1] == CHAT and register[2] == SENT.timestamp() and register[3] >= register[2]
    assert CHAT in h._active
    assert (OWNER, "Сбор по чату «Поток» включён, участники уведомлены.") in bot.sent


async def test_returned_bot_starts_a_new_epoch_before_registering(fake):
    fake.state = "active"
    update, _, _ = membership(new=admin(), old=ChatMemberLeft.model_construct())

    with pytest.raises(SkipHandler):
        await h.on_bot_membership(update)

    names = [c[0] for c in fake.calls]
    assert names == ["status", "unregister", "register"]


async def test_failed_pin_means_no_registration_and_a_report_to_the_owner(fake):
    fake.state = "unknown"
    update, bot, _ = membership(new=admin(), old=ChatMemberLeft.model_construct())
    bot.fail_pin = True

    with pytest.raises(SkipHandler):
        await h.on_bot_membership(update)

    assert not any(c[0] == "register" for c in fake.calls)
    assert CHAT not in h._active
    assert bot.sent[-1][0] == OWNER and "не включён" in bot.sent[-1][1]


async def test_admin_without_pin_right_gets_a_hint_and_nothing_is_sent_to_the_chat(fake):
    update, bot, order = membership(new=admin(can_pin=False), old=ChatMemberLeft.model_construct())

    with pytest.raises(SkipHandler):
        await h.on_bot_membership(update)

    assert chat_steps(order) == [] and fake.calls == []
    assert bot.sent == [(OWNER, "Сбор по чату «Поток» не включён: дайте боту право закреплять сообщения.")]


@pytest.mark.parametrize("kwargs", [
    {"username": "public_group"},   # публичный чат
    {"adder": 999},                 # добавил не владелец
    {"chat_type": "channel"},       # не группа
    {"chat_id": -1},                # чат не в списке
])
async def test_membership_that_must_not_start_collection_does_nothing(fake, kwargs):
    update, bot, order = membership(new=admin(), old=ChatMemberLeft.model_construct(), **kwargs)

    with pytest.raises(SkipHandler):
        await h.on_bot_membership(update)

    assert chat_steps(order) == [] and not any(c[0] in ("register", "unregister") for c in fake.calls)


async def test_service_down_at_start_sends_nothing_to_the_chat(fake):
    fake.fail["status"] = ChatAnalystError(0, "ClientError")
    update, bot, order = membership(new=admin(), old=ChatMemberLeft.model_construct())

    with pytest.raises(SkipHandler):
        await h.on_bot_membership(update)

    assert chat_steps(order) == []
    assert "сервис недоступен" in bot.sent[-1][1]


async def test_bot_removed_unregisters_and_stops_collection(fake):
    h._active.add(CHAT)
    update, _, _ = membership(new=ChatMemberLeft.model_construct(), old=admin())

    with pytest.raises(SkipHandler):
        await h.on_bot_membership(update)

    assert ("unregister", CHAT) in fake.calls
    assert CHAT not in h._active


async def test_losing_admin_rights_also_unregisters(fake):
    h._active.add(CHAT)
    update, _, _ = membership(new=ChatMemberMember.model_construct(), old=admin())

    with pytest.raises(SkipHandler):
        await h.on_bot_membership(update)

    assert ("unregister", CHAT) in fake.calls


async def test_a_crashing_handler_still_passes_the_event_on(fake, monkeypatch):
    async def boom(*_):
        raise RuntimeError("boom")

    monkeypatch.setattr(h, "_handle_membership", boom)
    update, _, _ = membership(new=admin(), old=ChatMemberLeft.model_construct())

    with pytest.raises(SkipHandler):
        await h.on_bot_membership(update)


# --- /chat_digest ---------------------------------------------------------

def command(text, *, sender_id=OWNER, chat_type="private"):
    answers: list[str] = []

    async def answer(reply, parse_mode=None):
        answers.append(reply)

    message = SimpleNamespace(
        text=text, chat=SimpleNamespace(type=chat_type), from_user=SimpleNamespace(id=sender_id), answer=answer
    )
    return message, answers


async def test_digest_goes_to_the_owner_with_participant_and_window(fake, monkeypatch):
    monkeypatch.setattr(h.time, "time", lambda: 1_000_000.0)
    message, answers = command(f"/chat_digest {CHAT} 3 user=555")

    await h.cmd_chat_digest(message)

    assert answers == ["digest text"]
    assert ("digest", CHAT, OWNER, 555) in fake.calls


@pytest.mark.parametrize("text", [
    "/chat_digest", "/chat_digest abc", "/chat_digest -100500 0", f"/chat_digest {CHAT} 91",
    f"/chat_digest {CHAT} user=x",
])
async def test_bad_arguments_get_usage_and_no_service_call(fake, text):
    message, answers = command(text)

    await h.cmd_chat_digest(message)

    assert answers == [h.USAGE_TEXT] and fake.calls == []


@pytest.mark.parametrize("kwargs", [{"sender_id": 999}, {"chat_type": "supergroup"}])
async def test_digest_is_silent_for_non_owner_and_for_group_chats(fake, kwargs):
    message, answers = command(f"/chat_digest {CHAT}", **kwargs)

    with pytest.raises(SkipHandler):  # команда идёт дальше, SC.118 в группе её не теряет
        await h.cmd_chat_digest(message)

    assert answers == [] and fake.calls == []


@pytest.mark.parametrize("status,expected", [
    (403, h.DIGEST_ERRORS[403]), (404, h.DIGEST_ERRORS[404]), (409, h.DIGEST_ERRORS[409]),
    (413, h.DIGEST_ERRORS[413]), (503, h.SERVICE_DOWN_TEXT), (0, h.SERVICE_DOWN_TEXT),
])
async def test_service_refusals_are_explained_to_the_owner(fake, status, expected):
    fake.fail["digest"] = ChatAnalystError(status, "x")
    message, answers = command(f"/chat_digest {CHAT}")

    await h.cmd_chat_digest(message)

    assert answers == [expected]


async def test_long_digest_is_split_under_the_telegram_limit(fake):
    fake.digest_text = "я" * (h.TELEGRAM_TEXT_LIMIT + 10)
    message, answers = command(f"/chat_digest {CHAT}")

    await h.cmd_chat_digest(message)

    assert [len(a) for a in answers] == [h.TELEGRAM_TEXT_LIMIT, 10]


def test_module_is_off_without_full_configuration(monkeypatch):
    for name in ("CHAT_ANALYST_URL", "CHAT_ANALYST_TOKEN", "CHAT_ANALYST_CHATS", "CHAT_ANALYST_TERMS_URL"):
        monkeypatch.setenv(name, "x" if name != "CHAT_ANALYST_CHATS" else "1")
    monkeypatch.setenv("DEVELOPER_CHAT_ID", "5")
    assert h._config() is not None

    monkeypatch.delenv("CHAT_ANALYST_TERMS_URL")
    assert h._config() is None


async def test_failed_unregister_keeps_collection_off_even_if_service_still_says_active(fake):
    h._active.add(CHAT)
    fake.fail["unregister"] = ChatAnalystError(503, "")
    update, _, _ = membership(new=ChatMemberMember.model_construct(), old=admin())
    with pytest.raises(SkipHandler):
        await h.on_bot_membership(update)
    fake.calls.clear()

    await deliver_group_message(group_message())

    assert not any(c[0] == "send_message" for c in fake.calls)


async def test_plain_group_admin_can_pin_without_the_field(fake):
    fake.state = "unknown"
    update, bot, order = membership(
        new=admin(can_pin=None), old=ChatMemberLeft.model_construct(), chat_type="group"
    )

    with pytest.raises(SkipHandler):
        await h.on_bot_membership(update)

    assert [s for s in order if s[1] == CHAT] == [("send", CHAT), ("pin", CHAT)]


def test_garbage_in_owner_id_turns_the_module_off(monkeypatch):
    for name in ("CHAT_ANALYST_URL", "CHAT_ANALYST_TOKEN", "CHAT_ANALYST_TERMS_URL"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("CHAT_ANALYST_CHATS", "1")
    monkeypatch.setenv("DEVELOPER_CHAT_ID", "not-a-number")

    assert h._config() is None


async def test_error_detail_keeps_only_short_codes(service):
    service.replies = [(409, {"detail": "ChatFrozen"}), (422, {"detail": [{"loc": ["body"], "type": "x"}]}),
                       (400, {"detail": "some free text with words"})]

    details = []
    for _ in range(3):
        with pytest.raises(ChatAnalystError) as caught:
            await service.client.send_message(CHAT, 1, 2, "x", 3.0)
        details.append(caught.value.detail)

    assert details == ["ChatFrozen", "", ""]
