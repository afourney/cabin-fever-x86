"""Browser identity, lifecycle and transport boundaries, without live providers."""

import asyncio
import json
import stat
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from starlette.websockets import WebSocketDisconnect

from cabin_fever_x86_core.config import Config, ConfigError, load_config
from cabin_fever_x86_core.messages import SessionInfo
from cabin_fever_x86_core.web_gateway import _main as web
from cabin_fever_x86_core.web_gateway.auth import COOKIE, BrowserAuth, Principal

ORIGIN = {"origin": "http://localhost"}


@pytest.fixture(scope="module")
def password_hash():
    return PasswordHasher().hash("correct horse battery staple")


@pytest.fixture
def config(tmp_path, monkeypatch, password_hash):
    monkeypatch.chdir(tmp_path)
    return Config.model_validate(
        {
            "users": [
                {"user_id": "guest", "identities": [{"type": "guest"}]},
                {
                    "user_id": "operator",
                    "identities": [
                        {
                            "type": "login",
                            "username": "Night Owl",
                            "password_hash": password_hash,
                        }
                    ],
                },
            ],
            "web_gateway": {"session_idle_seconds": 60, "session_max_seconds": 120},
        }
    )


@pytest.fixture
def client(config):
    with TestClient(
        web.create_app("ws://game", None, config), base_url="http://localhost"
    ) as value:
        yield value


def login(client, **overrides):
    body = {"username": "night owl", "password": "correct horse battery staple"}
    body.update(overrides)
    return client.post("/auth/login", json=body, headers=ORIGIN)


def test_callsign_login_cookie_and_no_hash_credential(client, config, password_hash):
    state = client.get("/auth").json()
    assert state == {
        "authenticated": False,
        "implicit_guest": False,
        "guest_available": True,
        "login_available": True,
        "refresh_seconds": 20,
    }
    assert login(client, username="operator").status_code == 401
    assert login(client, password=password_hash).status_code == 401
    assert client.get("/sessions").status_code == 401
    response = login(client, username="  NIGHT OWL  ")
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert "Secure" not in response.headers["set-cookie"]
    assert password_hash not in response.text
    assert client.get("/auth").json()["authenticated"]
    auth = client.app.state.browser_auth
    with auth.db() as connection:
        row = connection.execute("SELECT * FROM sessions").fetchone()
    assert row["user_id"] == "operator"
    assert row["identity"] == "login:night owl"
    assert password_hash not in json.dumps(dict(row))
    assert password_hash not in repr(config)
    assert stat.S_IMODE((config.web_gateway.data_dir / "signing.key").stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("users", "implicit", "guest"),
    [
        ([], False, False),
        ([{"user_id": "guest", "identities": []}], False, False),
        (
            [{"user_id": "guest", "identities": [{"type": "telegram", "account_id": 123}]}],
            False,
            False,
        ),
        ([{"user_id": "guest", "identities": [{"type": "guest"}]}], True, True),
        (
            [
                {"user_id": "guest", "identities": [{"type": "guest"}]},
                {"user_id": "radio", "identities": [{"type": "telegram", "account_id": 123}]},
            ],
            False,
            True,
        ),
    ],
)
def test_guest_is_explicit_and_conditional(config, users, implicit, guest):
    config.users = Config.model_validate({"users": users}).users
    with TestClient(
        web.create_app("ws://game", None, config), base_url="http://localhost"
    ) as client:
        state = client.get("/auth").json()
        assert state["implicit_guest"] is implicit
        assert state["guest_available"] is guest
        assert state["authenticated"] is implicit
        assert client.post("/auth/guest", headers=ORIGIN).status_code == (200 if guest else 403)
        assert client.get("/auth").json()["authenticated"] is guest


