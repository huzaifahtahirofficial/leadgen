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


class StubUsers:
    """An in-memory users collection for register + admin tests.

    Understands the query shapes the code uses: exact ``_id``, ``$or``
    email/username/name ``$regex`` clauses (compiled case-insensitively), and
    an empty query (everything).
    """

    def __init__(self, users=None):
        self._users: list[dict] = list(users or [])
        self.calls = 0

    @staticmethod
    def _clause_hits(user, clause):
        import re

        for field, cond in clause.items():
            if not isinstance(cond, dict) or "$regex" not in cond:
                continue
            opts = cond.get("$options") or ""
            flags = re.IGNORECASE if "i" in opts else 0
            try:
                rx = re.compile(cond["$regex"], flags)
            except re.error:
                continue
            value = str(user.get(field) or "")
            if field in ("username", "email") and not value:
                value = str(user.get("email") or user.get("username") or "")
            if rx.search(value):
                return True
        return False

    def _match(self, query):
        if not query:
            return list(self._users)
        if "_id" in query:
            return [u for u in self._users if str(u.get("_id")) == str(query["_id"])]
        if query.get("$or"):
            return [u for u in self._users
                    if any(self._clause_hits(u, c) for c in query["$or"])]
        return list(self._users)

    def find_one(self, query):
        self.calls += 1
        target = self._match(query)
        return dict(target[0]) if target else None

    def insert_one(self, doc):
        self.calls += 1
        self._users.append(doc)
        return type("R", (), {"inserted_id": "u-reg"})()

    def find(self, query):
        self.calls += 1
        rows = [dict(u) for u in self._match(query)]

        class Cursor:
            def __init__(self, rows):
                self._rows = list(rows)

            def sort(self, key, direction=1):
                return self

            def limit(self, n):
                return self._rows[:max(1, n)]

        return Cursor(rows)

    def update_one(self, query, update):
        self.calls += 1
        import copy

        for u in self._users:
            if str(u.get("_id")) == str(query.get("_id")):
                for k, v in update.get("$set", {}).items():
                    u[k] = copy.deepcopy(v)
                return type("R", (), {"matched_count": 1, "modified_count": 1})()
        return type("R", (), {"matched_count": 0, "modified_count": 0})()


def _bcrypt_hash(password: str) -> str:
    import bcrypt

    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _serve():
    """Start a fresh in-process server; returns (httpd, base_url)."""
    jobs = web.JobManager()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(jobs))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


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
        assert claims["userId"] == "u1"
        assert claims["sub"] == "u1"
        assert claims["email"] == "a@b.co"
        assert claims["role"] == "admin"
        assert claims["exp"] > claims["iat"]

    def test_collection_name_matches_central_platform(self):
        assert auth.AUTH_MONGODB_COLLECTION == "User Accounts"

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
        assert auth.last_error == "Incorrect password."

    def test_unknown_user(self, enabled, monkeypatch):
        monkeypatch.setattr(auth, "_users_collection", lambda: StubCollection({}))
        assert auth.verify_user("nobody@b.co", "pw") is None
        assert auth.last_error == "No account found for that email address."

    def test_email_match_is_case_insensitive(self, enabled, monkeypatch):
        user = {"email": "User@Example.COM", "password": _bcrypt_hash("pw")}
        monkeypatch.setattr(auth, "_users_collection", lambda: StubCollection(user))
        assert auth.verify_user("user@example.com", "pw") is not None

    def test_db_down_denies(self, enabled, monkeypatch):
        def boom(_query):
            raise RuntimeError("no server")

        coll = type("C", (), {"find_one": staticmethod(boom)})()
        monkeypatch.setattr(auth, "_users_collection", lambda: coll)
        assert auth.verify_user("a@b.co", "pw") is None
        assert auth.last_error.startswith("Auth database unreachable")

    def test_non_bcrypt_hash_flagged(self, enabled, monkeypatch):
        user = {"email": "a@b.co", "password": "sha256$deadbeef"}
        monkeypatch.setattr(auth, "_users_collection", lambda: StubCollection(user))
        assert auth.verify_user("a@b.co", "pw") is None
        assert "bcrypt" in auth.last_error

    def test_db_status_disabled(self, monkeypatch):
        monkeypatch.setattr(auth, "AUTH_MONGODB_URI", None)
        monkeypatch.setattr(auth, "JWT_SECRET", None)
        info = auth.db_status()
        assert info["enabled"] is False and info["reachable"] is False


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


