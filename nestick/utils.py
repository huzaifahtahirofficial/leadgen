"""Shared helpers: logging, URL normalisation, text tools."""

from __future__ import annotations

import html
import logging
import re
import unicodedata
from typing import Iterable, Iterator, TypeVar
from urllib.parse import urljoin, urlsplit, urlunsplit

log = logging.getLogger("nestick")

T = TypeVar("T")

_WS_RE = re.compile(r"\s+")
_TRACKING = re.compile(
    r"^(utm_|fbclid|gclid|msclkid|mc_cid|mc_eid|ref|source|igshid|_ga|yclid|"
    r"pk_campaign|pk_kwd|s_kwcid|dclid|twclid|scid)",
    re.I,
)


def setup_logging(verbose: bool = False, quiet: bool = False, log_file: str | None = None) -> None:
    """Configure the package logger, preferring Rich when available."""
    level = logging.DEBUG if verbose else logging.ERROR if quiet else logging.INFO
    log.setLevel(level)
    log.handlers.clear()
    log.propagate = False
    try:
        from rich.logging import RichHandler

        h: logging.Handler = RichHandler(
            rich_tracebacks=True, show_path=False, markup=True, show_time=not quiet
        )
        h.setFormatter(logging.Formatter("%(message)s", datefmt="%H:%M:%S"))
    except Exception:  # pragma: no cover
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    h.setLevel(level)
    log.addHandler(h)
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))
        log.addHandler(fh)


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def registrable_domain(url_or_host: str) -> str:
    """Best-effort eTLD+1 without external dependencies.

    Handles the common multi-part public suffixes (``co.uk``, ``com.pk``...).
    """
    host = host_of(url_or_host) if "//" in url_or_host else url_or_host.lower().strip()
    host = host.split("@")[-1].split(":")[0].strip(".")
    if not host or host.replace(".", "").isdigit():
        return host
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    second_level = {
        "co", "com", "net", "org", "gov", "edu", "ac", "mil", "or", "ne", "go",
        "in", "web", "biz", "info", "sch", "gob", "gouv", "nom", "id",
    }
    if parts[-2] in second_level and len(parts[-1]) <= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def normalise_url(url: str, base: str | None = None) -> str | None:
    """Absolutise, strip fragments/tracking params, and canonicalise."""
    if not url:
        return None
    url = html.unescape(url.strip())
    if url.startswith(("javascript:", "mailto:", "tel:", "data:", "#", "sms:")):
        return None
    if base:
        url = urljoin(base, url)
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    netloc = p.netloc.lower()
    if netloc.endswith(":80") and p.scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and p.scheme == "https":
        netloc = netloc[:-4]
    query = "&".join(
        q for q in p.query.split("&") if q and not _TRACKING.match(q.split("=")[0])
    )
    path = re.sub(r"/{2,}", "/", p.path) or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((p.scheme, netloc, path, query, ""))


def same_site(a: str, b: str) -> bool:
    return registrable_domain(a) == registrable_domain(b)


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
def clean_text(s: str | None, limit: int = 500) -> str | None:
    if not s:
        return None
    s = unicodedata.normalize("NFKC", html.unescape(s))
    s = _WS_RE.sub(" ", s).strip()
    return s[:limit] or None


def dedupe(items: Iterable[T]) -> Iterator[T]:
    """Order-preserving deduplication."""
    seen: set[T] = set()
    for it in items:
        if it not in seen:
            seen.add(it)
            yield it


def chunked(items: list[T], size: int) -> Iterator[list[T]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def human(n: float) -> str:
    for unit in ("", "K", "M", "B"):
        if abs(n) < 1000:
            return f"{n:.0f}{unit}" if unit else f"{n:.0f}"
        n /= 1000
    return f"{n:.1f}T"