@pytest.mark.parametrize("path", ["/sessions", f"/takes/{uuid4()}", f"/audio/{uuid4()}/clip.wav"])
def test_http_resources_require_auth_and_ignore_spoofed_user(client, path):
    method = client.post if path.startswith("/takes") else client.get
    response = method(path, headers={**ORIGIN, "X-CF86-User-ID": "operator"})
    assert response.status_code == 401


def test_cookie_tampering_logout_and_rotation(client):
    assert login(client).status_code == 200
    original = client.cookies.get(COOKIE)
    client.cookies.clear()
    client.cookies.set(COOKIE, original + "tampered", domain="localhost.local")
    assert client.get("/sessions").status_code == 401
    client.cookies.clear()
    client.cookies.set(COOKIE, original, domain="localhost.local")
    assert client.post("/auth/refresh", headers=ORIGIN).status_code == 200
    assert login(client).status_code == 200
    replacement = client.cookies.get(COOKIE)
    client.cookies.clear()
    client.cookies.set(COOKIE, original, domain="localhost.local")
    assert client.post("/auth/refresh", headers=ORIGIN).status_code == 401
    client.cookies.clear()
    client.cookies.set(COOKIE, replacement, domain="localhost.local")
    assert client.post("/auth/logout", headers=ORIGIN).status_code == 200
    client.cookies.set(COOKIE, replacement, domain="localhost.local")
    assert client.post("/auth/refresh", headers=ORIGIN).status_code == 401


def test_expiry_slides_only_via_http_and_has_absolute_limit(client, monkeypatch):
    auth = client.app.state.browser_auth
    assert login(client).status_code == 200
    with auth.db() as connection:
        row = connection.execute("SELECT * FROM sessions").fetchone()
    principal = Principal(row["user_id"], row["identity"], row["sid"])
    start = row["deadline"] - 120
    monkeypatch.setattr("cabin_fever_x86_core.web_gateway.auth.time.time", lambda: start + 40)
    assert auth.valid(principal)
    assert client.get("/auth").json()["authenticated"]
    with auth.db() as connection:
        assert connection.execute("SELECT expires FROM sessions").fetchone()[0] == start + 60
    assert client.post("/auth/refresh", headers=ORIGIN).status_code == 200
    monkeypatch.setattr("cabin_fever_x86_core.web_gateway.auth.time.time", lambda: start + 90)
    assert client.post("/auth/refresh", headers=ORIGIN).status_code == 200
    with auth.db() as connection:
        assert connection.execute("SELECT expires FROM sessions").fetchone()[0] == start + 120
    monkeypatch.setattr("cabin_fever_x86_core.web_gateway.auth.time.time", lambda: start + 121)
    assert not auth.valid(principal)
    assert client.post("/auth/refresh", headers=ORIGIN).status_code == 401


def test_idle_expiry_and_restart_preserve_validation(client, config):
    assert login(client).status_code == 200
    token = client.cookies.get(COOKIE)
    restarted = BrowserAuth(config)
    auth = client.app.state.browser_auth
    assert restarted.signer.loads(token) == auth.signer.loads(token)
    with restarted.db() as connection:
        row = connection.execute("SELECT * FROM sessions").fetchone()
        connection.execute("UPDATE sessions SET expires = 0")
    assert not auth.valid(Principal(row["user_id"], row["identity"], row["sid"]))
    assert client.post("/auth/refresh", headers=ORIGIN).status_code == 401


@pytest.mark.parametrize(
    "change", ["remove-user", "remove-identity", "change-password", "remove-guest"]
)
def test_session_revocation_rechecks_configured_identity(client, config, change):
    if change == "remove-guest":
        assert client.post("/auth/guest", headers=ORIGIN).status_code == 200
        config.users[0].identities.clear()
    else:
        assert login(client).status_code == 200
        if change == "remove-user":
            config.users.pop()
        elif change == "remove-identity":
            config.users[1].identities.clear()
        else:
            config.users[1].identities[0].password_hash = SecretStr(
                PasswordHasher().hash("new password")
            )
    assert client.post("/auth/refresh", headers=ORIGIN).status_code == 401


