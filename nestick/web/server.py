"""A dependency-free web UI server.

Runs the scraping pipeline on a background thread and exposes a small JSON API
that the single-page front-end polls. Deliberately built on ``http.server`` so
the whole tool still installs with nothing but ``httpx``.

API
---
``GET  /``                    the dashboard
``GET  /static/<file>``       assets
``POST /api/start``           begin a run           -> {"ok": true}
``POST /api/stop``            graceful stop         -> {"ok": true}
``GET  /api/status``          progress + leads      -> {...}
``GET  /api/settings``        stored API keys
``POST /api/settings``        persist API keys
``GET  /api/download/<fmt>``  csv | json | jsonl | xlsx | md | db
``POST /api/login``           credentials -> JWT (public, opt-in auth)
``POST /api/register``        create account -> JWT (public, opt-in auth)
``GET  /api/me``              profile + subscription state (bearer token)
``GET  /admin.html``          admin panel (static page; API is role-gated)
``GET  /api/admin/users``     list accounts + subscription state (admin)
``GET  /api/admin/account``    find one account by email (admin; 404 if missing)
``POST /api/admin/subscription``  grant/revoke subscription by email (admin)

With central auth enabled (AUTH_MONGODB_URI + JWT_SECRET), every /api/* route
except /api/login, /api/register and /api/auth-status requires a bearer token,
POST /api/start additionally requires an active subscription (or an
admin/owner role), and /api/admin/* requires an admin role — see
CENTRAL_AUTH_GUIDE.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
import queue
import socket
import tempfile
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from ..config import Settings
from ..export import Exporter, summarise
from ..models import Lead, Stats
from ..pipeline import Pipeline
from ..utils import log
from .. import auth

STATIC = Path(__file__).parent / "static"


def _pick_writable_dir(preferred: Path, fallback: Path) -> Path:
    """Return the first directory that can actually be written to.

    Lets the app survive a configured-but-unavailable mount (e.g. the Render
    disk at /var/data is only attached on paid plans; on free tier the path
    does not exist and cannot be created by a non-root process).
    """
    for candidate in (preferred, fallback):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".nestick-write-test"
            probe.write_text("ok", "utf-8")
            probe.unlink()
            return candidate
        except OSError:
            continue
    tmp = Path(tempfile.gettempdir()) / "nestick"
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp


CONFIG_DIR = _pick_writable_dir(
    Path(os.environ.get("NESTICK_CONFIG_DIR", Path.home() / ".nestick")),
    Path.home() / ".nestick",
)
CONFIG_FILE = CONFIG_DIR / "config.json"
SECRET_KEYS = ("serpapi_key", "hunter_key", "google_maps_key", "numverify_key")

#: Only these Host/Origin values may talk to the control panel. Anything else is
#: a cross-site request or DNS rebinding attempt. A leading dot (".") on an
#: entry is a subdomain wildcard, e.g. ".onrender.com" also allows
#: "<app>.onrender.com". localhost stays allowed regardless of configuration.
#: skelersecurity.app is allowed because the shared central-auth frontend is
#: hosted there.
_BASE_ALLOWED_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "::1", "", "skelersecurity.app", ".skelersecurity.app"}
)


def _parse_allowed_hosts() -> frozenset[str]:
    raw = os.environ.get("NESTICK_ALLOWED_HOSTS", "").strip()
    if not raw:
        return _BASE_ALLOWED_HOSTS
    extra = {h.strip().lower() for h in raw.split(",") if h.strip()}
    return _BASE_ALLOWED_HOSTS | frozenset(extra)


ALLOWED_HOSTS: frozenset[str] = _parse_allowed_hosts()
ALLOWED_SUFFIXES: tuple[str, ...] = tuple(
    h.lstrip(".") for h in ALLOWED_HOSTS if h.startswith(".")
)


# --------------------------------------------------------------------------- #
# Persisted settings (mirrors the electron-store behaviour of main.js)
# --------------------------------------------------------------------------- #
def load_config() -> dict[str, Any]:
    with contextlib.suppress(Exception):
        if CONFIG_FILE.is_file():
            return json.loads(CONFIG_FILE.read_text("utf-8"))
    return {}


def save_config(data: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    clean = {
        k: str(v).strip()
        for k, v in (data or {}).items()
        if k in SECRET_KEYS and isinstance(v, (str, int, float)) and str(v).strip()
    }
    CONFIG_FILE.write_text(json.dumps(clean, indent=2), "utf-8")
    with contextlib.suppress(OSError):
        CONFIG_FILE.chmod(0o600)


# --------------------------------------------------------------------------- #
# Log capture so the browser can show what the engine is doing
# --------------------------------------------------------------------------- #
class RingLogHandler(logging.Handler):
    """Keeps the last N log lines for the UI console."""

    def __init__(self, limit: int = 400) -> None:
        super().__init__(logging.INFO)
        self.lines: list[dict[str, Any]] = []
        self.limit = limit
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        with self._lock:
            self.lines.append(
                {"t": time.strftime("%H:%M:%S"), "level": record.levelname, "msg": msg[:400]}
            )
            if len(self.lines) > self.limit:
                del self.lines[: len(self.lines) - self.limit]

    def since(self, index: int) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            return self.lines[index:], len(self.lines)

    def clear(self) -> None:
        with self._lock:
            self.lines.clear()


# --------------------------------------------------------------------------- #
@dataclass
class JobState:
    running: bool = False
    finished: bool = False
    error: str | None = None
    api_errors: list[str] = field(default_factory=list)
    analytics: dict[str, Any] = field(default_factory=dict)
    started: float = 0.0
    ended: float = 0.0
    settings: dict[str, Any] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)


class JobManager:
    """Owns the single active scrape job and its results."""

    def __init__(self) -> None:
        self.state = JobState()
        self.stats = Stats()
        self.leads: list[Lead] = []
        self.log_handler = RingLogHandler()
        self._thread: threading.Thread | None = None
        self._pipeline: Pipeline | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        #: Traffic counters — the desktop launcher uses these to tell whether
        #: the app window is actually connected (a browser process exiting is
        #: not a reliable signal on Windows).
        self.hits = 0
        self.last_hit = 0.0
        self._outdir = Path(tempfile.gettempdir()) / "nestick-ui"
        self._outdir.mkdir(parents=True, exist_ok=True)
        log.addHandler(self.log_handler)
        log.setLevel(logging.INFO)

    # -- lifecycle ------------------------------------------------------ #
    def start(self, form: dict[str, Any]) -> tuple[bool, str]:
        with self._lock:
            if self.state.running:
                return False, "A run is already in progress."
            try:
                settings = self._settings_from_form(form)
            except ValueError as exc:
                return False, str(exc)

            self.log_handler.clear()
            self.leads = []
            self.stats = Stats()
            self.state = JobState(running=True, started=time.time(),
                                  settings=settings.to_dict())
            self._thread = threading.Thread(
                target=self._run, args=(settings,), daemon=True, name="nestick-job"
            )
            self._thread.start()
            return True, "started"

    def _settings_from_form(self, f: dict[str, Any]) -> Settings:
        def s(key: str, default: str = "") -> str:
            v = f.get(key, default)
            if isinstance(v, (list, tuple)):
                v = " ".join(str(x) for x in v)
            elif isinstance(v, dict):
                v = ""
            return str(v or "").strip()

        def i(key: str, default: int) -> int:
            v = f.get(key)
            if isinstance(v, bool) or isinstance(v, (list, tuple, dict)):
                return default
            try:
                n = int(float(v))
            except (TypeError, ValueError):
                return default
            # Reject absurd values outright rather than clamping silently.
            return n if -1_000_000 < n < 1_000_000 else default

        def fl(key: str, default: float) -> float:
            v = f.get(key)
            if isinstance(v, (list, tuple, dict)) or isinstance(v, bool):
                return default
            try:
                n = float(v)
            except (TypeError, ValueError):
                return default
            return n if 0 <= n <= 3600 else default

        queries = [q.strip() for q in s("query").splitlines() if q.strip()]
        urls = [u.strip() for u in s("urls").replace(",", "\n").splitlines() if u.strip()]
        if not queries and not urls:
            raise ValueError("Enter a search query or at least one URL.")

        stored = load_config()
        want = [w for w in ("email", "phone", "social") if f.get(f"want_{w}")]
        base = self._outdir / f"nestick-{time.strftime('%Y%m%d-%H%M%S')}"

        return Settings(
            queries=queries,
            urls=urls,
            engine=s("engine", "auto") or "auto",
            pages=max(1, min(i("pages", 1), 20)),
            location=s("location") or None,
            country=s("country", "us") or "us",
            language=s("language", "en") or "en",
            places=bool(f.get("places")),
            osm_fallback=not f.get("no_osm"),
            serpapi_key=s("serpapi_key") or stored.get("serpapi_key") or None,
            hunter_key=s("hunter_key") or stored.get("hunter_key") or None,
            google_maps_key=s("google_maps_key") or stored.get("google_maps_key") or None,
            numverify_key=s("numverify_key") or stored.get("numverify_key") or None,
            verify_mx=bool(f.get("verify_mx", True)),
            concurrency=max(1, min(i("concurrency", 24), 128)),
            max_pages_per_site=max(1, min(i("max_pages", 5), 50)),
            depth=max(0, min(i("depth", 1), 3)),
            delay=fl("delay", 0.0),
            timeout=max(1.0, fl("timeout", 15.0)),
            respect_robots=bool(f.get("respect_robots")),
            want=tuple(want) or ("email", "phone", "social"),
            min_confidence=min(1.0, max(0.0, fl("min_confidence", 0.0))),
            cache=bool(f.get("cache")),
            resume=False,
            output=str(base),
            formats=("csv", "json", "xlsx"),
            progress=False,
            quiet=False,
        )

    def _run(self, settings: Settings) -> None:
        async def main() -> None:
            self._loop = asyncio.get_running_loop()
            async with Pipeline(settings) as pipe:
                self._pipeline = pipe
                pipe.on_lead = self._on_lead
                self.stats = pipe.stats
                leads = await pipe.run()
                self.leads = leads
                self.state.api_errors = pipe.api_errors
                try:
                    self.state.analytics = pipe.analytics
                except Exception:
                    self.state.analytics = {}
                if leads:
                    paths = Exporter(settings).write(leads, pipe.stats)
                    self.state.files = [str(p) for p in paths]

        try:
            asyncio.run(main())
        except Exception as exc:  # noqa: BLE001
            self.state.error = f"{type(exc).__name__}: {exc}"
            log.error("Run failed: %s", exc)
        finally:
            self.state.running = False
            self.state.finished = True
            self.state.ended = time.time()
            self._pipeline = None
            self._loop = None

    def _on_lead(self, lead: Lead, stats: Stats) -> None:
        # Pipeline reports each finished site; keep an ordered snapshot.
        self.leads = sorted(
            self._pipeline.leads.values() if self._pipeline else [],
            key=lambda l: (-l.score, l.domain),
        )

    def stop(self) -> bool:
        pipe, loop = self._pipeline, self._loop
        if pipe is None or loop is None:
            return False
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(pipe._request_stop)
        return True

    # -- reporting ------------------------------------------------------ #
    def status(self, log_index: int = 0) -> dict[str, Any]:
        leads = list(self.leads)
        lines, next_index = self.log_handler.since(log_index)
        return {
            "running": self.state.running,
            "finished": self.state.finished,
            "error": self.state.error,
            "api_errors": list(self.state.api_errors),
            "analytics": dict(self.state.analytics),
            "elapsed": round(
                (self.state.ended or time.time()) - self.state.started, 1
            ) if self.state.started else 0,
            "stats": self.stats.as_row(),
            "summary": summarise(leads),
            "leads": [self._lead_row(l) for l in leads[:500]],
            "files": [Path(f).name for f in self.state.files],
            "log": lines,
            "log_index": next_index,
        }

    @staticmethod
    def _lead_row(l: Lead) -> dict[str, Any]:
        return {
            "domain": l.domain,
            "name": l.name or "",
            "url": l.url,
            "score": l.score,
            "emails": l.emails,
            "phones": l.phones,
            "socials": l.socials,
            "address": l.address or "",
            "rating": l.rating,
            "pages": l.pages_crawled,
        }

    def file_for(self, fmt: str) -> Path | None:
        want = {"csv": ".csv", "json": ".json", "jsonl": ".jsonl",
                "xlsx": ".xlsx", "md": ".md", "db": ".db"}.get(fmt)
        if not want:
            return None
        for f in self.state.files:
            if f.endswith(want):
                p = Path(f)
                if p.is_file():
                    return p
        # Not produced by the run (e.g. jsonl) — generate it on demand.
        if self.leads and self.state.settings.get("output"):
            s = Settings(
                urls=["https://placeholder.invalid"],
                output=self.state.settings["output"],
                formats=(fmt,),
            )
            with contextlib.suppress(Exception):
                paths = Exporter(s).write(self.leads, self.stats)
                if paths:
                    self.state.files.append(str(paths[0]))
                    return paths[0]
        return None


# --------------------------------------------------------------------------- #
def make_handler(jobs: JobManager) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "Nestick"  # SkelerSecurity Intelligence Engine
        #: HTTP/1.0 (the default) closes the socket after every response, which
        #: browsers report as an intermittent network failure during polling.
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            pass  # keep the console clean

        def handle_one_request(self) -> None:
            # A client that vanishes mid-request must not print a traceback.
            try:
                super().handle_one_request()
            except (ConnectionResetError, BrokenPipeError, TimeoutError):
                self.close_connection = True

        # -- helpers ---------------------------------------------------- #
        def _set_cors(self) -> None:
            """Allow an allowed cross-origin frontend (e.g. the Vercel build)
            to call the API. Echoes the origin back only when it passes the
            Host/Origin allow-list, so the guard is not weakened."""
            origin = self.headers.get("Origin")
            if origin and self._origin_ok():
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")

        def _fail(self, status: int, message: str) -> None:
            """Send a JSON error, never HTML — the UI parses every reply."""
            self._json({"ok": False, "error": message, "status": status}, status)

        def _json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, default=str).encode()
            self.send_response(status)
            self._set_cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)

        def _static(self, name: str) -> None:
            safe = Path(name).name  # no traversal
            path = STATIC / safe
            if not path.is_file():
                self.send_error(404)
                return
            data = path.read_bytes()
            ctype = mimetypes.guess_type(safe)[0] or "application/octet-stream"
            self.send_response(200)
            self._set_cors()
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(data)

        def do_OPTIONS(self) -> None:  # noqa: N802
            """CORS preflight for the cross-origin (Vercel) frontend."""
            if self._deny_cross_site():
                return
            self.send_response(204)
            self._set_cors()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Max-Age", "86400")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _origin_ok(self) -> bool:
            """Block cross-site requests and DNS rebinding.

            Without this, any page you happen to be browsing could POST to
            http://localhost:8765 and read back your saved API keys. On a public
            deployment the allowed set is extended with ``NESTICK_ALLOWED_HOSTS``.
            """
            def ok(hostname: str) -> bool:
                hostname = (hostname or "").split(":")[0].strip("[]").lower()
                if not hostname or hostname in ALLOWED_HOSTS:
                    return True
                return any(hostname.endswith(s) for s in ALLOWED_SUFFIXES)

            host = self.headers.get("Host") or ""
            if not ok(host):
                return False
            origin = self.headers.get("Origin")
            if origin:
                if not ok(urlsplit(origin).hostname or ""):
                    return False
            return True

        def _deny_cross_site(self) -> bool:
            if self._origin_ok():
                return False
            self._fail(403, "Cross-site request blocked. Open the app from "
                            "http://127.0.0.1 rather than another site.")
            return True

        def _authed(self) -> bool:
            """Return True when the request may proceed.

            No-op unless central auth is configured (AUTH_MONGODB_URI +
            JWT_SECRET). When enabled, every /api/ route except /api/login
            requires a valid ``Authorization: Bearer <jwt>`` header.
            """
            if not auth.enabled():
                return True
            if auth.verify_token(auth.bearer_token(self.headers.get("Authorization"))):
                return True
            self._fail(401, "Authentication required. POST your credentials to "
                            "/api/login to obtain a token.")
            return False

        def _current_user(self) -> dict[str, Any] | None:
            """Live user document for the bearer token.

            Returns None when auth is disabled (the caller decides what to
            report). When auth is enabled the token has already passed
            ``_authed()``, so a None here means the account was deleted (401)
            or the auth database went down (503) — the error is sent here.
            """
            if not auth.enabled():
                return None
            claims = auth.verify_token(auth.bearer_token(self.headers.get("Authorization")))
            if not claims:
                return None
            user = auth.user_by_id(claims.get("userId"))
            if user is not None:
                return user
            if auth.last_error and auth.last_error.startswith("Auth database unreachable"):
                self._fail(503, auth.last_error)
            else:
                self._fail(401, "That account no longer exists.")
            return None

        def _require_admin(self) -> dict[str, Any] | None:
            """Return the admin user document, or send the error and return None.

            Admin endpoints only make sense with central auth on; without it
            there are no roles to check, so the API answers 501. With auth on,
            the caller must hold a valid token for an account whose ``role`` is
            in :data:`auth.ADMIN_ROLES` (assigned in the database).
            """
            if not auth.enabled():
                self._fail(501, "The admin API requires central auth to be enabled "
                                "(AUTH_MONGODB_URI + JWT_SECRET).")
                return None
            user = self._current_user()
            if user is None:
                return None  # _current_user already sent the error
            if str(user.get("role") or "").strip().lower() not in auth.ADMIN_ROLES:
                self._fail(403, "Administrator access required. Your account role "
                                "does not allow this.")
                return None
            return user

        MAX_BODY = 2_000_000

        def _body(self) -> dict[str, Any]:
            """Read and decode a JSON object body, tolerating anything.

            Always drains the socket even when the payload is rejected —
            skipping the read leaves unread bytes that break the connection
            (the client sees "Broken pipe"). Always returns a dict, because a
            JSON array or ``null`` would otherwise crash callers doing .get().
            """
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            if length <= 0:
                return {}

            remaining = length
            chunks: list[bytes] = []
            oversized = length > self.MAX_BODY
            try:
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    if not oversized:
                        chunks.append(chunk)
            except (ConnectionResetError, BrokenPipeError, TimeoutError, OSError):
                return {}
            if oversized:
                return {}

            try:
                data = json.loads(b"".join(chunks).decode("utf-8", "replace"))
            except Exception:
                return {}
            # A list, string, number or null is valid JSON but not a form.
            return data if isinstance(data, dict) else {}

        # -- routes ----------------------------------------------------- #
        def do_GET(self) -> None:  # noqa: N802
            try:
                self._route_get()
            except (ConnectionResetError, BrokenPipeError):
                self.close_connection = True
            except Exception as exc:  # noqa: BLE001
                log.exception("GET %s failed", self.path)
                with contextlib.suppress(Exception):
                    self._fail(500, f"Internal error: {type(exc).__name__}")

        def _route_get(self) -> None:
            jobs.hits += 1
            jobs.last_hit = time.monotonic()
            path = unquote(urlsplit(self.path).path)
            if path.startswith("/api/") and self._deny_cross_site():
                return
            if path == "/api/auth-status":
                # Public probe: is auth on, and can we reach the auth DB?
                self._json(auth.db_status())
                return
            if path.startswith("/api/") and not self._authed():
                return
            if path in ("/", "/index.html"):
                self._static("index.html")
            elif path in ("/admin", "/admin.html"):
                self._static("admin.html")
            elif path.startswith("/static/"):
                self._static(path[len("/static/"):])
            elif path == "/api/me":
                # Profile + subscription state so the UI can gate scraping.
                user = self._current_user()
                if user is None and auth.enabled():
                    return  # _current_user already sent the error
                if user is None:
                    self._json({"enabled": False, "email": None, "name": None,
                                "role": None, "plan": "free",
                                "subscription": {"plan": "free", "active": True,
                                                 "expiresAt": None},
                                "can_scrape": True})
                else:
                    self._json({"enabled": True, **auth.public_profile(user)})
            elif path == "/api/admin/users":
                if self._require_admin() is None:
                    return
                qs = urlsplit(self.path).query
                q = ""
                for part in qs.split("&"):
                    if part.startswith("q="):
                        q = unquote(part[3:])
                rows = auth.list_users(q)
                if rows is None:
                    self._fail(503, auth.last_error or "Could not list accounts.")
                    return
                self._json({"items": rows})
            elif path == "/api/admin/account":
                if self._require_admin() is None:
                    return
                qs = urlsplit(self.path).query
                email = ""
                for part in qs.split("&"):
                    if part.startswith("email="):
                        email = unquote(part[6:])
                profile = auth.find_account(email)
                if profile is None:
                    reason = auth.last_error or "No account found."
                    if reason.startswith("Auth database unreachable"):
                        status = 503
                    elif "No account found" in reason:
                        status = 404
                    else:
                        status = 400
                    self._fail(status, reason)
                    return
                self._json({"user": profile})
            elif path == "/api/status":
                qs = urlsplit(self.path).query
                idx = 0
                for part in qs.split("&"):
                    if part.startswith("log="):
                        with contextlib.suppress(ValueError):
                            idx = int(part[4:])
                self._json(jobs.status(idx))
            elif path == "/api/settings":
                cfg = load_config()
                self._json({k: bool(cfg.get(k)) for k in SECRET_KEYS})
            elif path.startswith("/api/download/"):
                fmt = path.rsplit("/", 1)[-1]
                p = jobs.file_for(fmt)
                if not p:
                    self._fail(404, "That export is not ready yet — run a scrape first.")
                    return
                data = p.read_bytes()
                self.send_response(200)
                self._set_cors()
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{p.name}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(data)
            elif path == "/api/jobs":
                from ..resources import JobStore

                store = JobStore(CONFIG_DIR / "jobs.json")
                self._json({"items": [j.to_dict() for j in store.list()]})
            elif path == "/healthz":
                self._json({"status": "ok"})
            elif path.startswith("/api/"):
                self._fail(404, f"Unknown endpoint: {path}")
            else:
                self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._route_post()
            except (ConnectionResetError, BrokenPipeError):
                self.close_connection = True
            except Exception as exc:  # noqa: BLE001
                log.exception("POST %s failed", self.path)
                with contextlib.suppress(Exception):
                    self._fail(500, f"Internal error: {type(exc).__name__}")

        def _route_post(self) -> None:
            jobs.hits += 1
            jobs.last_hit = time.monotonic()
            if self._deny_cross_site():
                return
            path = unquote(urlsplit(self.path).path)
            if path == "/api/login":
                self._login()
            elif path == "/api/register":
                self._register()
            elif path.startswith("/api/") and not self._authed():
                return
            elif path == "/api/start":
                if auth.enabled():
                    user = self._current_user()
                    if user is None:
                        return  # _current_user already sent the error
                    if not auth.can_scrape(user):
                        self._fail(403, "Scraping requires an active subscription. "
                                        "Contact your administrator to enable your plan.")
                        return
                ok, msg = jobs.start(self._body())
                self._json({"ok": ok, "message": msg}, 200 if ok else 400)
            elif path == "/api/stop":
                self._json({"ok": jobs.stop()})
            elif path == "/api/settings":
                save_config(self._body())
                self._json({"ok": True})
            elif path == "/api/admin/subscription":
                if self._require_admin() is None:
                    return
                body = self._body()
                user = auth.set_subscription(
                    str(body.get("email") or ""),
                    bool(body.get("active")),
                    str(body.get("plan") or "") or None,
                    body.get("expiresAt"),
                )
                if user is None:
                    reason = auth.last_error or "Could not update the subscription."
                    if reason.startswith("Auth database unreachable"):
                        status = 503
                    elif "No account found" in reason:
                        status = 404
                    else:
                        status = 400
                    self._fail(status, reason)
                    return
                self._json({"ok": True, "user": auth.public_profile(user)})
            else:
                self._fail(404, f"Unknown endpoint: {path}")

        def _login(self) -> None:
            """Public endpoint: verify credentials and return a JWT.

            Stays public so the login screen can obtain a token. When central
            auth is not configured it responds 501 so the UI knows it is
            running without authentication. Distinguishes a wrong password
            (401) from an unreachable database (503).
            """
            if not auth.enabled():
                self._fail(501, "Authentication is not enabled on this server.")
                return
            body = self._body()
            user = auth.verify_user(
                str(body.get("email") or body.get("username") or ""),
                str(body.get("password") or ""),
            )
            if not user:
                reason = auth.last_error or "Invalid email or password."
                status = 503 if reason.startswith("Auth database unreachable") else 401
                self._fail(status, reason)
                return
            self._json({"ok": True, "token": auth.issue_token(user),
                        "email": user.get("email") or user.get("username"),
                        "user": auth.public_profile(user)})

        def _register(self) -> None:
            """Public endpoint: create an account and return a JWT for it.

            Kept public like /api/login so the login screen's "Create account"
            tab can self-service. New accounts start with no active
            subscription; roles and plans are assigned from the database by an
            administrator (never via this endpoint).
            """
            if not auth.enabled():
                self._fail(501, "Authentication is not enabled on this server.")
                return
            body = self._body()
            user = auth.register_user(
                str(body.get("name") or ""),
                str(body.get("email") or body.get("username") or ""),
                str(body.get("password") or ""),
            )
            if not user:
                reason = auth.last_error or "Registration failed."
                if reason.startswith("Auth database unreachable"):
                    status = 503
                elif "already exists" in reason:
                    status = 409
                else:
                    status = 400
                self._fail(status, reason)
                return
            self._json({"ok": True, "token": auth.issue_token(user),
                        "email": user.get("email") or user.get("username"),
                        "user": auth.public_profile(user)})

    return Handler


def _free_port(host: str, preferred: int) -> int:
    """Return ``preferred`` if it is bindable, otherwise an OS-assigned port."""
    try:
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, preferred))
        return preferred
    except OSError:
        pass
    with socket.socket() as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def serve(host: str | None = None, port: int | None = None, open_browser: bool = True):
    """Start the dashboard and block until interrupted.

    Defaults resolve from the environment so a plain ``nestick-ui`` process
    works unchanged on a PaaS like Render: ``PORT`` (Render's convention) is
    honoured, and when ``PORT`` is set the panel binds ``0.0.0.0`` and does not
    try to open a browser.
    """
    if host is None:
        host = os.environ.get(
            "NESTICK_UI_HOST",
            "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1",
        )
    if port is None:
        raw = os.environ.get("NESTICK_UI_PORT") or os.environ.get("PORT") or "8765"
        with contextlib.suppress(ValueError):
            port = int(raw)
        if port is None:
            port = 8765
    if os.environ.get("NESTICK_NO_BROWSER"):
        open_browser = False

    jobs = JobManager()
    chosen = _free_port(host, port)
    if chosen != port:
        print(f"  Port {port} is busy — using {chosen} instead.")
    httpd = ThreadingHTTPServer((host, chosen), make_handler(jobs))
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{chosen}"

    print(f"\n  Nestick Tech Lead Generator  →  \033[1;36m{url}\033[0m")
    print("  SkelerSecurity Intelligence Engine")
    print("  Press Ctrl-C to stop.\n", flush=True)
    if open_browser:
        def _open() -> None:
            with contextlib.suppress(Exception):
                webbrowser.open(url)

        threading.Timer(0.7, _open).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down…")
    finally:
        httpd.shutdown()
    return 0


def launch(**kw: Any) -> int:
    return serve(**kw)
