"""Tests for centralized authentication (CENTRAL_AUTH_GUIDE.md).

The DB-backed credential check is tested against a stubbed users collection,
so the suite runs without a live MongoDB. JWT and HTTP-gate behaviour are
tested in-process. Skipped entirely when the optional auth deps are missing.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

pytest.importorskip("pymongo")
pytest.importorskip("bcrypt")
pytest.importorskip("jwt")

from nestick import auth  # noqa: E402
from nestick.web import server as web  # noqa: E402


class StubCollection:
    def __init__(self, user):
        self._user = user
        self.calls = 0

    def find_one(self, query):
        self.calls += 1
        return dict(self._user)


def _bcrypt_hash(password: str) -> str:
    import bcrypt

    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


@pytest.fixture
def enabled(monkeypatch):
    """Force auth on with a fake URI/secret and no live client."""
    monkeypatch.setattr(auth, "AUTH_MONGODB_URI", "mongodb://auth.example/central")
    monkeypatch.setattr(auth, "JWT_SECRET", "shared-test-secret-0123456789abcdef0123456789abcdef")
    auth._reset_client()
    yield
    auth._reset_client()


class TestConfig:
    def test_disabled_without_env(self, monkeypatch):
        monkeypatch.delenv("AUTH_MONGODB_URI", raising=False)
        monkeypatch.delenv("MONGODB_URI", raising=False)
        monkeypatch.delenv("JWT_SECRET", raising=False)
        monkeypatch.setattr(auth, "AUTH_MONGODB_URI", None)
        monkeypatch.setattr(auth, "JWT_SECRET", None)
        assert auth.enabled() is False

    def test_requires_both(self, monkeypatch):
        monkeypatch.setattr(auth, "AUTH_MONGODB_URI", "mongodb://x/db")
        monkeypatch.setattr(auth, "JWT_SECRET", None)
        assert auth.enabled() is False

    def test_bearer_parsing(self):
        assert auth.bearer_token("Bearer abc.def") == "abc.def"
        assert auth.bearer_token("bearer  xyz") == "xyz"
        assert auth.bearer_token("Basic abc") is None
        assert auth.bearer_token(None) is None


class TestJwt:
    def test_roundtrip(self, enabled):
        tok = auth.issue_token({"_id": "u1", "email": "a@b.co", "role": "admin"})
        claims = auth.verify_token(tok)
        assert claims["sub"] == "u1"
        assert claims["email"] == "a@b.co"
        assert claims["role"] == "admin"
        assert claims["exp"] > claims["iat"]

    def test_invalid_signature(self, enabled):
        import jwt

        tok = jwt.encode({"sub": "u1"}, "other-secret-that-is-long-enough-0000000000",
                         algorithm="HS256")
        assert auth.verify_token(tok) is None

    def test_expired(self, enabled):
        import jwt
        import time

        tok = jwt.encode({"sub": "u1", "exp": int(time.time()) - 60},
                         auth.JWT_SECRET, algorithm="HS256")
        assert auth.verify_token(tok) is None


class TestCredentials:
    def test_valid_user(self, enabled, monkeypatch):
        user = {"_id": "u1", "email": "A@B.co", "password": _bcrypt_hash("pw")}
        monkeypatch.setattr(auth, "_users_collection", lambda: StubCollection(user))
        out = auth.verify_user("a@b.co", "pw")
        assert out is not None and out["email"] == "A@B.co"

    def test_wrong_password(self, enabled, monkeypatch):
        user = {"email": "a@b.co", "password": _bcrypt_hash("right")}
        monkeypatch.setattr(auth, "_users_collection", lambda: StubCollection(user))
        assert auth.verify_user("a@b.co", "wrong") is None

    def test_unknown_user(self, enabled, monkeypatch):
        monkeypatch.setattr(auth, "_users_collection", lambda: StubCollection({}))
        assert auth.verify_user("nobody@b.co", "pw") is None

    def test_db_down_denies(self, enabled, monkeypatch):
        def boom(_query):
            raise RuntimeError("no server")

        coll = type("C", (), {"find_one": staticmethod(boom)})()
        monkeypatch.setattr(auth, "_users_collection", lambda: coll)
        assert auth.verify_user("a@b.co", "pw") is None


# --------------------------------------------------------------------------- #
# HTTP gate: /api/login is public; everything else needs a bearer token
# --------------------------------------------------------------------------- #
@pytest.fixture
def http(enabled, monkeypatch):
    user = {"_id": "u1", "email": "a@b.co", "password": _bcrypt_hash("pw")}
    monkeypatch.setattr(auth, "_users_collection", lambda: StubCollection(user))
    jobs = web.JobManager()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(jobs))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base
    httpd.shutdown()
    thread.join(timeout=3)


def _json(base, path, method="GET", token=None, body=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read().decode())


def test_login_issues_token(http):
    status, data = _json(http, "/api/login", method="POST",
                         body={"email": "a@b.co", "password": "pw"})
    assert status == 200 and data["token"]
    assert auth.verify_token(data["token"])


def test_login_rejects_bad_password(http):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _json(http, "/api/login", method="POST",
              body={"email": "a@b.co", "password": "nope"})
    assert exc.value.code == 401


def test_protected_route_needs_token(http):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _json(http, "/api/status")
    assert exc.value.code == 401


def test_protected_route_with_token(http):
    _, login = _json(http, "/api/login", method="POST",
                     body={"email": "a@b.co", "password": "pw"})
    status, data = _json(http, "/api/status", token=login["token"])
    assert status == 200
    assert "running" in data


def test_login_when_auth_disabled_is_501(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_MONGODB_URI", None)
    monkeypatch.setattr(auth, "JWT_SECRET", None)
    jobs = web.JobManager()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(jobs))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _json(base, "/api/login", method="POST",
                  body={"email": "a@b.co", "password": "pw"})
        assert exc.value.code == 501
    finally:
        httpd.shutdown()