@pytest.mark.parametrize("origin", [None, "null", "http://evil.example", "https://localhost"])
@pytest.mark.parametrize(
    "path", ["/auth/login", "/auth/guest", "/auth/logout", "/auth/refresh", "/takes/123"]
)
def test_mutations_require_exact_origin(client, origin, path):
    headers = {} if origin is None else {"origin": origin}
    assert client.post(path, headers=headers).status_code == 403


def test_remote_http_is_denied_and_public_https_sets_secure_cookie(config):
    app = web.create_app("ws://game", None, config)
    with TestClient(app, base_url="http://evil.example") as client:
        assert client.get("/auth").status_code in (400, 403)
    config.web_gateway.public_origin = "https://radio.example"
    with TestClient(
        web.create_app("ws://game", None, config), base_url="https://radio.example"
    ) as client:
        response = client.post("/auth/guest", headers={"origin": "https://radio.example"})
        assert response.status_code == 200
        assert "Secure" in response.headers["set-cookie"]
        assert client.get("/auth").json()["authenticated"]
        assert (
            client.post("/auth/refresh", headers={"origin": "https://other.example"}).status_code
            == 403
        )


@pytest.mark.parametrize(
    "settings",
    [
        {"public_origin": "http://radio.example"},
        {"public_origin": "https://*.example"},
        {"public_origin": "https://radio.example/subpath"},
        {"public_origin": "https://username:password@radio.example"},
        {"signing_secret": "too-short-secret"},
        {"session_idle_seconds": 30},
        {"session_idle_seconds": 3600, "session_max_seconds": 60},
    ],
)
def test_unsafe_browser_settings_are_rejected(settings):
    with pytest.raises(ValidationError):
        Config.model_validate({"web_gateway": settings})


def test_password_login_without_guest_access(client, config):
    config.users.pop(0)
    assert client.get("/auth").json()["guest_available"] is False
    assert client.post("/auth/guest", headers=ORIGIN).status_code == 403
    assert login(client).status_code == 200
    assert client.get("/auth").json()["authenticated"]


def test_login_rate_limit_and_validation_do_not_echo_password(client, monkeypatch):
    auth = client.app.state.browser_auth
    calls = []

    def verify(_hash, _password):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        calls.append(True)
        return False

    monkeypatch.setattr(auth, "verify", verify)
    for _ in range(20):
        assert login(client).status_code == 401
    assert login(client).status_code == 429
    assert len(calls) == 20
    auth.attempts.clear()
    response = login(client, password={"secret": "never echo me"})
    assert response.status_code == 400
    assert "never echo me" not in response.text
    assert client.post("/auth/login", content=b"x" * 8193, headers=ORIGIN).status_code == 400
    auth.verifying = 2
    assert login(client).status_code == 429


def test_login_hash_validation_and_uniqueness(config, password_hash, tmp_path):
    raw = config.model_dump(mode="json")
    raw["users"][1]["identities"][0]["password_hash"] = password_hash
    raw["users"].append(
        {
            "user_id": "other",
            "identities": [
                {
                    "type": "login",
                    "username": "NIGHT OWL",
                    "password_hash": password_hash,
                }
            ],
        }
    )
    with pytest.raises(ValidationError, match="duplicate login identity") as error:
        Config.model_validate(raw)
    assert password_hash not in str(error.value)
    raw["users"].pop()
    raw["users"][1]["identities"][0]["password_hash"] = "secret-not-a-hash"
    with pytest.raises(ValidationError) as error:
        Config.model_validate(raw)
    assert "secret-not-a-hash" not in str(error.value)
    path = tmp_path / "invalid.yaml"
    path.write_text('users: ["password: secret-not-a-hash')
    with pytest.raises(ConfigError) as error:
        load_config(path)
    assert "secret-not-a-hash" not in str(error.value)


