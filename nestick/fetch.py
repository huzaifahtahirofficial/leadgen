"""``nestick fetch`` — fetch a single page and dump it in useful formats.

Zero-dependency page inspection built on the existing async fetcher:

.. code-block:: bash

    nestick fetch https://example.com --dump html
    nestick fetch https://example.com --dump text
    nestick fetch https://example.com --dump markdown
    nestick fetch https://example.com --dump links
    nestick fetch https://example.com --dump assets
    nestick fetch https://example.com --dump original
    nestick fetch https://example.com --dump cookies

Because Nestick is a pure-HTTP engine (no browser / JavaScript runtime),
``--dump html`` and ``--dump original`` return the same bytes for static
pages, and asset discovery covers what the HTML *declares* (stylesheets,
scripts, images, fonts, iframes) — URLs only requested at runtime by JS
cannot be seen without a headless browser.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import sys
import time
from html.parser import HTMLParser
from http.cookies import SimpleCookie
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

from .config import Settings
from .http import Fetcher
from .models import Response
from .utils import log, normalise_url, setup_logging

DUMP_FORMATS = ("html", "text", "markdown", "links", "assets", "original", "cookies")

_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "aside", "main",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li", "tr", "td", "th",
    "table", "figure", "figcaption", "address", "dt", "dd",
}
_SKIP_TAGS = {"script", "style", "noscript", "head", "svg", "template", "iframe"}
_WINDOW_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


# --------------------------------------------------------------------------- #
# Text + markdown extraction
# --------------------------------------------------------------------------- #
class _MarkdownParser(HTMLParser):
    """Converts a document to plain text or Markdown in a single pass."""

    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.out: list[str] = []
        self.skip = 0
        self.pre_depth = 0
        self.fenced = False
        self.in_list: list[bool] = []      # ordered flag per nesting level
        self.li_index: list[int] = []
        self.link_hrefs: list[str] = []
        self._pending_text = False

    # -- helpers -------------------------------------------------------- #
    def _emit(self, s: str) -> None:
        self.out.append(s)

    def _blank(self) -> None:
        if self.out and self.out[-1].rstrip():
            self._emit("\n")

    def _push_text(self) -> None:
        # Separate adjacent text from an inline marker that needs a space.
        if self._pending_text:
            self._emit(" ")
            self._pending_text = False

    def _block(self) -> None:
        # A block boundary ends any pending inline text run.
        self._pending_text = False
        self._blank()

    def _resolve(self, url: str) -> str:
        if self.base:
            return normalise_url(urljoin(self.base, url)) or url
        return url

    # -- events ---------------------------------------------------------- #
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag in _SKIP_TAGS:
            self.skip += 1
        if self.skip:
            return
        if tag == "pre":
            if self.pre_depth == 0 and not self.fenced:
                self._block()
                self._emit("```\n")
                self.fenced = True
            self.pre_depth += 1
            return
        if tag == "code":
            if self.pre_depth:
                return  # already inside a fenced block
            self._push_text()
            self._emit("`")
            return
        if tag == "br":
            self._emit("\n")
            return
        if tag == "hr":
            self._block()
            self._emit("---\n")
            return
        if tag in _WINDOW_HEADINGS:
            self._block()
            self._emit("#" * int(tag[1]) + " ")
            return
        if tag in ("ul", "ol"):
            self.in_list.append(tag == "ol")
            self.li_index.append(0)
            self._block()
            return
        if tag == "li":
            depth = max(0, len(self.in_list) - 1)
            ordered = self.in_list[-1] if self.in_list else False
            self.li_index[-1] += 1
            marker = f"{self.li_index[-1]}." if ordered else "-"
            self._block()
            self._emit("  " * depth + marker + " ")
            return
        if tag == "blockquote":
            self._block()
            self._emit("> ")
            return
        if tag == "img":
            alt = a.get("alt", "")
            src = self._resolve(a.get("src", ""))
            self._push_text()
            self._emit(f"![{alt}]({src})")
            return
        if tag == "a":
            self._push_text()
            self.link_hrefs.append(self._resolve(a.get("href", "")))
            self._emit("[")
            return
        if tag in ("strong", "b"):
            self._push_text()
            self._emit("**")
            return
        if tag in ("em", "i"):
            self._push_text()
            self._emit("*")
            return
        if tag in ("kbd", "samp"):
            self._push_text()
            self._emit("`")
            return
        if tag in ("del", "s"):
            self._push_text()
            self._emit("~~")
            return
        if tag in _BLOCK_TAGS or tag in ("ul", "ol", "table", "thead", "tbody", "tfoot"):
            self._block()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "pre":
            self.pre_depth = max(0, self.pre_depth - 1)
            if self.pre_depth == 0 and self.fenced:
                self._emit("\n```\n")
                self.fenced = False
            return
        if tag == "code":
            if self.pre_depth:
                return
            self._emit("`")
            return
        if tag in _WINDOW_HEADINGS:
            self._emit("\n")
            return
        if tag in ("ul", "ol"):
            if self.in_list:
                self.in_list.pop()
            if self.li_index:
                self.li_index.pop()
            self._block()
            return
        if tag == "li":
            self._emit("\n")
            return
        if tag == "blockquote":
            self._emit("\n")
            return
        if tag == "a":
            href = self.link_hrefs.pop() if self.link_hrefs else ""
            self._emit(f"]({href})")
            self._pending_text = True
            return
        if tag in ("strong", "b"):
            self._emit("**")
            return
        if tag in ("em", "i"):
            self._emit("*")
            return
        if tag in ("kbd", "samp"):
            self._emit("`")
            return
        if tag in ("del", "s"):
            self._emit("~~")
            return
        if tag in _BLOCK_TAGS or tag in ("table", "thead", "tbody", "tfoot"):
            self._block()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.pre_depth:
            self._emit(data)
            return
        # Collapse runs of whitespace; a single space is enough.
        text = re.sub(r"\s+", " ", data).strip()
        if text:
            if self._pending_text and text[0] not in ".,;:!?)]}":
                self._emit(" ")
            self._emit(text)
            self._pending_text = True

    # -- result ---------------------------------------------------------- #
    def result(self) -> str:
        text = "".join(self.out)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"(?m)^ {1,3}([-*]|\d+\.) ", r"\1 ", text)
        text = re.sub(r"```\n\n", "```\n", text)
        return text.strip() + "\n"


# --------------------------------------------------------------------------- #
# Link + asset discovery
# --------------------------------------------------------------------------- #
class _ResourceParser(HTMLParser):
    """Collects <a href> and declared external resources."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.links: list[str] = []
        self.assets: list[dict[str, str]] = []

    def _resolve(self, url: str) -> str | None:
        if not url or url.startswith(("javascript:", "#", "data:")):
            return None
        return normalise_url(urljoin(self.base, url)) or url

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            if u := self._resolve(a["href"]):
                self.links.append(u)
        elif tag == "link":
            rel = (a.get("rel") or "").lower().split()
            href = a.get("href")
            if href and "stylesheet" in rel and (u := self._resolve(href)):
                self.assets.append({"kind": "stylesheet", "url": u})
            elif (href and "preload" in rel and a.get("as") == "font"
                  and (u := self._resolve(href))):
                self.assets.append({"kind": "font", "url": u})
        elif tag == "script" and a.get("src"):
            if u := self._resolve(a["src"]):
                self.assets.append({"kind": "script", "url": u})
        elif tag == "img" and a.get("src"):
            if u := self._resolve(a["src"]):
                self.assets.append({"kind": "image", "url": u})
        elif tag == "source" and a.get("srcset"):
            srcset = a["srcset"].split(",")[0].split()[0]
            if u := self._resolve(srcset):
                self.assets.append({"kind": "image", "url": u})
        elif tag == "iframe" and a.get("src"):
            if u := self._resolve(a["src"]):
                self.assets.append({"kind": "iframe", "url": u})