def test_login_reports_db_down_as_503(enabled, monkeypatch):
    def boom(_query):
        raise RuntimeError("connection refused")

    coll = type("C", (), {"find_one": staticmethod(boom)})()
    monkeypatch.setattr(auth, "_users_collection", lambda: coll)
    jobs = web.JobManager()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(jobs))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _json(base, "/api/login", method="POST",
                  body={"email": "a@b.co", "password": "pw"})
        assert exc.value.code == 503
        assert b"Auth database unreachable" in exc.value.read()
    finally:
        httpd.shutdown()


def test_auth_status_is_public(enabled, monkeypatch):
    monkeypatch.setattr(auth, "db_status", lambda: {
        "enabled": True, "reachable": False,
        "database": "central", "collection": "User Accounts", "users": 0,
    })
    jobs = web.JobManager()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(jobs))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, data = _json(base, "/api/auth-status")
        assert status == 200 and data["enabled"] is True
        assert "reachable" in data and "database" in data
    finally:
        httpd.shutdown()


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


# --------------------------------------------------------------------------- #
# Account creation + subscriptions (DB-driven plans/roles)
# --------------------------------------------------------------------------- #
class TestRegister:
    def test_creates_user_with_bcrypt_and_inactive_subscription(self, enabled, monkeypatch):
        users = StubUsers()
        monkeypatch.setattr(auth, "_users_collection", lambda: users)
        out = auth.register_user("Pat", "pat@b.co", "secret1")
        assert out is not None
        assert out["email"] == "pat@b.co" and out["role"] == "user"
        assert out["plan"] == "free"
        assert out["subscription"]["active"] is False
        assert out["password"].startswith("$2")
        assert users._users[0]["createdAt"] is not None

    def test_duplicate_email_rejected_case_insensitive(self, enabled, monkeypatch):
        users = StubUsers()
        monkeypatch.setattr(auth, "_users_collection", lambda: users)
        assert auth.register_user("a", "x@b.co", "secret1") is not None
        assert auth.register_user("a", "X@B.CO", "secret1") is None
        assert "already exists" in auth.last_error

    def test_invalid_email_rejected(self, enabled):
        assert auth.register_user("a", "not-an-email", "secret1") is None
        assert "email" in auth.last_error.lower()

    def test_short_password_rejected(self, enabled):
        assert auth.register_user("a", "a@b.co", "abc") is None
        assert "6 characters" in auth.last_error

    def test_disabled_when_auth_off(self, monkeypatch):
        monkeypatch.setattr(auth, "AUTH_MONGODB_URI", None)
        monkeypatch.setattr(auth, "JWT_SECRET", None)
        assert auth.register_user("a", "a@b.co", "secret1") is None
        assert "not enabled" in auth.last_error


class TestSubscription:
    def test_free_user_cannot_scrape(self):
        assert auth.can_scrape({"role": "user", "plan": "free"}) is False

    def test_subscribed_user_can_scrape(self):
        user = {"role": "user", "plan": "pro",
                "subscription": {"active": True, "plan": "pro", "expiresAt": None}}
        assert auth.can_scrape(user) is True

    def test_admin_bypasses_subscription(self):
        assert auth.can_scrape({"role": "admin", "plan": "free"}) is True

    def test_expired_subscription_revoked(self):
        user = {"role": "user", "plan": "pro",
                "subscription": {"active": True, "expiresAt": "2020-01-01T00:00:00Z"}}
        assert auth.can_scrape(user) is False

    def test_epoch_expiry_past(self):
        user = {"role": "user", "plan": "pro",
                "subscription": {"active": True, "expiresAt": 1_600_000_000}}
        assert auth.can_scrape(user) is False

    def test_legacy_paid_plan_implies_subscribed(self):
        assert auth.can_scrape({"role": "user", "plan": "pro"}) is True

    def test_explicit_inactive_overrides_paid_plan(self):
        user = {"role": "user", "plan": "pro",
                "subscription": {"active": False, "plan": "pro"}}
        assert auth.can_scrape(user) is False

    def test_public_profile_never_exposes_password(self):
        user = {"_id": "u1", "email": "a@b.co", "name": "A", "role": "user",
                "plan": "pro", "subscription": {"active": True},
                "password": "the-hash"}
        p = auth.public_profile(user)
        assert p["can_scrape"] is True and p["plan"] == "pro"
        assert "password" not in p


