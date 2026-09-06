"""Browser-only authentication, with signed cookies and revocable, bounded sessions."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import secrets
import sqlite3
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import HTTPConnection

from cabin_fever_x86_core.config import Config, GuestIdentity, LoginIdentity

COOKIE = "cf86_session"


@dataclass(frozen=True)
class Principal:
    """A resolved browser identity, never taken from a request header."""

    user_id: str
    identity: str
    sid: str | None = None


class BrowserAuth:
    """Own the gateway's persistent secret and shared SQLite session registry."""

    def __init__(self, config: Config):
        """Initialize persistent authentication state under gateway data, not a game."""
        self.config = config
        self.settings = config.web_gateway
        directory = self.settings.data_dir
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.settings.signing_secret is not None:
            secret = self.settings.signing_secret.get_secret_value()
        else:
            # Lock initialization: concurrent workers must never read a partial key.
            descriptor = os.open(directory / "signing.key", os.O_RDWR | os.O_CREAT, 0o600)
            with os.fdopen(descriptor, "r+") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                secret = handle.read()
                if not secret:
                    secret = secrets.token_urlsafe(48)
                    handle.write(secret)
                    handle.flush()
                    os.fsync(handle.fileno())
                if len(secret) < 32:
                    raise ValueError("web gateway signing key is invalid")
        self.signer = URLSafeTimedSerializer(secret, salt="cf86-browser-session-v1")
        self.database = directory / "sessions.sqlite3"
        descriptor = os.open(self.database, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(descriptor)
        with self.db() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS sessions "
                "(sid TEXT PRIMARY KEY, user_id TEXT NOT NULL, identity TEXT NOT NULL, "
                "fingerprint TEXT NOT NULL, expires REAL NOT NULL, deadline REAL NOT NULL)"
            )
        self.hasher = PasswordHasher()
        self.dummy_hash = self.hasher.hash(secrets.token_urlsafe(32))
        self.attempts: deque[float] = deque()
        self.verifying = 0

    @contextmanager
    def db(self):
        """Open a short-lived transaction shared across gateway workers."""
        connection = sqlite3.connect(self.database, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @property
    def guest_available(self) -> bool:
        """Only the reserved user with an actual guest identity is anonymous."""
        return any(
            user.user_id == "guest"
            and any(isinstance(identity, GuestIdentity) for identity in user.identities)
            for user in self.config.users
        )

    @property
    def implicit_guest(self) -> bool:
        """Keep the original radio prompt only for a sole, configured guest."""
        return len(self.config.users) == 1 and self.guest_available

    def identities(self) -> dict[str, tuple[str, LoginIdentity]]:
        """Map normalized callsigns, not internal user IDs, to login identities."""
        return {
            identity.username.casefold(): (user.user_id, identity)
            for user in self.config.users
            for identity in user.identities
            if isinstance(identity, LoginIdentity)
        }

    def fingerprint(self, principal: Principal) -> str | None:
        """Recheck authorization, including removal or password replacement."""
        if principal.identity == "guest":
            return "guest" if principal.user_id == "guest" and self.guest_available else None
        entry = self.identities().get(principal.identity.removeprefix("login:"))
        if not entry or entry[0] != principal.user_id:
            return None
        return hashlib.sha256(entry[1].password_hash.get_secret_value().encode()).hexdigest()

    def check_origin(self, request: HTTPConnection, *, required: bool = True) -> None:
        """Require the exact deployment origin for mutations and WebSockets."""
        scheme = {"ws": "http", "wss": "https"}.get(request.url.scheme, request.url.scheme)
        actual = f"{scheme}://{request.headers.get('host', '')}"
        expected = self.settings.public_origin or actual
        if not self.settings.public_origin and request.url.hostname not in (
            "localhost",
            "127.0.0.1",
            "::1",
        ):
            raise HTTPException(403, "Configure an HTTPS public_origin for remote access")
        origin = request.headers.get("origin")
        if actual != expected or (origin != expected and (required or origin is not None)):
            raise HTTPException(403, "Request origin is not allowed")

    def valid(self, principal: Principal) -> bool:
        """Validate a live socket against current identity and server-side expiry."""
        if principal.sid is None:
            return self.implicit_guest and principal.user_id == "guest"
        now = time.time()
        with self.db() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE sid = ?", (principal.sid,)
            ).fetchone()
        return bool(
            row
            and row["user_id"] == principal.user_id
            and row["identity"] == principal.identity
            and now < row["expires"]
            and now < row["deadline"]
            and row["fingerprint"] == self.fingerprint(principal)
        )

    def require(self, request: HTTPConnection) -> Principal:
        """Resolve a signed session, or the narrowly defined implicit guest."""
        token = request.cookies.get(COOKIE)
        if not token and self.implicit_guest:
            return Principal("guest", "guest")
        try:
            sid = self.signer.loads(token or "", max_age=self.settings.session_max_seconds)
            if not isinstance(sid, str):
                raise ValueError
            with self.db() as connection:
                row = connection.execute("SELECT * FROM sessions WHERE sid = ?", (sid,)).fetchone()
            if row:
                principal = Principal(row["user_id"], row["identity"], sid)
                if self.valid(principal):
                    return principal
        except (BadSignature, ValueError):
            pass
        raise HTTPException(401, "Sign in to use the radio")

    def revoke(self, request: HTTPConnection) -> None:
        """Revoke this cookie's session without accepting any client-selected user."""
        try:
            sid = self.signer.loads(
                request.cookies.get(COOKIE, ""), max_age=self.settings.session_max_seconds
            )
            if isinstance(sid, str):
                with self.db() as connection:
                    connection.execute("DELETE FROM sessions WHERE sid = ?", (sid,))
        except BadSignature:
            pass

    def cookie(self, response: JSONResponse, sid: str, lifetime: int) -> None:
        """Set a host-only, script-inaccessible cookie with deployment-safe flags."""
        response.set_cookie(
            COOKIE,
            self.signer.dumps(sid),
            max_age=lifetime,
            secure=bool(self.settings.public_origin),
            httponly=True,
            samesite="strict",
            path="/",
        )

    def issue(self, request: Request, principal: Principal) -> JSONResponse:
        """Rotate any previous session and persist a new bounded login."""
        self.revoke(request)
        sid = secrets.token_urlsafe(32)
        now = time.time()
        with self.db() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE expires <= ? OR deadline <= ?", (now, now)
            )
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    principal.user_id,
                    principal.identity,
                    self.fingerprint(principal),
                    now + self.settings.session_idle_seconds,
                    now + self.settings.session_max_seconds,
                ),
            )
        response = JSONResponse({"authenticated": True})
        self.cookie(response, sid, self.settings.session_idle_seconds)
        return response

    def admit_password_check(self) -> None:
        """Bound both Argon2 concurrency and attempt rate (per gateway worker)."""
        now = time.monotonic()
        while self.attempts and self.attempts[0] <= now - 60:
            self.attempts.popleft()
        if len(self.attempts) >= 20 or self.verifying >= 2:
            raise HTTPException(429, "Too many sign-in attempts. Wait a minute and try again.")
        self.attempts.append(now)

    def verify(self, password_hash: str, password: str) -> bool:
        """Verify a password off the event loop; never expose Argon2 diagnostics."""
        try:
            return self.hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False

    def install(self, app: FastAPI) -> None:
        """Install origin/host enforcement and the browser authentication API."""
        host = (
            urlsplit(self.settings.public_origin).hostname if self.settings.public_origin else None
        )
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=[host] if host else ["localhost", "127.0.0.1", "[::1]"],
            www_redirect=False,
        )

        @app.middleware("http")
        async def protect(request: Request, call_next):
            try:
                self.check_origin(request, required=request.method not in ("GET", "HEAD"))
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "same-origin"
            response.headers["X-Frame-Options"] = "DENY"
            return response

        @app.get("/auth")
        async def status(request: Request) -> dict:
            try:
                principal = self.require(request)
            except HTTPException:
                principal = None
            return {
                "authenticated": principal is not None,
                "implicit_guest": self.implicit_guest,
                "guest_available": self.guest_available,
                "login_available": bool(self.identities()),
                "refresh_seconds": min(300, self.settings.session_idle_seconds // 3),
            }

        @app.post("/auth/login")
        async def login(request: Request) -> JSONResponse:
            self.admit_password_check()
            # Read incrementally and avoid FastAPI validation echoing credentials.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 8192:
                    raise HTTPException(400, "Invalid sign-in request")
            try:
                data = json.loads(body)
                username, password = data["username"], data["password"]
                if (
                    not isinstance(username, str)
                    or not isinstance(password, str)
                    or not 1 <= len(username.strip()) <= 64
                    or not 1 <= len(password) <= 1024
                ):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise HTTPException(400, "Invalid sign-in request") from None
            entry = self.identities().get(username.strip().casefold())
            password_hash = entry[1].password_hash.get_secret_value() if entry else self.dummy_hash
            # Recheck concurrency after reading the body, before starting costly work.
            if self.verifying >= 2:
                raise HTTPException(429, "Too many sign-in attempts. Wait a minute and try again.")
            self.verifying += 1
            try:
                verified = await asyncio.to_thread(self.verify, password_hash, password)
            finally:
                self.verifying -= 1
            if not verified or entry is None:
                raise HTTPException(401, "Callsign or password not recognized")
            return self.issue(request, Principal(entry[0], f"login:{entry[1].username.casefold()}"))

        @app.post("/auth/guest")
        async def guest(request: Request) -> JSONResponse:
            if not self.guest_available:
                raise HTTPException(403, "Guest access is not configured")
            return self.issue(request, Principal("guest", "guest"))

        @app.post("/auth/logout")
        async def logout(request: Request) -> JSONResponse:
            self.revoke(request)
            response = JSONResponse({"authenticated": False})
            response.delete_cookie(
                COOKIE, secure=bool(self.settings.public_origin), httponly=True, samesite="strict"
            )
            return response

        @app.post("/auth/refresh")
        async def refresh(request: Request) -> JSONResponse:
            principal = self.require(request)
            if principal.sid is None:
                return JSONResponse({"authenticated": True})
            now = time.time()
            with self.db() as connection:
                # Conditional UPDATE prevents refresh from resurrecting an expired/revoked session.
                row = connection.execute(
                    "UPDATE sessions SET expires = min(?, deadline) "
                    "WHERE sid = ? AND expires > ? AND deadline > ? RETURNING expires",
                    (now + self.settings.session_idle_seconds, principal.sid, now, now),
                ).fetchone()
            if row is None:
                raise HTTPException(401, "Sign in to use the radio")
            response = JSONResponse({"authenticated": True})
            self.cookie(response, principal.sid, max(1, int(row["expires"] - now)))
            return response