def text(html: str) -> str:
    """Plain text rendering: no markup, headings/paragraphs separated."""
    # Reuse the markdown walker, then strip its formatting markers.
    md = markdown(html)
    md = re.sub(r"#{1,6} ", "", md)
    md = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", md)
    md = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", md)
    md = re.sub(r"[*_~`>]+", "", md)
    md = re.sub(r"(?m)^\d+\. ", "- ", md)
    return re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"


def markdown(html: str, base_url: str = "") -> str:
    """HTML to Markdown conversion: headings, lists, links, code, images."""
    p = _MarkdownParser(base_url)
    p.feed(html or "")
    p.close()
    return p.result()


def links(html: str, base_url: str = "") -> list[str]:
    """Every <a href> on the page, resolved and deduplicated."""
    p = _ResourceParser(base_url)
    p.feed(html or "")
    p.close()
    seen: set[str] = set()
    out: list[str] = []
    for u in p.links:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def assets(html: str, base_url: str = "") -> list[dict[str, str]]:
    """Declared external resources: stylesheets, scripts, images, fonts, iframes."""
    p = _ResourceParser(base_url)
    p.feed(html or "")
    p.close()
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for a in p.assets:
        key = (a["kind"], a["url"])
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out


def cookies(response: Response) -> list[dict[str, Any]]:
    """Cookies the server set, parsed from ``Set-Cookie`` headers.

    Includes ``HttpOnly`` cookies that ``document.cookie`` cannot see. The
    fetcher collapses multi-valued headers, so a response that sets several
    cookies at once will only expose the last one.
    """
    header = response.headers.get("set-cookie") or ""
    jar = SimpleCookie()
    jar.load(header)
    out: list[dict[str, Any]] = []
    for morsel in jar.values():
        attrs: dict[str, Any] = {
            "name": morsel.key,
            "value": morsel.value,
            "domain": morsel.get("domain"),
            "path": morsel.get("path"),
            "secure": bool(morsel.get("secure")),
            "httponly": bool(morsel.get("httponly")),
            "expires": morsel.get("expires"),
            "samesite": morsel.get("samesite"),
        }
        out.append({k: v for k, v in attrs.items() if v not in (None, "") or k in ("name", "value")})
    return out


