"""Tests for sitemap/Wayback contact-page discovery (toolkit integration)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nestick.models import Response  # noqa: E402
from nestick.sitemap import (  # noqa: E402
    CDX_URL,
    SitemapIndex,
    WaybackCdx,
    bare_domain,
    is_public_domain,
)


def run(coro):
    return asyncio.run(coro)

ROBOTS_WITH_SITEMAP = "User-agent: *\nAllow: /\nSitemap: https://acme.com/sitemap.xml\n"
ROBOTS_PLAIN = "User-agent: *\nAllow: /\n"

SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://acme.com/pages.xml</loc></sitemap>
</sitemapindex>
"""

SITEMAP_URLS = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://acme.com/</loc></url>
  <url><loc>https://acme.com/contact</loc></url>
  <url><loc>https://acme.com/about</loc></url>
  <url><loc>https://acme.com/products</loc></url>
  <url><loc>https://acme.com/team</loc></url>
</urlset>
"""

CDX_JSON = [
    ["original"],
    ["https://acme.com/contact"],
    ["https://acme.com/products"],
    ["https://acme.com/about-us"],
    ["https://acme.com/privacy"],
]


class FakeFetcher:
    def __init__(self, routes=None, cdx=None):
        self.routes = routes or {}
        self.cdx = cdx or ([], None)
        self.calls: list[tuple] = []

    async def get(self, url, **kw):
        self.calls.append(("get", url))
        r = self.routes.get(url)
        if r is not None:
            return r
        return Response(url=url, status=404, text="")

    async def fetch_json(self, url, **kw):
        self.calls.append(("json", url))
        if url == CDX_URL:
            return self.cdx
        return (None, "unexpected endpoint")


def ok(url, text):
    return Response(url=url, status=200, text=text)


def test_bare_domain_and_public_guard():
    assert bare_domain("acme.com") == "acme.com"
    assert bare_domain("https://acme.com/x") == "acme.com"
    assert is_public_domain("acme.com") is True
    assert is_public_domain("acme.com.pk") is True
    assert is_public_domain("127.0.0.1") is False
    assert is_public_domain("localhost") is False
    assert is_public_domain("") is False


def test_sitemap_contact_urls_from_robots():
    f = FakeFetcher({
        "https://acme.com/robots.txt": ok("https://acme.com/robots.txt", ROBOTS_WITH_SITEMAP),
        "https://acme.com/sitemap.xml": ok("https://acme.com/sitemap.xml", SITEMAP_INDEX),
        "https://acme.com/pages.xml": ok("https://acme.com/pages.xml", SITEMAP_URLS),
    })
    urls = run(SitemapIndex(f).contact_urls("acme.com"))
    assert "https://acme.com/contact" in urls
    assert "https://acme.com/about" in urls
    assert "https://acme.com/team" in urls
    assert "https://acme.com/products" not in urls
    # Obvious guesses are appended even though the sitemap lists the real ones.
    assert "https://acme.com/contact-us" in urls
    assert "https://acme.com/about-us" in urls
    # Recursion into the child sitemap happened.
    assert ("get", "https://acme.com/pages.xml") in f.calls


def test_sitemap_fallback_guess_when_no_sitemap_listed():
    f = FakeFetcher({
        "https://acme.com/robots.txt": ok("https://acme.com/robots.txt", ROBOTS_PLAIN),
        "https://acme.com/sitemap.xml": Response(url="https://acme.com/sitemap.xml", status=404, text=""),
    })
    urls = run(SitemapIndex(f).contact_urls("acme.com"))
    assert urls == [
        "https://acme.com/contact",
        "https://acme.com/contact-us",
        "https://acme.com/about",
        "https://acme.com/about-us",
    ]


def test_sitemap_skips_non_public_domains():
    f = FakeFetcher()
    assert run(SitemapIndex(f).contact_urls("localhost")) == []
    assert ("get", "https://localhost/robots.txt") not in f.calls


def test_wayback_cdx_contact_urls():
    f = FakeFetcher(cdx=(CDX_JSON, None))
    urls = run(WaybackCdx(f).contact_urls("acme.com"))
    assert "https://acme.com/contact" in urls
    assert "https://acme.com/about-us" in urls
    assert "https://acme.com/privacy" in urls
    assert "https://acme.com/products" not in urls


def test_wayback_cdx_error_returns_empty():
    f = FakeFetcher(cdx=(None, "HTTP 503: retry later"))
    assert run(WaybackCdx(f).contact_urls("acme.com")) == []


def test_wayback_cdx_skips_non_public_domains():
    f = FakeFetcher()
    assert run(WaybackCdx(f).contact_urls("127.0.0.1")) == []
    assert f.calls == []
