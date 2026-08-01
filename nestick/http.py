"""Asynchronous fetching core: HTTP/2, rotation, adaptive throttling, caching."""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import random
import re
import sqlite3
import time
import urllib.robotparser
import zlib
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import httpx

from .config import CLIENT_HINTS, Settings
from .models import Response, Stats
from .security import BlockedTarget, check_url
from .utils import host_of, log

_CHROME_RE = re.compile(r"Chrome/(\d+)")


def api_error_message(payload: Any) -> str | None:
    """Extract a human error from the various vendor JSON shapes.

    SerpApi  ``{"error": "..."}``
    Hunter   ``{"errors": [{"details": "..."}]}``
    Google   ``{"status": "REQUEST_DENIED", "error_message": "..."}``
    """
    if not isinstance(payload, dict):
        return None
    err = payload.get("error")
    if isinstance(err, str) and err:
        return err
    if isinstance(err, dict):  # Google-style {"error": {"message": ...}}
        msg = err.get("message")
        if isinstance(msg, str) and msg:
            return msg
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            for key in ("details", "message", "detail", "id"):
                if isinstance(first.get(key), str) and first[key]:
                    return first[key]
        elif isinstance(first, str):
            return first
    if isinstance(payload.get("error_message"), str) and payload["error_message"]:
        return payload["error_message"]
    status = payload.get("status")
    if isinstance(status, str) and status not in ("OK", "ZERO_RESULTS", "Success"):
        return status
    return None


# --------------------------------------------------------------------------- #
# Persistent response cache
# --------------------------------------------------------------------------- #
class ResponseCache:
    """Tiny thread-safe SQLite cache so re-runs cost nothing."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS pages (
        key      TEXT PRIMARY KEY,
        url      TEXT NOT NULL,
        status   INTEGER NOT NULL,
        body     BLOB NOT NULL,
        ts       REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS pages_ts ON pages(ts);
    """

    def __init__(self, path: str, ttl: int = 86_400, enabled: bool = True) -> None:
        self.enabled = enabled
        self.ttl = ttl
        self._lock = asyncio.Lock()
        self._db: sqlite3.Connection | None = None
        if not enabled:
            return
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._db.executescript(self._SCHEMA)
        with contextlib.suppress(sqlite3.DatabaseError):
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.commit()

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.blake2b(url.encode("utf-8", "ignore"), digest_size=16).hexdigest()

    async def get(self, url: str) -> Response | None:
        if not self._db:
            return None
        async with self._lock:
            row = self._db.execute(
                "SELECT status, body, ts FROM pages WHERE key=?", (self._key(url),)
            ).fetchone()
        if not row:
            return None
        status, body, ts = row
        if self.ttl and time.time() - ts > self.ttl:
            return None
        try:
            text = gzip.decompress(body).decode("utf-8", "replace")
        except Exception:
            return None
        return Response(url=url, status=status, text=text, from_cache=True)

    async def put(self, resp: Response) -> None:
        if not self._db or not resp.ok:
            return
        blob = gzip.compress(resp.text.encode("utf-8", "ignore"), 5)
        async with self._lock:
            with contextlib.suppress(sqlite3.DatabaseError):
                self._db.execute(
                    "INSERT OR REPLACE INTO pages(key,url,status,body,ts) VALUES(?,?,?,?,?)",
                    (self._key(resp.url), resp.url, resp.status, blob, time.time()),
                )
                self._db.commit()

    def close(self) -> None:
        if self._db:
            with contextlib.suppress(Exception):
                self._db.close()
            self._db = None


