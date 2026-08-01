"""Centralized-authentication integration (see CENTRAL_AUTH_GUIDE.md).

Connects to the shared ``AUTH_MONGODB_URI`` to verify user credentials and
mints/verifies HS256 JWTs signed with the shared ``JWT_SECRET``, so tokens
issued here interoperate with the Node.js/Mongoose platforms that share the
same secret.

Auth is strictly OPT-IN: it activates only when both ``AUTH_MONGODB_URI``
(or ``MONGODB_URI``) and ``JWT_SECRET`` are present. Without them the app
behaves exactly as before (no login, no bearer checks).

Schema assumptions (the standard Mongoose/bcryptjs pattern):
  * ``User`` collection holds ``email`` (and optionally ``username``),
    ``password`` as a bcrypt hash, and a ``role`` string.
  * JWTs carry ``sub`` (user id), ``email`` and ``role`` claims.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

log = logging.getLogger(__name__)

AUTH_MONGODB_URI = os.environ.get("AUTH_MONGODB_URI") or os.environ.get("MONGODB_URI")
JWT_SECRET = os.environ.get("JWT_SECRET")
JWT_ALGORITHM = os.environ.get("NESTICK_JWT_ALGORITHM", "HS256")
JWT_EXPIRES = int(os.environ.get("NESTICK_JWT_EXPIRES", "86400") or "86400")  # 24h


def enabled() -> bool:
    """True when the central auth DB and shared secret are configured."""
    return bool(AUTH_MONGODB_URI and JWT_SECRET)


# --------------------------------------------------------------------------- #
# MongoDB access (lazy so the core stays runnable without PyMongo installed)
# --------------------------------------------------------------------------- #
_client: Any = None


def _users_collection() -> Any:
    """Return the ``users`` collection of the central auth database."""
    global _client
    if _client is None:
        from pymongo import MongoClient

        _client = MongoClient(AUTH_MONGODB_URI, serverSelectionTimeoutMS=3000)
    return _client.get_default_database().users


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

    Returns None on any database or hashing failure (never raises), so a
    down central auth DB simply denies access rather than crashing the API.
    """
    if not enabled():
        return None
    email = (email or "").strip().lower()
    if not email or not password:
        return None
    try:
        user = _users_collection().find_one(
            {"$or": [{"email": email}, {"username": email}]}
        )
    except Exception as exc:  # noqa: BLE001
        log.error("Auth DB unavailable: %s", exc)
        return None
    if not user:
        return None
    stored = user.get("password") or user.get("passwordHash") or ""
    if not stored:
        return None
    if isinstance(stored, str):
        stored = stored.encode("utf-8")
    try:
        import bcrypt

        ok = bcrypt.checkpw(password.encode("utf-8"), stored)
    except Exception:  # noqa: BLE001  (wrong hash format, missing dep, …)
        log.warning("Password check failed for %s", email)
        return None
    return dict(user) if ok else None


# --------------------------------------------------------------------------- #
# JWT — HS256 with the shared secret so Node platforms accept our tokens
# --------------------------------------------------------------------------- #
def issue_token(user: dict[str, Any]) -> str:
    """Sign a short-lived JWT for a verified user document."""
    import jwt

    now = int(time.time())
    payload = {
        "sub": str(user.get("_id") or user.get("id") or user.get("email")),
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
