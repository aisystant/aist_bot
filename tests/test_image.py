"""Клиент и хендлеры окна службы Image (РП-614).

Клиент проверяется на настоящем HTTP-сервере aiohttp (путь, токен, тело, ожидание
задания); хендлеры — с подменой клиента службы и сообщения, без сети и Telegram.
"""

from types import SimpleNamespace

import pytest
from aiogram.dispatcher.event.bases import SkipHandler
from aiohttp import web

from clients import image_service as client_module
from clients.image_service import ImageServiceClient, ImageServiceError
from handlers import image as h


class FakeService:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict | None, str]] = []
        self.replies: list[tuple[int, dict]] = []

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else None
        self.requests.append((request.method, request.path, body, request.headers.get("Authorization", "")))
        status, payload = self.replies.pop(0)
        return web.json_response(payload, status=status)


@pytest.fixture
async def service(monkeypatch):
    monkeypatch.setattr(client_module, "POLL_INTERVAL_SECONDS", 0)
    fake = FakeService()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    fake.client = ImageServiceClient(f"http://127.0.0.1:{port}/", "tok")
    yield fake
    await runner.cleanup()


# --- клиент на настоящем сервере ------------------------------------------

async def test_submit_sends_kind_prompt_user_and_token(service):
    service.replies.append((202, {"job_id": "j1"}))

    assert await service.client.submit("video", "кот", 42, "Alyona") == "j1"
    assert service.requests == [
        ("POST", "/v1/jobs", {"kind": "video", "prompt": "кот", "user": {"id": "42", "username": "Alyona"}},
         "Bearer tok"),
    ]


async def test_submit_refusal_carries_status_and_limit(service):
    service.replies.append((429, {"error": "limit_reached", "limit": 5}))

    with pytest.raises(ImageServiceError) as err:
        await service.client.submit("video", "кот", 42, None)
    assert (err.value.status, err.value.limit) == (429, 5)


async def test_wait_polls_until_done(service):
    service.replies += [(200, {"status": "running"}), (200, {"status": "done", "urls": ["https://cdn/x.png"]})]

    assert await service.client.wait("j1") == ["https://cdn/x.png"]
    assert [r[1] for r in service.requests] == ["/v1/jobs/j1", "/v1/jobs/j1"]


async def test_wait_raises_on_failed_job(service):
    service.replies.append((200, {"status": "failed", "urls": []}))

    with pytest.raises(ImageServiceError):
        await service.client.wait("j1")


async def test_unreachable_service_is_status_zero():
    client = ImageServiceClient("http://127.0.0.1:9", "tok")
    with pytest.raises(ImageServiceError) as err:
        await client.submit("image", "x", 1, None)
    assert err.value.status == 0


# --- хендлеры с подменой --------------------------------------------------

class FakeMessage:
    def __init__(self, chat_type: str = "private") -> None:
        self.chat = SimpleNamespace(type=chat_type)
        self.from_user = SimpleNamespace(id=7, username="tserentserenov")
        self.answers: list[str] = []
        self.photos: list[str] = []

    async def answer(self, text, parse_mode=None):
        self.answers.append(text)

    async def answer_photo(self, file):
        self.photos.append((file.filename, file.data))


class FakeClient:
    def __init__(self, submit_error: ImageServiceError | None = None) -> None:
        self.submit_error = submit_error
        self.submitted: list[tuple] = []

    async def submit(self, kind, prompt, user_id, username):
        if self.submit_error:
            raise self.submit_error
        self.submitted.append((kind, prompt, user_id, username))
        return "j1"

    async def wait(self, job_id):
        return ["https://cdn/robot.png"]


def _command(args):
    return SimpleNamespace(args=args)


async def _run_pending():
    for task in list(h._pending):
        await task


async def test_image_command_submits_and_uploads_downloaded_photo(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(h, "_client", lambda: client)
    async def fake_download(url, max_bytes):
        return b"PNG"
    monkeypatch.setattr(h, "download", fake_download)
    message = FakeMessage()

    await h.cmd_image(message, _command(" красный робот "))
    await _run_pending()

    assert client.submitted == [("image", "красный робот", 7, "tserentserenov")]
    assert message.answers == ["Делаю картинку, это займёт около минуты."]
    assert message.photos == [("robot.png", b"PNG")]


async def test_file_too_big_for_telegram_is_sent_as_link(monkeypatch):
    monkeypatch.setattr(h, "_client", lambda: FakeClient())
    async def too_big(url, max_bytes):
        return None
    monkeypatch.setattr(h, "download", too_big)
    message = FakeMessage()

    await h.cmd_image(message, _command("робот"))
    await _run_pending()

    assert message.answers[-1] == "Готово, но файл слишком большой для Telegram. Ссылка: https://cdn/robot.png"
    assert message.photos == []


async def test_download_respects_size_cap():
    async def big(request):
        return web.Response(body=b"x" * 100)
    app = web.Application()
    app.router.add_get("/f", big)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        assert await client_module.download(f"http://127.0.0.1:{port}/f", 10) is None
        assert await client_module.download(f"http://127.0.0.1:{port}/f", 100) == b"x" * 100
    finally:
        await runner.cleanup()


async def test_refusal_from_service_is_explained(monkeypatch):
    monkeypatch.setattr(h, "_client", lambda: FakeClient(ImageServiceError(403, "not_allowed")))
    message = FakeMessage()

    await h.cmd_video(message, _command("кот"))

    assert message.answers == [h.REFUSALS[403]]


async def test_limit_text_includes_limit(monkeypatch):
    monkeypatch.setattr(h, "_client", lambda: FakeClient(ImageServiceError(429, "limit_reached", 5)))
    message = FakeMessage()

    await h.cmd_video(message, _command("кот"))

    assert message.answers == ["На сегодня лимит исчерпан (5 на всех). Завтра снова можно."]


async def test_empty_prompt_shows_usage(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(h, "_client", lambda: client)
    message = FakeMessage()

    await h.cmd_image(message, _command(None))

    assert message.answers == [h.USAGE_TEXT]
    assert client.submitted == []


async def test_group_chat_and_missing_config_pass_the_command_on(monkeypatch):
    monkeypatch.setattr(h, "_client", lambda: FakeClient())
    with pytest.raises(SkipHandler):
        await h.cmd_image(FakeMessage("group"), _command("робот"))

    monkeypatch.setattr(h, "_client", lambda: None)
    with pytest.raises(SkipHandler):
        await h.cmd_image(FakeMessage(), _command("робот"))