# --------------------------------------------------------------------------- #
# Adaptive per-host throttle (AIMD)
# --------------------------------------------------------------------------- #
class HostGovernor:
    """Per-host concurrency + delay that backs off on 429/5xx and recovers."""

    __slots__ = ("_sem", "_delay", "_next", "_base", "_limit", "_lock")

    def __init__(self, per_host: int, base_delay: float) -> None:
        self._limit = per_host
        self._base = base_delay
        self._sem: dict[str, asyncio.Semaphore] = {}
        self._delay: dict[str, float] = defaultdict(lambda: base_delay)
        self._next: dict[str, float] = defaultdict(float)
        self._lock = asyncio.Lock()

    def sem(self, host: str) -> asyncio.Semaphore:
        s = self._sem.get(host)
        if s is None:
            s = self._sem[host] = asyncio.Semaphore(self._limit)
        return s

    async def wait(self, host: str, jitter: float) -> None:
        async with self._lock:
            now = time.monotonic()
            gap = self._delay[host]
            start = max(now, self._next[host])
            self._next[host] = start + gap * (1 + random.uniform(-jitter, jitter))
        sleep_for = start - time.monotonic()
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)

    def penalise(self, host: str) -> None:
        self._delay[host] = min(max(self._delay[host] * 2, 1.0), 30.0)

    def reward(self, host: str) -> None:
        d = self._delay[host]
        if d > self._base:
            self._delay[host] = max(self._base, d * 0.8)


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #
class RobotsCache:
    """Lazy, non-blocking robots.txt gate (fails open on error)."""

    def __init__(self, fetch, enabled: bool = True) -> None:
        self._fetch = fetch
        self.enabled = enabled
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def allowed(self, url: str, ua: str) -> bool:
        if not self.enabled:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._cache:
            async with self._locks[origin]:
                if origin not in self._cache:
                    self._cache[origin] = await self._load(origin)
        rp = self._cache[origin]
        if rp is None:
            return True
        with contextlib.suppress(Exception):
            return rp.can_fetch(ua, url)
        return True

    async def _load(self, origin: str):
        try:
            r = await self._fetch(f"{origin}/robots.txt", robots_check=False, retries=1)
            if not r.ok or len(r.text) > 512_000:
                return None
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(r.text.splitlines())
            return rp
        except Exception:
            return None


