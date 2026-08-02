"""Centralized-authentication integration (see CENTRAL_AUTH_GUIDE.md).

Connects to the shared ``AUTH_MONGODB_URI`` to verify user credentials and
mints/verifies HS256 JWTs signed with the shared ``JWT_SECRET``, so tokens
issued here interoperate with the Node.js/Mongoose platforms that share the
same secret.

Auth is strictly OPT-IN: it activates only when both ``AUTH_MONGODB_URI``
(or ``MONGODB_URI``) and ``JWT_SECRET`` are present. Without them the app
behaves exactly as before (no login, no bearer checks).

Schema assumptions (the Mongoose/bcryptjs pattern used by the central
auth platform, e.g. KeywordSearch's ``Backend/models/User.js``):
  * the accounts collection is named ``User Accounts`` (env override
    ``AUTH_MONGODB_COLLECTION``), holding ``email`` (lowercased), a bcrypt
    ``password`` hash, ``name`` and a ``role`` string;
  * JWTs carry a ``userId`` claim (the ``_id`` string) so tokens minted here
    are accepted by the Node platforms, which do ``User.findById(decoded.userId)``.

Subscriptions are DB-driven: a user document carries ``plan`` (e.g. ``free``)
and an optional ``subscription`` object with ``active`` and ``expiresAt``.
Only subscribed users (or admin roles) may scrape — ``POST /api/start`` is
gated. There is no billing provider; an administrator grants/revokes access
by editing the document in MongoDB, and new self-service accounts from
``POST /api/register`` always start inactive.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlsplit

log = logging.getLogger(__name__)

AUTH_MONGODB_URI = os.environ.get("AUTH_MONGODB_URI") or os.environ.get("MONGODB_URI")
JWT_SECRET = os.environ.get("JWT_SECRET")
JWT_ALGORITHM = os.environ.get("NESTICK_JWT_ALGORITHM", "HS256")
JWT_EXPIRES = int(os.environ.get("NESTICK_JWT_EXPIRES", "86400") or "86400")  # 24h

#: Human-readable reason for the last failed verify_user() call (None when OK).
last_error: str | None = None

#: Collection holding central-auth accounts ("User Accounts" in the Node
#: platform: ``Backend/models/User.js`` sets ``collection: 'User Accounts'``).
AUTH_MONGODB_COLLECTION = os.environ.get("AUTH_MONGODB_COLLECTION", "User Accounts")


def enabled() -> bool:
    """True when the central auth DB and shared secret are configured."""
    return bool(AUTH_MONGODB_URI and JWT_SECRET)


# --------------------------------------------------------------------------- #
# MongoDB access (lazy so the core stays runnable without PyMongo installed)
# --------------------------------------------------------------------------- #
_client: Any = None


def _db_name() -> str:
    """Database name for the central auth DB.

    Prefers AUTH_MONGODB_NAME, then the database embedded in the URI path
    (e.g. ``.../CentralAuthDB``), then the guide's default.
    """
    name = os.environ.get("AUTH_MONGODB_NAME")
    if name:
        return name
    path = urlsplit(AUTH_MONGODB_URI or "").path
    seg = [unquote(x) for x in path.split("/") if x]
    if seg:
        return seg[0]
    return "CentralAuthDB"


def _users_collection() -> Any:
    """Return the accounts collection of the central auth database."""
    global _client
    if _client is None:
        from pymongo import MongoClient

        _client = MongoClient(
            AUTH_MONGODB_URI, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000
        )
    return _client[_db_name()][AUTH_MONGODB_COLLECTION]


def db_status() -> dict[str, Any]:
    """Reachability probe used by /api/auth-status (never raises)."""
    info: dict[str, Any] = {
        "enabled": enabled(),
        "reachable": False,
        "database": None,
        "collection": AUTH_MONGODB_COLLECTION,
        "users": 0,
    }
    if not enabled():
        info["reason"] = "AUTH_MONGODB_URI and JWT_SECRET must both be set."
        return info
    try:
        coll = _users_collection()
        info["database"] = coll.database.name
        coll.database.client.admin.command("ping")
        info["reachable"] = True
        try:
            info["users"] = coll.estimated_document_count()
        except Exception:  # noqa: BLE001
            info["users"] = -1
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def _reset_client() -> None:
    """Close the cached client (used by tests)."""
    global _client
    if _client is not None:
        with _client:
            _client.close()
        _client = None


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
def verify_user(email: str, password: str) -> dict[str, Any] | None:
    """Return the user document when the credentials are valid, else None.

    Never raises; the exact failure reason is left in ``last_error`` so the
    API can tell a wrong password from a down database.
    """
    global last_error
    last_error = None
    if not enabled():
        last_error = "Authentication is not enabled (AUTH_MONGODB_URI/JWT_SECRET missing)."
        return None
    email = (email or "").strip().lower()
    if not email or not password:
        last_error = "Email and password are required."
        return None
    try:
        esc = re.escape(email)
        user = _users_collection().find_one(
            {"$or": [
                {"email": {"$regex": f"^{esc}$", "$options": "i"}},
                {"username": {"$regex": f"^{esc}$", "$options": "i"}},
            ]}
        )
    except Exception as exc:  # noqa: BLE001
        last_error = f"Auth database unreachable: {type(exc).__name__}: {exc}"
        log.error("Auth DB unavailable: %s", exc)
        return None
    if not user:
        last_error = "No account found for that email address."
        return None
    stored = user.get("password") or user.get("passwordHash") or ""
    if not stored:
        last_error = "That account has no stored password."
        return None
    if isinstance(stored, str):
        stored = stored.encode("utf-8")
    if not stored.startswith(b"$2"):
        last_error = ("Stored password is not a bcrypt hash ($2a/$2b/$2y). The central "
                      "auth platform must store bcrypt hashes for Python verification.")
        return None
    try:
        import bcrypt

        ok = bcrypt.checkpw(password.encode("utf-8"), stored)
    except Exception as exc:  # noqa: BLE001  (wrong hash format, missing dep, …)
        last_error = f"Password check failed: {type(exc).__name__}: {exc}"
        log.warning("Password check failed for %s", email)
        return None
    if not ok:
        last_error = "Incorrect password."
        return None
    return dict(user)


# --------------------------------------------------------------------------- #
# JWT — HS256 with the shared secret so Node platforms accept our tokens
# --------------------------------------------------------------------------- #
def issue_token(user: dict[str, Any]) -> str:
    """Sign a short-lived JWT for a verified user document.

    The ``userId`` claim matches the Node platform
    (``jsonwebtoken.sign({ userId }, JWT_SECRET)``), so tokens issued here are
    accepted by KeywordSearch and any other platform sharing ``JWT_SECRET``.
    ``sub``/``email``/``role`` are kept as convenience claims.
    """
    import jwt

    uid = str(user.get("_id") or user.get("id") or user.get("userId") or user.get("email"))
    now = int(time.time())
    payload = {
        "userId": uid,
        "sub": uid,
        "email": user.get("email") or user.get("username") or "",
        "role": user.get("role") or "user",
        "iat": now,
        "exp": now + JWT_EXPIRES,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> dict[str, Any] | None:
    """Validate a JWT against the shared secret. None when invalid/expired."""
    if not enabled() or not token:
        return None
    try:
        import jwt

        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except Exception:  # noqa: BLE001
        return None


def bearer_token(header: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <token>`` header."""
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