def test_audio_is_scoped_and_traversal_is_rejected(client):
    session_id = uuid4()
    from cabin_fever_x86_core.sessions import session_dir

    base = session_dir(session_id, "web_gateway", user_id="operator")
    (base / "audio").mkdir()
    (base / "audio" / "clip.wav").write_bytes(b"operator audio")
    (base / "private.txt").write_text("private")
    (base / "audio" / "linked.wav").symlink_to((base / "private.txt").resolve())
    assert client.post("/auth/guest", headers=ORIGIN).status_code == 200
    assert client.get(f"/audio/{session_id}/clip.wav").status_code == 404
    assert login(client).status_code == 200
    assert client.get(f"/audio/{session_id}/clip.wav").content == b"operator audio"
    assert client.get(f"/audio/{session_id}/linked.wav").status_code == 404
    assert client.get(f"/audio/{session_id}/..%2Fprivate.txt").status_code == 404


@contextmanager
def fake_upstream(monkeypatch):
    headers = []
    resumed = []
    session_id = uuid4()

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def __await__(self):
            async def ready():
                return self

            return ready().__await__()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

        async def close(self):
            pass

    def connect(_uri, *, additional_headers):
        headers.append(additional_headers)
        return Connection()

    async def open_session(_connection, resume):
        resumed.append(resume)
        return resume or session_id

    async def list_sessions(_connection):
        return [SessionInfo(session_id=session_id, modified=datetime.now(UTC))]

    monkeypatch.setattr(web, "connect", connect)
    monkeypatch.setattr(web, "open_session", open_session)
    monkeypatch.setattr(web, "list_sessions", list_sessions)
    yield headers, resumed, session_id


def test_upstream_identity_on_list_new_resume_and_upload_ownership(client, monkeypatch):
    with fake_upstream(monkeypatch) as (headers, resumed, session_id):
        assert login(client).status_code == 200
        assert client.get("/sessions", headers={"X-CF86-User-ID": "guest"}).status_code == 200
        with client.websocket_connect("ws://localhost/ws", headers=ORIGIN) as browser:
            assert browser.receive_json()["session_id"] == str(session_id)
            assert (
                client.post(f"/takes/{session_id}", content=b"", headers=ORIGIN).status_code == 400
            )
            assert client.post("/auth/guest", headers=ORIGIN).status_code == 200
            assert (
                client.post(f"/takes/{session_id}", content=b"clip", headers=ORIGIN).status_code
                == 404
            )
            browser.close()
            assert browser.receive()["type"] == "websocket.close"
        assert login(client).status_code == 200
        with client.websocket_connect(
            f"ws://localhost/ws?resume={session_id}", headers=ORIGIN
        ) as browser:
            assert browser.receive_json()["session_id"] == str(session_id)
            browser.close()
            assert browser.receive()["type"] == "websocket.close"
        assert resumed == [None, session_id]
        assert headers == [{"X-CF86-User-ID": "operator"}] * 3


@pytest.mark.parametrize("origin", [None, "http://evil.example", "http://localhost"])
def test_websocket_rejects_unauthenticated_and_cross_origin(client, monkeypatch, origin):
    with fake_upstream(monkeypatch) as (headers, _, _):
        if origin != "http://localhost":
            assert login(client).status_code == 200
        handshake = {"X-CF86-User-ID": "operator"}
        if origin:
            handshake["origin"] = origin
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("ws://localhost/ws", headers=handshake),
        ):
            pass
        assert not headers


def test_live_websocket_is_closed_after_logout(client, monkeypatch):
    with fake_upstream(monkeypatch):
        assert login(client).status_code == 200
        with client.websocket_connect("ws://localhost/ws", headers=ORIGIN) as browser:
            browser.receive_json()
            assert client.post("/auth/logout", headers=ORIGIN).status_code == 200
            with pytest.raises(WebSocketDisconnect) as error:
                browser.receive_json()
            assert error.value.code == 4401