# --------------------------------------------------------------------------- #
# Fetcher
# --------------------------------------------------------------------------- #
class Fetcher:
    """High-throughput async HTTP client with everything a scraper needs.

    Features: HTTP/2, connection pooling, UA + proxy rotation, coherent client
    hints, adaptive per-host throttling, exponential backoff with jitter,
    transparent decompression, robots.txt, and an on-disk response cache.
    """

    RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524})

    def __init__(self, settings: Settings, stats: Stats | None = None) -> None:
        self.s = settings
        self.stats = stats or Stats()
        self.cache = ResponseCache(settings.cache_path, settings.cache_ttl, settings.cache)
        self.gov = HostGovernor(settings.per_host_concurrency, settings.delay)
        self.robots = RobotsCache(self.get, settings.respect_robots)
        self._global = asyncio.Semaphore(settings.concurrency)
        self._clients: list[httpx.AsyncClient] = []
        self._proxy_cycle = list(settings.proxies) or [None]  # type: ignore[list-item]
        self._rr = 0
        self._closed = False

    # -- lifecycle ------------------------------------------------------ #
    async def __aenter__(self) -> "Fetcher":
        limits = httpx.Limits(
            max_connections=self.s.concurrency * 2,
            max_keepalive_connections=self.s.concurrency,
            keepalive_expiry=30.0,
        )
        timeout = httpx.Timeout(
            self.s.timeout, connect=self.s.connect_timeout, read=self.s.timeout
        )
        for proxy in self._proxy_cycle:
            kwargs: dict[str, Any] = dict(
                limits=limits,
                timeout=timeout,
                follow_redirects=self.s.follow_redirects,
                verify=self.s.verify_ssl,
                max_redirects=5,
                trust_env=False,
            )
            if self.s.http2:
                kwargs["http2"] = True
            if proxy:
                kwargs["proxy"] = proxy
            try:
                self._clients.append(httpx.AsyncClient(**kwargs))
            except Exception as exc:  # pragma: no cover - env dependent
                kwargs.pop("http2", None)
                log.debug("http2 unavailable (%s); falling back to HTTP/1.1", exc)
                self._clients.append(httpx.AsyncClient(**kwargs))
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.gather(*(c.aclose() for c in self._clients), return_exceptions=True)
        self.cache.close()

    # -- internals ------------------------------------------------------ #
    def _client(self) -> httpx.AsyncClient:
        self._rr += 1
        return self._clients[self._rr % len(self._clients)]

    def _headers(self, url: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        ua = self.s.random_ua()
        h = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                      "image/webp,*/*;q=0.8",
            "Accept-Language": f"{self.s.language},en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
            "DNT": "1",
        }
        m = _CHROME_RE.search(ua)
        if m and m.group(1) in CLIENT_HINTS:
            h["Sec-CH-UA"] = CLIENT_HINTS[m.group(1)]
            h["Sec-CH-UA-Mobile"] = "?0"
            h["Sec-CH-UA-Platform"] = (
                '"Windows"' if "Windows" in ua else '"macOS"' if "Mac" in ua else '"Linux"'
            )
        if extra:
            h.update(extra)
        return h

    @staticmethod
    def _decode(r: httpx.Response) -> str:
        raw = r.content
        enc = (r.headers.get("content-encoding") or "").lower()
        if enc == "br" and raw[:1] not in (b"<", b"{", b"["):
            for mod in ("brotlicffi", "brotli"):
                with contextlib.suppress(Exception):
                    raw = __import__(mod).decompress(raw)
                    break
        elif enc == "deflate":
            with contextlib.suppress(Exception):
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        try:
            return raw.decode(r.encoding or "utf-8", "replace")
        except (LookupError, TypeError):
            return raw.decode("utf-8", "replace")

    # -- public API ------------------------------------------------------ #
    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        retries: int | None = None,
        robots_check: bool = True,
        use_cache: bool = True,
        method: str = "GET",
        data: dict[str, Any] | None = None,
    ) -> Response:
        """Fetch ``url`` with the full retry / throttle / cache pipeline."""
        if method != "GET":
            use_cache = False
        if use_cache and self.s.cache:
            hit = await self.cache.get(url)
            if hit is not None:
                self.stats.cache_hits += 1
                return hit

        host = host_of(url)
        max_tries = self.s.max_retries if retries is None else retries
        started = time.monotonic()
        last_err: str | None = None
        status = 0

        # SSRF guard: a scraped link must never reach the internal network or a
        # cloud metadata endpoint. Checked before every request, including the
        # ones discovered mid-crawl.
        if not getattr(self.s, "allow_private_networks", False):
            try:
                check_url(url, resolve=getattr(self.s, "resolve_dns_guard", True))
            except BlockedTarget as exc:
                log.debug("Blocked %s: %s", url, exc)
                return Response(url=url, status=0, text="",
                                error=f"blocked: {exc}")

        if robots_check and self.s.respect_robots:
            if not await self.robots.allowed(url, self.s.user_agents[0]):
                return Response(url=url, status=0, text="", error="blocked-by-robots")

        for attempt in range(1, max_tries + 1):
            async with self._global, self.gov.sem(host):
                await self.gov.wait(host, self.s.jitter)
                try:
                    self.stats.requests += 1
                    client = self._client()
                    if method == "GET":
                        r = await client.get(
                            url, headers=self._headers(url, headers), params=params
                        )
                    else:
                        r = await client.request(
                            method, url, headers=self._headers(url, headers),
                            params=params, data=data,
                        )
                    status = r.status_code
                    if status in self.RETRY_STATUS:
                        self.gov.penalise(host)
                        last_err = f"http-{status}"
                        raise _Retry(status)
                    if status >= 400:
                        self.stats.failures += 1
                        # Keep small JSON/text error bodies: APIs explain the
                        # real problem there ("Invalid API key", quota, ...).
                        detail = ""
                        ctype = r.headers.get("content-type", "")
                        if ("json" in ctype or "text" in ctype) and len(r.content) <= 8192:
                            with contextlib.suppress(Exception):
                                detail = self._decode(r)
                        return Response(
                            url=str(r.url), status=status, text=detail,
                            headers=dict(r.headers), error=f"http-{status}",
                            attempts=attempt, elapsed=time.monotonic() - started,
                        )
                    if len(r.content) > self.s.max_body_bytes:
                        return Response(
                            url=str(r.url), status=status, text="",
                            error="body-too-large", attempts=attempt,
                        )
                    # Note: httpx transparently decompresses, so the check above
                    # already measures expanded bytes and stops gzip bombs.
                    ctype = r.headers.get("content-type", "")
                    if ctype and not any(
                        t in ctype for t in ("html", "text", "json", "xml", "javascript")
                    ):
                        return Response(
                            url=str(r.url), status=status, text="",
                            error=f"content-type:{ctype.split(';')[0]}", attempts=attempt,
                        )
                    self.gov.reward(host)
                    self.stats.bytes_down += len(r.content)
                    resp = Response(
                        url=str(r.url), status=status, text=self._decode(r),
                        headers=dict(r.headers), elapsed=time.monotonic() - started,
                        attempts=attempt,
                    )
                    if use_cache:
                        await self.cache.put(resp)
                    return resp
                except _Retry:
                    pass
                except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError) as exc:
                    last_err = f"{type(exc).__name__}: {exc}"[:200]
                except Exception as exc:  # noqa: BLE001 - never kill the crawl
                    last_err = f"{type(exc).__name__}: {exc}"[:200]

            if attempt < max_tries:
                self.stats.retries += 1
                delay = min(self.s.backoff_base * 2 ** (attempt - 1), self.s.backoff_max)
                await asyncio.sleep(delay * (1 + random.uniform(0, self.s.jitter)))

        self.stats.failures += 1
        return Response(
            url=url, status=status, text="", error=last_err or "failed",
            attempts=max_tries, elapsed=time.monotonic() - started,
        )

    @staticmethod
    def _parse_json(text: str) -> Any:
        if not text:
            return None
        try:
            import orjson

            return orjson.loads(text)
        except Exception:
            import json

            with contextlib.suppress(Exception):
                return json.loads(text)
        return None

    async def get_json(self, url: str, **kw: Any) -> dict[str, Any] | None:
        """Fetch and decode JSON, or ``None`` on failure.

        Use :meth:`fetch_json` when you need the reason for the failure.
        """
        data, _ = await self.fetch_json(url, **kw)
        return data

    async def fetch_json(
        self, url: str, **kw: Any
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Return ``(payload, error)``.

        On an HTTP error the API's own message is extracted from the response
        body, so callers can tell "bad key" apart from "no results".

        ``robots_check`` defaults to False: these are authenticated calls to a
        vendor's documented API endpoint, not crawling. SerpApi, for instance,
        disallows ``/search.json`` in robots.txt to keep crawlers out, which
        would otherwise block the very API its customers pay to use.
        """
        kw.setdefault("robots_check", False)
        r = await self.get(url, use_cache=kw.pop("use_cache", False), **kw)
        parsed = self._parse_json(r.text)
        if r.ok:
            if parsed is None:
                return None, "invalid-json-response"
            return parsed, None
        detail = api_error_message(parsed) if parsed is not None else None
        if not detail:
            detail = (r.text or "").strip()[:200] or r.error or f"http-{r.status}"
        return None, f"HTTP {r.status}: {detail}" if r.status else detail

    async def gather(self, urls: Iterable[str], **kw: Any) -> list[Response]:
        """Fetch many URLs concurrently, preserving input order."""
        tasks = [asyncio.create_task(self.get(u, **kw)) for u in urls]
        return list(await asyncio.gather(*tasks))


class _Retry(Exception):
    """Internal signal: retryable status code."""
