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
"""

from __future__ import annotations

import logging
import os
import re
import time
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