# --------------------------------------------------------------------------- #
# CLI entry point
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nestick fetch",
        description="Fetch a single URL and dump it as HTML, text, markdown, "
                    "links, assets, or cookies.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  nestick fetch https://news.ycombinator.com --dump text
  nestick fetch https://docs.example.com/page --dump markdown > page.md
  nestick fetch https://example.com --dump links
  nestick fetch https://my-spa.example --dump original > before.html
  nestick fetch https://example.com --dump cookies
  nestick fetch https://example.com --dump text --quiet | wc -w""",
    )
    p.add_argument("url", help="URL to fetch")
    p.add_argument("--dump", choices=DUMP_FORMATS, default="html",
                   help="output format (default: html)")
    p.add_argument("--wait-until", choices=("load", "networkidle", "idle"),
                   default="load",
                   help="wait condition before dumping (default: load)")
    p.add_argument("--wait", type=float, default=0.0,
                   help="additional seconds to wait before dumping")
    p.add_argument("--timeout", type=float, default=15.0, help="request timeout (default: 15)")
    p.add_argument("--retries", type=int, default=3, help="max attempts (default: 3)")
    p.add_argument("--max-bytes", type=int, default=3_000_000,
                   help="cap on response size (default: 3 MB)")
    p.add_argument("--respect-robots", action="store_true",
                   help="consult robots.txt before fetching (default: off)")
    p.add_argument("--no-cache", action="store_true",
                   help="bypass the on-disk response cache")
    p.add_argument("--no-http2", action="store_true", help="disable HTTP/2")
    p.add_argument("--allow-private", action="store_true",
                   help="permit localhost/private addresses (default: off)")
    p.add_argument("--proxy", action="append", dest="proxies", default=[],
                   help="proxy URL (repeatable)")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="suppress logs; dump goes to stdout only")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _emit(text: str) -> None:
    sys.stdout.buffer.write(text.encode("utf-8", "replace"))


async def _fetch(argv: list[str]) -> int:
    a = build_parser().parse_args(argv)
    setup_logging(a.verbose, a.quiet)

    settings = Settings(
        queries=[],
        urls=[a.url],
        cache=not a.no_cache,
        respect_robots=a.respect_robots,
        allow_private_networks=a.allow_private,
        timeout=a.timeout,
        max_retries=a.retries,
        max_body_bytes=a.max_bytes,
        http2=not a.no_http2,
        proxies=list(a.proxies),
        verbose=a.verbose,
        quiet=a.quiet,
    )

    # Cookies are never served from cache: a cached body has no headers.
    use_cache = (not a.no_cache) and a.dump != "cookies"
    async with Fetcher(settings) as fetcher:
        resp = await fetcher.get(a.url, robots_check=a.respect_robots,
                                 use_cache=use_cache)

    if resp.error or not resp.text:
        log.error("Fetch failed for %s: %s", a.url, resp.error or "empty body")
        if resp.status:
            log.error("HTTP %d", resp.status)
        return 1

    if a.wait:
        await asyncio.sleep(a.wait)
    elif a.wait_until in ("networkidle", "idle"):
        await asyncio.sleep(0.5)  # no JS engine — emulate a quiet period

    base = resp.url or a.url
    if a.dump in ("html", "original"):
        _emit(resp.text)
    elif a.dump == "text":
        _emit(text(resp.text))
    elif a.dump == "markdown":
        _emit(markdown(resp.text, base))
    elif a.dump == "links":
        _emit("\n".join(links(resp.text, base)) + "\n")
    elif a.dump == "assets":
        for item in assets(resp.text, base):
            _emit(json.dumps(item) + "\n")
    elif a.dump == "cookies":
        _emit(json.dumps(cookies(resp), indent=2) + "\n")

    if not a.quiet:
        log.info("Fetched %s (%d bytes, %d ms, %s)",
                 base, len(resp.text), int(resp.elapsed * 1000),
                 "cache" if resp.from_cache else f"HTTP {resp.status}")
    return 0


def fetch_main(argv: list[str] | None = None) -> int:
    """Entry point for ``nestick fetch``, mirroring cli.main()'s conventions."""
    argv = list(sys.argv[2:] if argv is None else argv)
    started = time.monotonic()
    try:
        return asyncio.run(_fetch(argv))
    except KeyboardInterrupt:
        log.warning("Interrupted after %.1fs", time.monotonic() - started)
        return 130
    except Exception as exc:  # noqa: BLE001
        log.error("Fatal: %s", exc, exc_info="-v" in argv)
        return 1


if __name__ == "__main__":
    raise SystemExit(fetch_main())