# --------------------------------------------------------------------------- #
# HTTP: /api/register, /api/me and the /api/start subscription gate
# --------------------------------------------------------------------------- #
def test_register_returns_token_and_profile(enabled, monkeypatch):
    users = StubUsers()
    monkeypatch.setattr(auth, "_users_collection", lambda: users)
    httpd, base = _serve()
    try:
        status, data = _json(base, "/api/register", method="POST",
                             body={"name": "Pat", "email": "pat@b.co",
                                   "password": "secret1"})
        assert status == 200 and data["token"]
        assert auth.verify_token(data["token"])
        assert data["user"]["email"] == "pat@b.co"
        assert data["user"]["plan"] == "free"
        assert data["user"]["can_scrape"] is False
        assert len(users._users) == 1
        assert users._users[0]["password"].startswith("$2")
    finally:
        httpd.shutdown()


def test_register_duplicate_returns_409(enabled, monkeypatch):
    users = StubUsers()
    monkeypatch.setattr(auth, "_users_collection", lambda: users)
    httpd, base = _serve()
    try:
        _json(base, "/api/register", method="POST",
              body={"email": "dup@b.co", "password": "secret1"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            _json(base, "/api/register", method="POST",
                  body={"email": "DUP@b.co", "password": "secret1"})
        assert exc.value.code == 409
    finally:
        httpd.shutdown()


def test_register_invalid_password_400(enabled, monkeypatch):
    users = StubUsers()
    monkeypatch.setattr(auth, "_users_collection", lambda: users)
    httpd, base = _serve()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _json(base, "/api/register", method="POST",
                  body={"email": "a@b.co", "password": "abc"})
        assert exc.value.code == 400
    finally:
        httpd.shutdown()


def test_me_returns_profile(http):
    _, login = _json(http, "/api/login", method="POST",
                     body={"email": "a@b.co", "password": "pw"})
    status, data = _json(http, "/api/me", token=login["token"])
    assert status == 200
    assert data["enabled"] is True
    assert data["email"] == "a@b.co"
    assert data["plan"] == "free"
    assert data["can_scrape"] is False   # u1 has no subscription
    assert "password" not in data


def test_start_blocked_for_free_user(http):
    _, login = _json(http, "/api/login", method="POST",
                     body={"email": "a@b.co", "password": "pw"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        _json(http, "/api/start", method="POST", token=login["token"], body={})
    assert exc.value.code == 403
    assert b"subscription" in exc.value.read().lower()


def _subscribed_user(email, **extra):
    user = {"_id": f"u-{email}", "email": email, "password": _bcrypt_hash("pw"),
            "plan": "pro", "subscription": {"active": True, "plan": "pro",
                                            "expiresAt": None}}
    user.update(extra)
    return user


def test_start_allowed_for_subscribed_user(enabled, monkeypatch):
    monkeypatch.setattr(web.JobManager, "start",
                        lambda self, form: (True, "started"))
    monkeypatch.setattr(auth, "_users_collection",
                        lambda: StubCollection(_subscribed_user("pro@b.co")))
    httpd, base = _serve()
    try:
        _, login = _json(base, "/api/login", method="POST",
                         body={"email": "pro@b.co", "password": "pw"})
        status, data = _json(base, "/api/start", method="POST",
                             token=login["token"], body={})
        assert status == 200 and data["ok"] is True
    finally:
        httpd.shutdown()


def test_start_allowed_for_admin(enabled, monkeypatch):
    monkeypatch.setattr(web.JobManager, "start",
                        lambda self, form: (True, "started"))
    user = {"_id": "u-boss", "email": "boss@b.co", "password": _bcrypt_hash("pw"),
            "role": "admin", "plan": "free"}
    monkeypatch.setattr(auth, "_users_collection", lambda: StubCollection(user))
    httpd, base = _serve()
    try:
        _, login = _json(base, "/api/login", method="POST",
                         body={"email": "boss@b.co", "password": "pw"})
        status, data = _json(base, "/api/start", method="POST",
                             token=login["token"], body={})
        assert status == 200 and data["ok"] is True
    finally:
        httpd.shutdown()


def test_start_blocked_for_expired_subscription(enabled, monkeypatch):
    user = _subscribed_user("exp@b.co")
    user["subscription"] = {"active": True, "expiresAt": "2020-01-01T00:00:00Z"}
    monkeypatch.setattr(auth, "_users_collection", lambda: StubCollection(user))
    httpd, base = _serve()
    try:
        _, login = _json(base, "/api/login", method="POST",
                         body={"email": "exp@b.co", "password": "pw"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            _json(base, "/api/start", method="POST", token=login["token"], body={})
        assert exc.value.code == 403
    finally:
        httpd.shutdown()


def test_preflight_allows_authorization_header(enabled, monkeypatch):
    monkeypatch.setattr(auth, "_users_collection",
                        lambda: StubCollection({"_id": "u1"}))
    httpd, base = _serve()
    try:
        req = urllib.request.Request(base + "/api/status", method="OPTIONS")
        req.add_header("Origin", "https://app.skelersecurity.app")
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 204
            allowed = r.headers.get("Access-Control-Allow-Headers", "").lower()
            assert "authorization" in allowed
    finally:
        httpd.shutdown()


# --------------------------------------------------------------------------- #
# Admin panel: list accounts and grant/revoke subscriptions by email
# --------------------------------------------------------------------------- #
def _admin_doc(email="boss@b.co"):
    return {"_id": "u-boss", "email": email, "password": _bcrypt_hash("pw"),
            "role": "admin", "plan": "free", "subscription": {"active": False}}


class TestAdminHelpers:
    def test_set_subscription_grant_by_email(self, enabled, monkeypatch):
        user = {"_id": "u1", "email": "a@b.co", "role": "user", "plan": "free",
                "subscription": {"active": False, "plan": "free", "expiresAt": None}}
        users = StubUsers([user])
        monkeypatch.setattr(auth, "_users_collection", lambda: users)
        out = auth.set_subscription("a@b.co", True, "pro")
        assert out is not None
        assert auth.can_scrape(out) is True
        assert users._users[0]["plan"] == "pro"
        assert users._users[0]["subscription"]["active"] is True

    def test_set_subscription_revoke_is_case_insensitive(self, enabled, monkeypatch):
        user = {"_id": "u1", "email": "a@b.co", "role": "user", "plan": "pro",
                "subscription": {"active": True, "plan": "pro", "expiresAt": None}}
        users = StubUsers([user])
        monkeypatch.setattr(auth, "_users_collection", lambda: users)
        out = auth.set_subscription("A@B.CO", False)
        assert out is not None
        assert auth.can_scrape(out) is False
        assert users._users[0]["subscription"]["active"] is False

    def test_set_subscription_unknown_email(self, enabled, monkeypatch):
        monkeypatch.setattr(auth, "_users_collection", lambda: StubUsers([]))
        assert auth.set_subscription("nobody@b.co", True) is None
        assert "No account found" in auth.last_error

    def test_set_subscription_clears_stale_expiry_on_grant(self, enabled, monkeypatch):
        user = {"_id": "u1", "email": "a@b.co", "role": "user", "plan": "pro",
                "subscription": {"active": True, "plan": "pro",
                                 "expiresAt": "2020-01-01T00:00:00Z"}}
        users = StubUsers([user])
        monkeypatch.setattr(auth, "_users_collection", lambda: users)
        out = auth.set_subscription("a@b.co", True)
        assert auth.can_scrape(out) is True
        assert users._users[0]["subscription"]["expiresAt"] is None

    def test_list_users_excludes_passwords(self, enabled, monkeypatch):
        users = StubUsers([{"_id": "u1", "email": "a@b.co", "password": "hash",
                            "role": "user", "plan": "pro",
                            "subscription": {"active": True}}])
        monkeypatch.setattr(auth, "_users_collection", lambda: users)
        rows = auth.list_users()
        assert rows is not None and len(rows) == 1
        assert rows[0]["email"] == "a@b.co"
        assert rows[0]["subscription"]["active"] is True
        assert "password" not in rows[0]

    def test_list_users_db_down_returns_none(self, enabled, monkeypatch):
        def boom(query):
            raise RuntimeError("no server")

        coll = type("C", (), {"find": staticmethod(boom)})()
        monkeypatch.setattr(auth, "_users_collection", lambda: coll)
        assert auth.list_users() is None
        assert auth.last_error.startswith("Auth database unreachable")


class TestAdminApi:
    def test_users_list_requires_admin_role(self, http):
        _, login = _json(http, "/api/login", method="POST",
                         body={"email": "a@b.co", "password": "pw"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            _json(http, "/api/admin/users", token=login["token"])
        assert exc.value.code == 403

    def test_users_list_as_admin(self, enabled, monkeypatch):
        user = {"_id": "u1", "email": "a@b.co", "password": _bcrypt_hash("pw"),
                "role": "user", "plan": "free",
                "subscription": {"active": False, "plan": "free"}}
        monkeypatch.setattr(auth, "_users_collection",
                            lambda: StubUsers([_admin_doc(), user]))
        httpd, base = _serve()
        try:
            _, login = _json(base, "/api/login", method="POST",
                             body={"email": "boss@b.co", "password": "pw"})
            status, data = _json(base, "/api/admin/users", token=login["token"])
            assert status == 200
            assert len(data["items"]) == 2
            by_email = {r["email"]: r for r in data["items"]}
            assert "password" not in by_email["a@b.co"]
            assert by_email["a@b.co"]["subscription"]["active"] is False
        finally:
            httpd.shutdown()

    def test_subscription_grant_then_revoke(self, enabled, monkeypatch):
        user = {"_id": "u1", "email": "a@b.co", "password": _bcrypt_hash("pw"),
                "role": "user", "plan": "free",
                "subscription": {"active": False, "plan": "free", "expiresAt": None}}
        monkeypatch.setattr(auth, "_users_collection",
                            lambda: StubUsers([_admin_doc(), user]))
        httpd, base = _serve()
        try:
            _, login = _json(base, "/api/login", method="POST",
                             body={"email": "boss@b.co", "password": "pw"})
            status, data = _json(base, "/api/admin/subscription", method="POST",
                                 token=login["token"],
                                 body={"email": "a@b.co", "active": True, "plan": "pro"})
            assert status == 200 and data["user"]["can_scrape"] is True
            status, data = _json(base, "/api/admin/subscription", method="POST",
                                 token=login["token"],
                                 body={"email": "A@B.CO", "active": False})
            assert status == 200 and data["user"]["can_scrape"] is False
        finally:
            httpd.shutdown()

    def test_subscription_unknown_email_404(self, enabled, monkeypatch):
        monkeypatch.setattr(auth, "_users_collection",
                            lambda: StubUsers([_admin_doc()]))
        httpd, base = _serve()
        try:
            _, login = _json(base, "/api/login", method="POST",
                             body={"email": "boss@b.co", "password": "pw"})
            with pytest.raises(urllib.error.HTTPError) as exc:
                _json(base, "/api/admin/subscription", method="POST",
                      token=login["token"],
                      body={"email": "nobody@b.co", "active": True})
            assert exc.value.code == 404
        finally:
            httpd.shutdown()

    def test_admin_api_requires_auth_to_be_enabled(self, monkeypatch):
        monkeypatch.setattr(auth, "AUTH_MONGODB_URI", None)
        monkeypatch.setattr(auth, "JWT_SECRET", None)
        httpd, base = _serve()
        try:
            with pytest.raises(urllib.error.HTTPError) as exc:
                _json(base, "/api/admin/users")
            assert exc.value.code == 501
        finally:
            httpd.shutdown()

    def test_admin_account_lookup_requires_token(self, enabled, monkeypatch):
        monkeypatch.setattr(auth, "_users_collection", lambda: StubUsers([]))
        httpd, base = _serve()
        try:
            with pytest.raises(urllib.error.HTTPError) as exc:
                _json(base, "/api/admin/account?email=a@b.co")
            assert exc.value.code == 401
        finally:
            httpd.shutdown()

    def test_admin_account_returns_profile_no_secret(self, enabled, monkeypatch):
        user = {"_id": "u1", "email": "carlos@gmail.com", "name": "Carlos",
                "password": _bcrypt_hash("pw"), "role": "user", "plan": "free",
                "subscription": {"active": False, "plan": "free",
                                 "expiresAt": None}}
        monkeypatch.setattr(auth, "_users_collection",
                            lambda: StubUsers([_admin_doc(), user]))
        httpd, base = _serve()
        try:
            _, login = _json(base, "/api/login", method="POST",
                             body={"email": "boss@b.co", "password": "pw"})
            status, data = _json(base, "/api/admin/account?email=Carlos@Gmail.com",
                                 token=login["token"])
            assert status == 200
            blob = json.dumps(data)
            assert "Carlos@Gmail.com".lower() in blob
            assert "password" not in data["user"]
            assert "$2b$" not in blob
        finally:
            httpd.shutdown()

    def test_admin_account_unknown_404(self, enabled, monkeypatch):
        monkeypatch.setattr(auth, "_users_collection",
                            lambda: StubUsers([_admin_doc()]))
        httpd, base = _serve()
        try:
            _, login = _json(base, "/api/login", method="POST",
                             body={"email": "boss@b.co", "password": "pw"})
            with pytest.raises(urllib.error.HTTPError) as exc:
                _json(base, "/api/admin/account?email=nobody@b.co",
                      token=login["token"])
            assert exc.value.code == 404
        finally:
            httpd.shutdown()


class TestAdminSecurity:
    """The admin surface must be inert without a valid admin bearer token."""

    def test_subscription_requires_token(self, http):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _json(http, "/api/admin/subscription", method="POST",
                  body={"email": "a@b.co", "active": True})
        assert exc.value.code == 401

    def test_users_list_requires_token(self, http):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _json(http, "/api/admin/users")
        assert exc.value.code == 401

    def test_admin_grant_blocks_cross_site_origin(self, enabled, monkeypatch):
        monkeypatch.setattr(auth, "_users_collection",
                            lambda: StubUsers([_admin_doc()]))
        httpd, base = _serve()
        try:
            _, login = _json(base, "/api/login", method="POST",
                             body={"email": "boss@b.co", "password": "pw"})
            req = urllib.request.Request(
                base + "/api/admin/subscription",
                data=json.dumps({"email": "a@b.co", "active": True}).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {login['token']}",
                         "Origin": "https://evil.example.com"},
                method="POST")
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req, timeout=5)
            assert exc.value.code == 403
            ctype = exc.value.headers.get("Content-Type") or ""
            assert "json" in ctype
            body = json.loads(exc.value.read())
            assert body["ok"] is False
        finally:
            httpd.shutdown()

    def test_admin_list_blocks_cross_site_origin(self, enabled, monkeypatch):
        monkeypatch.setattr(auth, "_users_collection",
                            lambda: StubUsers([_admin_doc()]))
        httpd, base = _serve()
        try:
            _, login = _json(base, "/api/login", method="POST",
                             body={"email": "boss@b.co", "password": "pw"})
            req = urllib.request.Request(
                base + "/api/admin/users",
                headers={"Authorization": f"Bearer {login['token']}",
                         "Origin": "https://evil.example.com"})
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req, timeout=5)
            assert exc.value.code == 403
        finally:
            httpd.shutdown()

    def test_admin_panel_exposes_no_account_secrets(self, enabled, monkeypatch):
        # The list rows and profiles never include the password hash.
        users = StubUsers([_admin_doc(), {
            "_id": "u1", "email": "victim@b.co", "name": "Victim",
            "password": "$2b$10$secretsecretsecretsecretsecre",
            "role": "user", "plan": "free",
            "subscription": {"active": False}}])
        monkeypatch.setattr(auth, "_users_collection", lambda: users)
        httpd, base = _serve()
        try:
            _, login = _json(base, "/api/login", method="POST",
                             body={"email": "boss@b.co", "password": "pw"})
            status, data = _json(base, "/api/admin/users", token=login["token"])
            assert status == 200
            blob = json.dumps(data)
            assert "secretsecretsecret" not in blob
            assert "$2b$" not in blob
            _, me = _json(base, "/api/me", token=login["token"])
            assert "password" not in me
        finally:
            httpd.shutdown()
