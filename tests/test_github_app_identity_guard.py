"""WP-406: защитный код против настройки чужого GitHub App.

Покрывает: forbidden-id fail-fast, is_app_enabled(), verify_app_identity(),
verify_installation_belongs_to_app(). Реального GitHub App нет (регистрация
только руками пилота) — все сетевые вызовы мокнуты на уровне ClientSession,
тем же паттерном, что tests/test_health_check_l3.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import clients.github_app as github_app


def _mock_get_session(status: int, payload: dict):
    """Build a ClientSession mock whose get() returns `payload` as JSON."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=payload)
    resp.text = AsyncMock(return_value=f"mocked body for status {status}")

    get_cm = MagicMock()
    get_cm.__aenter__ = AsyncMock(return_value=resp)
    get_cm.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=get_cm)

    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    return session_cm


@pytest.fixture(autouse=True)
def _reset_identity_state():
    """Три-state флаг переживает между тестами модуля — сбрасываем."""
    github_app._app_identity_verified = None
    yield
    github_app._app_identity_verified = None


class TestIsAppEnabled:
    def test_default_disabled(self, monkeypatch):
        monkeypatch.delenv("GITHUB_APP_ENABLED", raising=False)
        assert github_app.is_app_enabled() is False

    @pytest.mark.parametrize("value", ["true", "1", "yes", "on", "True", "ON"])
    def test_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv("GITHUB_APP_ENABLED", value)
        assert github_app.is_app_enabled() is True

    @pytest.mark.parametrize("value", ["false", "0", "no", "", "garbage"])
    def test_falsy_values(self, monkeypatch, value):
        monkeypatch.setenv("GITHUB_APP_ENABLED", value)
        assert github_app.is_app_enabled() is False


class TestLoadAppCredentialsForbiddenId:
    def test_default_forbidden_id_rejected(self, monkeypatch):
        """Известный чужой App ID (WP-406) — отказ даже без явного GITHUB_APP_FORBIDDEN_IDS."""
        monkeypatch.delenv("GITHUB_APP_FORBIDDEN_IDS", raising=False)
        monkeypatch.setenv("GITHUB_APP_ID", "3261992")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "dummy-pem")
        with pytest.raises(RuntimeError, match="запрещённых"):
            github_app._load_app_credentials()

    def test_custom_forbidden_id_rejected(self, monkeypatch):
        monkeypatch.setenv("GITHUB_APP_FORBIDDEN_IDS", "111,222")
        monkeypatch.setenv("GITHUB_APP_ID", "222")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "dummy-pem")
        with pytest.raises(RuntimeError, match="запрещённых"):
            github_app._load_app_credentials()

    def test_valid_id_accepted(self, monkeypatch):
        monkeypatch.setenv("GITHUB_APP_FORBIDDEN_IDS", "3261992")
        monkeypatch.setenv("GITHUB_APP_ID", "9999999")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "dummy-pem")
        app_id, private_key = github_app._load_app_credentials()
        assert app_id == "9999999"
        assert private_key == "dummy-pem"

    def test_missing_credentials_still_raises(self, monkeypatch):
        monkeypatch.delenv("GITHUB_APP_ID", raising=False)
        monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
        with pytest.raises(RuntimeError, match="не установлены"):
            github_app._load_app_credentials()