# --------------------------------------------------------------------------- #
# Accounts & subscriptions (DB-driven; an admin edits the user document)
# --------------------------------------------------------------------------- #
#: Plans treated as "subscribed" when the document carries a plan string but
#: no explicit ``subscription`` object (legacy/seed accounts).
PAID_PLANS = frozenset({"pro", "premium", "business", "enterprise", "agency", "lifetime", "paid"})

#: Roles that may scrape regardless of subscription state (assigned by the
#: database — there is no self-signup path to an admin role).
ADMIN_ROLES = frozenset({"admin", "administrator", "owner", "root", "superadmin"})

PASSWORD_MIN_LENGTH = 6


def register_user(name: str, email: str, password: str,
                  role: str = "user", plan: str = "free") -> dict[str, Any] | None:
    """Create a self-service account in the central auth DB.

    Mirrors the Node platform's schema (bcrypt ``password``, ``role`` string)
    and adds ``plan`` + ``subscription`` so scraping can be gated per-account.
    New accounts always start with an INACTIVE subscription; an administrator
    flips ``subscription.active`` (or sets a paid ``plan``) in MongoDB to let
    the user scrape. Never raises; failures are left in ``last_error``.
    """
    global last_error
    last_error = None
    if not enabled():
        last_error = "Authentication is not enabled (AUTH_MONGODB_URI/JWT_SECRET missing)."
        return None
    email = (email or "").strip().lower()
    name = (name or "").strip()
    if not email or "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        last_error = "A valid email address is required."
        return None
    if not password or len(password) < PASSWORD_MIN_LENGTH:
        last_error = f"Password must be at least {PASSWORD_MIN_LENGTH} characters."
        return None
    try:
        import bcrypt

        coll = _users_collection()
        esc = re.escape(email)
        existing = coll.find_one(
            {"$or": [
                {"email": {"$regex": f"^{esc}$", "$options": "i"}},
                {"username": {"$regex": f"^{esc}$", "$options": "i"}},
            ]}
        )
        if existing:
            last_error = "An account already exists for that email address."
            return None
        doc: dict[str, Any] = {
            "email": email,
            "username": email,
            "name": name or email.rsplit("@", 1)[0],
            "password": bcrypt.hashpw(password.encode("utf-8"),
                                      bcrypt.gensalt()).decode("utf-8"),
            "role": role,
            "plan": plan,
            "subscription": {"active": False, "plan": plan, "expiresAt": None},
            "createdAt": datetime.now(timezone.utc),
            "provider": "nestick",
        }
        res = coll.insert_one(doc)
        doc["_id"] = res.inserted_id
        return doc
    except Exception as exc:  # noqa: BLE001
        last_error = f"Registration failed: {type(exc).__name__}: {exc}"
        log.error("Registration failed: %s", exc)
        return None


