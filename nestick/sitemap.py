"""Supplemental page discovery: robots.txt → sitemap → contact pages.

Port of the leadgen toolkit's ``sitemap_crawler`` and its Wayback Machine CDX
trick, rebuilt on the engine's own Fetcher so sitemap fetches inherit the
throttle, retry, robots and SSRF-guard pipeline instead of using a parallel
``urllib`` stack.

Why this exists: sites routinely hide their contact page from the homepage
graph but still publish it in ``robots.txt``-referenced sitemaps. When a site
has no sitemap (or none that parses), the Wayback Machine's CDX index still
lists the domain's archived pages — including contact/about URLs that survive
even after the live site goes JavaScript-heavy.
"""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from .utils import log

#: URL substrings that mark a sitemap entry as contact-bearing. Mirror of the
#: toolkit's keyword set, widened with the legal pages the extractor values.
CONTACT_KEYWORDS: tuple[str, ...] = (
    "contact", "about", "team", "support", "impressum", "imprint",
    "get-in-touch", "reach-us", "legal", "privacy", "locations",
    "office", "staff", "people", "careers",
)

#: Obvious contact paths tried even when the sitemap does not list one.
GUESS_PATHS: tuple[str, ...] = ("contact", "contact-us", "about", "about-us")

SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
SITEMAP_INDEX_NS = "http://www.sitemaps.org/schemas/sitemap-index/0.9"

#: Wayback Machine CDX index (JSON flavour).
CDX_URL = "https://web.archive.org/cdx/search/cdx"

#: Bounds so a pathological sitemap cannot stall a run.
MAX_SITEMAPS = 25
MAX_URLS = 2000
MAX_DEPTH = 3


def bare_domain(domain: str) -> str:
    """Tolerate full URLs or bare domains, like the toolkit does."""
    if not domain:
        return ""
    with_scheme = domain if "://" in domain else f"https://{domain}"
    return urlsplit(with_scheme).netloc or domain


def is_public_domain(domain: str) -> bool:
    """False for IPs, localhost and empty hosts — none can have a sitemap."""
    domain = bare_domain(domain)
    if not domain:
        return False
    host = domain.split(":", 1)[0].lower()
    if host in ("localhost",) or host.endswith(".local"):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return "." in host
    return False


class SitemapIndex:
    """Resolve ``domain/robots.txt`` → sitemap(s) → page URLs."""

    def __init__(self, fetcher: Any, max_depth: int = MAX_DEPTH,
                 max_sitemaps: int = MAX_SITEMAPS, max_urls: int = MAX_URLS) -> None:
        self.f = fetcher
        self.max_depth = max_depth
        self.max_sitemaps = max_sitemaps
        self.max_urls = max_urls

    async def sitemap_urls(self, domain: str) -> list[str]:
        """Sitemaps advertised in robots.txt, else the common location."""
        url = f"https://{domain}/robots.txt"
        r = await self.f.get(url, robots_check=False, retries=1)
        if r.ok:
            listed = [
                line.split(":", 1)[1].strip()
                for line in r.text.splitlines()
                if line.lower().startswith("sitemap:")
            ]
            if listed:
                return list(dict.fromkeys(listed))[: self.max_sitemaps]
        return [f"https://{domain}/sitemap.xml"]

    async def page_urls(self, sitemap_url: str, depth: int = 0) -> list[str]:
        """Recursively resolve a sitemap (or index) into page URLs."""
        if depth > self.max_depth:
            return []
        r = await self.f.get(sitemap_url, robots_check=False, retries=1)
        if not r.ok or not r.text or len(r.text) > 20_000_000:
            return []
        try:
            root = ET.fromstring(r.text)
        except ET.ParseError:
            log.debug("Malformed sitemap %s", sitemap_url)
            return []

        urls: list[str] = []
        for tag, is_index in (
            ("sitemap", True),  # child sitemaps of an index
            ("url", False),     # leaf page entries
        ):
            # Both ``urlset`` and sitemap-index roots use ``<loc>``; try the
            # sitemaps.org namespace, then the sitemap-index one.
            for ns in (SITEMAP_NS, SITEMAP_INDEX_NS):
                for loc in root.findall(f"{{{ns}}}{tag}/{{{ns}}}loc"):
                    u = (loc.text or "").strip()
                    if not u:
                        continue
                    if is_index:
                        urls.extend(await self.page_urls(u, depth + 1))
                    else:
                        urls.append(u)
                    if len(urls) >= self.max_urls:
                        return urls
        return urls

    async def contact_urls(self, domain: str) -> list[str]:
        """Contact-ish URLs from the sitemap, plus the obvious guesses."""
        domain = bare_domain(domain)
        if not is_public_domain(domain):
            return []
        pages: list[str] = []
        for sm in await self.sitemap_urls(domain):
            pages.extend(await self.page_urls(sm))
        out = [u for u in pages if _is_contact(u)]
        out += [f"https://{domain}/{g}" for g in GUESS_PATHS]
        return list(dict.fromkeys(out))


class WaybackCdx:
    """Query the Wayback Machine CDX index for a domain's archived pages.

    A fallback when the live site exposes no useful sitemap: the archive still
    knows the domain's contact/about URLs. The live URLs are returned so the
    normal crawl pipeline (robots, throttling, SSRF guard) fetches them.
    """

    def __init__(self, fetcher: Any, limit: int = 500) -> None:
        self.f = fetcher
        self.limit = limit

    async def contact_urls(self, domain: str, limit: int = 40) -> list[str]:
        domain = bare_domain(domain)
        if not is_public_domain(domain):
            return []
        data, err = await self.f.fetch_json(
            CDX_URL,
            params={
                "url": f"{domain}/*",
                "output": "json",
                "fl": "original",
                "collapse": "urlkey",
                "filter": "statuscode:200",
                "limit": self.limit,
                "fastLatest": "true",
            },
            robots_check=False, retries=1,
        )
        if err or not isinstance(data, list) or len(data) < 2:
            if err:
                log.debug("Wayback CDX failed for %s: %s", domain, err)
            return []
        out: list[str] = []
        for row in data[1:]:
            u = (row[0] if isinstance(row, list) and row else "") or ""
            u = u.strip()
            if u.startswith("http") and _is_contact(u):
                out.append(u)
            if len(out) >= limit:
                break
        return out


def _is_contact(url: str) -> bool:
    low = url.lower()
    return any(kw in low for kw in CONTACT_KEYWORDS)