class TestVerifyAppIdentity:
    async def test_matching_id_and_slug_verified(self, monkeypatch):
        monkeypatch.setenv("GITHUB_APP_ID", "9999999")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "dummy-pem")
        monkeypatch.setenv("GITHUB_APP_SLUG", "aisystant-personal-guide")
        monkeypatch.setattr(github_app, "generate_app_jwt", lambda: "fake-jwt")

        payload = {"id": 9999999, "slug": "aisystant-personal-guide"}
        with patch("clients.github_app.aiohttp.ClientSession", return_value=_mock_get_session(200, payload)):
            result = await github_app.verify_app_identity()

        assert result is True
        assert github_app.app_identity_status() is True

    async def test_id_mismatch_rejected(self, monkeypatch):
        monkeypatch.setenv("GITHUB_APP_ID", "9999999")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "dummy-pem")
        monkeypatch.delenv("GITHUB_APP_SLUG", raising=False)
        monkeypatch.setattr(github_app, "generate_app_jwt", lambda: "fake-jwt")

        payload = {"id": 3261992, "slug": "aisystant-knowledge"}  # чужой App
        with patch("clients.github_app.aiohttp.ClientSession", return_value=_mock_get_session(200, payload)):
            result = await github_app.verify_app_identity()

        assert result is False
        assert github_app.app_identity_status() is False

    async def test_slug_mismatch_rejected(self, monkeypatch):
        monkeypatch.setenv("GITHUB_APP_ID", "9999999")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "dummy-pem")
        monkeypatch.setenv("GITHUB_APP_SLUG", "expected-slug")
        monkeypatch.setattr(github_app, "generate_app_jwt", lambda: "fake-jwt")

        payload = {"id": 9999999, "slug": "different-slug"}
        with patch("clients.github_app.aiohttp.ClientSession", return_value=_mock_get_session(200, payload)):
            result = await github_app.verify_app_identity()

        assert result is False

    async def test_http_error_fails_closed(self, monkeypatch):
        monkeypatch.setenv("GITHUB_APP_ID", "9999999")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "dummy-pem")
        monkeypatch.setattr(github_app, "generate_app_jwt", lambda: "fake-jwt")

        with patch("clients.github_app.aiohttp.ClientSession", return_value=_mock_get_session(401, {})):
            result = await github_app.verify_app_identity()

        assert result is False
        assert github_app.app_identity_status() is False

    async def test_forbidden_id_short_circuits_without_network_call(self, monkeypatch):
        monkeypatch.setenv("GITHUB_APP_ID", "3261992")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "dummy-pem")
        session_ctor = MagicMock()
        with patch("clients.github_app.aiohttp.ClientSession", session_ctor):
            result = await github_app.verify_app_identity()

        assert result is False
        session_ctor.assert_not_called()


class TestVerifyInstallationBelongsToApp:
    async def test_matching_app_id_accepted(self, monkeypatch):
        monkeypatch.setenv("GITHUB_APP_ID", "9999999")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "dummy-pem")
        monkeypatch.setattr(github_app, "generate_app_jwt", lambda: "fake-jwt")

        payload = {"id": 555, "app_id": 9999999}
        with patch("clients.github_app.aiohttp.ClientSession", return_value=_mock_get_session(200, payload)):
            result = await github_app.verify_installation_belongs_to_app(555)

        assert result is True

    async def test_foreign_app_id_rejected(self, monkeypatch):
        monkeypatch.setenv("GITHUB_APP_ID", "9999999")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "dummy-pem")
        monkeypatch.setattr(github_app, "generate_app_jwt", lambda: "fake-jwt")

        payload = {"id": 555, "app_id": 3261992}  # installation чужого App
        with patch("clients.github_app.aiohttp.ClientSession", return_value=_mock_get_session(200, payload)):
            result = await github_app.verify_installation_belongs_to_app(555)

        assert result is False

    async def test_http_error_fails_closed(self, monkeypatch):
        monkeypatch.setenv("GITHUB_APP_ID", "9999999")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "dummy-pem")
        monkeypatch.setattr(github_app, "generate_app_jwt", lambda: "fake-jwt")

        with patch("clients.github_app.aiohttp.ClientSession", return_value=_mock_get_session(404, {})):
            result = await github_app.verify_installation_belongs_to_app(555)

        assert result is False

    async def test_network_exception_fails_closed(self, monkeypatch):
        monkeypatch.setenv("GITHUB_APP_ID", "9999999")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "dummy-pem")
        monkeypatch.setattr(github_app, "generate_app_jwt", lambda: "fake-jwt")

        with patch("clients.github_app.aiohttp.ClientSession", side_effect=RuntimeError("network down")):
            result = await github_app.verify_installation_belongs_to_app(555)

        assert result is False

    async def test_missing_credentials_fails_closed_without_network_call(self, monkeypatch):
        monkeypatch.delenv("GITHUB_APP_ID", raising=False)
        monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
        session_ctor = MagicMock()
        with patch("clients.github_app.aiohttp.ClientSession", session_ctor):
            result = await github_app.verify_installation_belongs_to_app(555)

        assert result is False
        session_ctor.assert_not_called()