def user_by_id(uid: Any) -> dict[str, Any] | None:
    """Fetch a live user document by ``_id`` (accepts str or ObjectId).

    Returns None when the account no longer exists; when the auth database is
    unreachable the reason is left in ``last_error`` (prefix
    ``Auth database unreachable``) so the API can return 503 instead of 401.
    """
    global last_error
    last_error = None
    if not enabled() or not uid:
        return None
    try:
        from bson import ObjectId

        coll = _users_collection()
        query: dict[str, Any]
        if isinstance(uid, str):
            try:
                query = {"_id": ObjectId(uid)}
            except Exception:  # noqa: BLE001
                query = {"_id": uid}
        else:
            query = {"_id": uid}
        user = coll.find_one(query)
    except Exception as exc:  # noqa: BLE001
        last_error = f"Auth database unreachable: {type(exc).__name__}: {exc}"
        log.error("Auth DB unavailable: %s", exc)
        return None
    return dict(user) if user else None


def user_for_token(token: str) -> dict[str, Any] | None:
    """Resolve the live user document for a bearer token (None when invalid)."""
    claims = verify_token(token)
    if not claims:
        return None
    return user_by_id(claims.get("userId"))


def _expiry_past(value: Any) -> bool:
    """True when an expiry value (ISO-8601 string or epoch number) is in the past."""
    now = time.time()
    if isinstance(value, datetime):
        return value.timestamp() <= now
    if isinstance(value, (int, float)):
        ts = value
        if ts > 1e12:  # milliseconds
            ts /= 1000
        return ts <= now
    s = str(value or "").strip()
    if not s:
        return False
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            return float(s) <= now
        except ValueError:
            return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() <= now


def subscription_status(user: dict[str, Any]) -> dict[str, Any]:
    """Derive the public subscription state for a user document.

    An account is subscribed when its ``subscription`` object has ``active``
    truthy and is not past ``expiresAt``, or (for legacy/seed docs) its
    ``plan`` string is in :data:`PAID_PLANS`. An explicit ``subscription``
    object overrides the plan string, so an admin can revoke access.
    """
    plan = str(user.get("plan") or "free").strip() or "free"
    sub = user.get("subscription")
    if isinstance(sub, dict):
        active = bool(sub.get("active"))
        expires = sub.get("expiresAt") or sub.get("expires_at") or sub.get("expires")
    elif isinstance(sub, str):
        active = bool(sub) and sub.strip().lower() not in ("free", "none", "inactive", "0", "false")
        expires = None
    else:
        active = plan.lower() in PAID_PLANS
        expires = None
    if active and expires:
        try:
            if _expiry_past(expires):
                active = False
        except Exception:  # noqa: BLE001
            pass  # unparseable expiry is treated as "no expiry set"
    return {"plan": plan, "active": active,
            "expiresAt": expires.strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(expires, datetime) else expires}


def can_scrape(user: dict[str, Any]) -> bool:
    """Whether a user may run scrapes.

    Admin/owner roles (assigned in the DB) always may; everyone else needs an
    active subscription. This is the gate applied by ``POST /api/start``.
    """
    if str(user.get("role") or "").strip().lower() in ADMIN_ROLES:
        return True
    return subscription_status(user)["active"]


def public_profile(user: dict[str, Any]) -> dict[str, Any]:
    """Safe, UI-facing subset of a user document (never exposes the hash)."""
    sub = subscription_status(user)
    return {
        "email": user.get("email") or user.get("username") or "",
        "name": user.get("name") or "",
        "role": user.get("role") or "user",
        "plan": sub["plan"],
        "subscription": sub,
        "can_scrape": can_scrape(user),
    }
