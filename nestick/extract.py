"""Extraction engine: e-mails, phones, socials, metadata and crawl targets.

Merges and hardens the regex sets from the Ruby scraper (``clauneck``), the
Puppeteer email harvester (``script.js``) and the Electron lead app (``app.js``),
then adds deobfuscation, JSON-LD parsing and confidence scoring.
"""

from __future__ import annotations

import html
import re
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from .config import (
    CONTACT_HINTS,
    EMAIL_BLOCKLIST,
    EMAIL_LOCAL_BLOCKLIST,
    ROLE_LOCALS,
    SKIP_DOMAINS,
    SKIP_EXTENSIONS,
)
from .models import Contact, ContactKind
from .utils import clean_text, normalise_url, registrable_domain, same_site

# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #
EMAIL_RE = re.compile(
    r"(?<![\w.+-])"
    r"([A-Za-z0-9](?:[A-Za-z0-9._%+-]{0,62}[A-Za-z0-9])?)"
    r"@"
    r"((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24})"
    r"(?![\w-])"
)

#: name (at) domain (dot) com  /  name[at]domain[dot]com  /  name AT domain DOT com
#:
#: The textual forms ("at", "dot") MUST be delimited by a bracket or whitespace.
#: Without that guard the pattern fires inside ordinary words — "ycombinator.com"
#: would be read as "ycombin (at) or.com".
#: Delimited "at": brackets, or a spaced @. Unambiguous — safe with plain dots.
_AT_MARK = r"(?:\s*(?:\(|\[|\{)\s*at\s*(?:\)|\]|\})\s*|\s*@\s*)"
#: Bare word "at". Ambiguous with prose ("available at example.com"), so it is
#: only honoured when the domain's dots are *also* written out as words.
_AT_WORD = r"\s+(?:at|arroba|chiocciola)\s+"
#: A literal dot inside a domain is never spaced — allowing whitespace here
#: would let "…@postgresql.org. In addition" absorb the next word as a label.
_DOT_MARK = r"(?:\s*(?:\(|\[|\{)\s*(?:dot|punkt|nokta|punto)\s*(?:\)|\]|\})\s*|\.)"
_DOT_WORD = (r"(?:\s*(?:\(|\[|\{)\s*(?:dot|punkt|nokta|punto)\s*(?:\)|\]|\})\s*"
             r"|\s+(?:dot|punkt|nokta|punto)\s+)")
_LOCAL = r"([A-Za-z0-9._%+-]{1,64}?)"
_TLD = r"[A-Za-z]{2,24}\b"

OBFUSCATED_PATTERNS: tuple[re.Pattern[str], ...] = (
    # john (at) acme.com   |   john [at] acme (dot) com   |   john @ acme.com
    re.compile(_LOCAL + _AT_MARK + r"((?:[A-Za-z0-9-]{1,63}" + _DOT_MARK + r")+" + _TLD + r")", re.I),
    # bob AT widgets DOT io   (fully written out — both markers must be words)
    re.compile(_LOCAL + _AT_WORD + r"((?:[A-Za-z0-9-]{1,63}" + _DOT_WORD + r")+" + _TLD + r")", re.I),
)

CF_EMAIL_RE = re.compile(r'data-cfemail="([0-9a-fA-F]+)"')
MAILTO_RE = re.compile(r'(?:href|content)\s*=\s*["\']\s*mailto:([^"\'?>]+)', re.I)
TEL_RE = re.compile(r'href\s*=\s*["\']\s*tel:([+0-9().\-\s]{6,})["\']', re.I)

PHONE_RE = re.compile(
    r"(?<![\w.])(?:\+|00)?\d{1,3}[\s.\-]?"
    r"(?:\(\d{1,4}\)[\s.\-]?)?"
    r"\d{2,4}(?:[\s.\-]?\d{2,4}){1,4}"
    r"(?:\s*(?:ext|x|extn|poste)\.?\s*\d{1,5})?(?![\w.])",
    re.I,
)

SOCIAL_PATTERNS: tuple[tuple[ContactKind, re.Pattern[str]], ...] = (
    (ContactKind.LINKEDIN, re.compile(
        r"https?://(?:[a-z0-9-]+\.)?linkedin\.com/((?:in|company|school|pub)/[A-Za-z0-9_%\-.]{2,100})", re.I)),
    (ContactKind.TWITTER, re.compile(
        r"https?://(?:www\.|mobile\.)?(?:twitter|x)\.com/(?!(?:share|intent|home|search|hashtag|i/)\b)([A-Za-z0-9_]{1,15})", re.I)),
    (ContactKind.FACEBOOK, re.compile(
        r"https?://(?:www\.|web\.|m\.|business\.)?facebook\.com/(?!(?:sharer|share|tr|plugins|dialog|events/create)\b)([A-Za-z0-9_.\-]{2,80})", re.I)),
    (ContactKind.INSTAGRAM, re.compile(
        r"https?://(?:www\.)?instagram\.com/(?!(?:p|reel|reels|explore|stories|accounts)/)([A-Za-z0-9_.]{1,30})", re.I)),
    (ContactKind.TIKTOK, re.compile(
        r"https?://(?:www\.|m\.)?tiktok\.com/(@[A-Za-z0-9_.]{1,30})", re.I)),
    (ContactKind.YOUTUBE, re.compile(
        r"https?://(?:www\.|m\.)?youtube\.com/(channel/[A-Za-z0-9_\-]{6,}|c/[A-Za-z0-9_\-]{2,}|@[A-Za-z0-9_.\-]{2,}|user/[A-Za-z0-9_\-]{2,})", re.I)),
    (ContactKind.GITHUB, re.compile(
        r"https?://(?:www\.)?github\.com/(?!(?:features|about|pricing|topics|collections|events|sponsors)\b)([A-Za-z0-9_\-]{1,39})(?:/([A-Za-z0-9_.\-]{1,100}))?", re.I)),
    (ContactKind.MEDIUM, re.compile(
        r"https?://(?:www\.)?medium\.com/(@?[A-Za-z0-9_\-]{2,60})", re.I)),
    (ContactKind.TELEGRAM, re.compile(
        r"https?://(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,32})", re.I)),
    (ContactKind.WHATSAPP, re.compile(
        r"https?://(?:api\.whatsapp\.com/send\?phone=|wa\.me/)(\+?\d{6,15})", re.I)),
)

#: Bare URLs / long digit-ids left in visible text — stripped before phone parsing.
URL_IN_TEXT_RE = re.compile(r"\bhttps?://\S+|\bwww\.\S+|\b\S+\.(?:com|org|net|io)/\S*", re.I)

HREF_RE = re.compile(r'<a\b[^>]*?href\s*=\s*["\']([^"\'>]+)["\']', re.I | re.S)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
META_DESC_RE = re.compile(
    r'<meta[^>]+(?:name|property)\s*=\s*["\'](?:description|og:description)["\'][^>]*'
    r'content\s*=\s*["\']([^"\']*)["\']', re.I)
META_SITE_RE = re.compile(
    r'<meta[^>]+property\s*=\s*["\']og:site_name["\'][^>]*content\s*=\s*["\']([^"\']*)["\']', re.I)
JSONLD_RE = re.compile(
    r'<script[^>]+type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)
SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript|svg)\b[^>]*>.*?</\1>", re.I | re.S)

#: State blobs used by SPA frameworks. On JS-rendered sites the human-readable
#: content never appears in the markup — it lives here as escaped JSON.
STATE_BLOB_RE = re.compile(
    r"<script[^>]*>\s*(?:self\.__next_f\.push\(|window\.__NUXT__\s*=|"
    r"window\.__INITIAL_STATE__\s*=|window\.__APOLLO_STATE__\s*=|"
    r"window\.__PRELOADED_STATE__\s*=|window\.__remixContext\s*=)(.*?)</script>",
    re.I | re.S,
)
#: <script id="__NEXT_DATA__" type="application/json">…</script> and friends.
JSON_SCRIPT_RE = re.compile(
    r'<script[^>]+type\s*=\s*["\']application/json["\'][^>]*>(.*?)</script>', re.I | re.S
)
#: \u0040 / \x40 escaped "@" plus HTML entities, as emitted inside JSON strings.
UNICODE_AT_RE = re.compile(r"\\u0040|\\x40|&#0*64;|&commat;", re.I)
UNICODE_DOT_RE = re.compile(r"\\u002e|\\x2e|&#0*46;|&period;", re.I)
TAG_RE = re.compile(r"<[^>]+>")
DOT_WORDS = re.compile(r"\s*(?:\(|\[|\{)?\s*(?:dot|punkt|nokta|punto)\s*(?:\)|\]|\})?\s*", re.I)

IMAGE_TAIL_RE = re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|bmp|tiff?|avif)$", re.I)
VERSION_TAIL_RE = re.compile(r"^(?:\d+\.){1,}\d+$")
HEX_LOCAL_RE = re.compile(r"^[0-9a-f]{16,}$", re.I)

#: Hosting, CDN, analytics and agency domains that every page links to.
_INFRA_DOMAINS: frozenset[str] = frozenset({
    "grafdom.com", "wix.com", "wordpress.com", "squarespace.com", "godaddy.com",
    "cloudflare.com", "jsdelivr.net", "unpkg.com", "bootstrapcdn.com",
    "fontawesome.com", "googleapis.com", "gstatic.com", "google-analytics.com",
    "googletagmanager.com", "doubleclick.net", "hotjar.com", "sentry.io",
    "recaptcha.net", "adobe.com", "microsoft.com", "apple.com", "mozilla.org",
    "w3.org", "schema.org", "creativecommons.org", "gravatar.com",
    "shopify.com", "hubspot.com", "mailchimp.com", "typeform.com",
})

_TLD_OK = re.compile(r"^[A-Za-z]{2,24}$")
_BAD_TLD = frozenset({"png", "jpg", "jpeg", "gif", "webp", "svg", "js", "css",
                      "json", "html", "php", "aspx", "min", "map", "woff", "ttf",
                      "mp4", "webp2", "ico", "xml", "txt", "zip", "gz"})


# --------------------------------------------------------------------------- #
class Extractor:
    """Stateless HTML → :class:`Contact` engine (safe to share across tasks)."""

    def __init__(self, settings: Any | None = None) -> None:
        self.s = settings
        self.deobfuscate = getattr(settings, "deobfuscate", True)

    # ------------------------------------------------------------------ #
    # E-mail
    # ------------------------------------------------------------------ #
    @staticmethod
    def _cf_decode(token: str) -> str | None:
        """Decode Cloudflare's ``data-cfemail`` XOR obfuscation."""
        try:
            key = int(token[:2], 16)
            return "".join(
                chr(int(token[i : i + 2], 16) ^ key) for i in range(2, len(token), 2)
            )
        except Exception:
            return None

    def valid_email(self, email: str) -> bool:
        """Reject images, versions, placeholders, vendor noise and junk TLDs."""
        if not email or email.count("@") != 1 or len(email) > 254:
            return False
        local, _, domain = email.partition("@")
        if not local or not domain or ".." in email or email.startswith("."):
            return False
        if local[-1] == "." or domain[0] == "-" or domain[-1] in ".-":
            return False
        if IMAGE_TAIL_RE.search(domain) or VERSION_TAIL_RE.match(domain):
            return False
        if HEX_LOCAL_RE.match(local) and len(local) > 24:
            return False
        tld = domain.rsplit(".", 1)[-1].lower()
        if tld in _BAD_TLD or not _TLD_OK.match(tld):
            return False
        if domain.lower() in EMAIL_BLOCKLIST or registrable_domain(domain) in EMAIL_BLOCKLIST:
            return False
        if local.lower() in EMAIL_LOCAL_BLOCKLIST:
            return False
        if any(x in email.lower() for x in ("@2x", "@3x", "sentry.io", "wixpress")):
            return False
        # u0040-style or entity leftovers
        if "%" in email or "&" in email or "+" in domain:
            return False
        return True

    def emails(self, text: str, url: str = "") -> list[Contact]:
        found: dict[str, Contact] = {}
        page_domain = registrable_domain(url) if url else ""
        page_is_contact = self._is_contact_url(url)

        def add(raw: str, conf: float, **meta: Any) -> None:
            email = html.unescape(unquote(raw)).strip().strip(".,;:'\"<>()[]").lower()
            email = email.split("?")[0]

            if not self.valid_email(email):
                return
            local, _, dom = email.partition("@")
            score = conf
            if local in ROLE_LOCALS:
                score += 0.05
            if page_domain and registrable_domain(dom) == page_domain:
                score += 0.20  # same-domain mailbox → very likely genuine
            if page_is_contact:
                score += 0.10
            if dom.split(".")[0] in {"gmail", "yahoo", "hotmail", "outlook", "proton"}:
                score -= 0.05
            score = max(0.05, min(score, 1.0))
            prev = found.get(email)
            if prev is None or score > prev.confidence:
                found[email] = Contact(
                    kind=ContactKind.EMAIL, value=email, source_url=url,
                    confidence=round(score, 2),
                    meta={"role": local in ROLE_LOCALS, **meta},
                )

        # 1. Cloudflare-protected addresses (highest trust)
        for token in CF_EMAIL_RE.findall(text):
            decoded = self._cf_decode(token)
            if decoded:
                add(decoded, 0.95, cfemail=True)

        # 2. mailto: links (explicit intent)
        for m in MAILTO_RE.findall(text):
            add(m.split(",")[0], 0.90, mailto=True)

        # 3. Plain addresses in the visible text / source
        visible = self.visible_text(text)
        for m in EMAIL_RE.finditer(visible):
            add(m.group(0), 0.75)
        for m in EMAIL_RE.finditer(text):
            add(m.group(0), 0.60)

        # 3b. Addresses inside SPA state blobs (Next.js/Nuxt/Remix/Apollo).
        #     These are real page content, just shipped as escaped JSON.
        state = self.embedded_state(text)
        if state:
            for m in EMAIL_RE.finditer(state):
                add(m.group(0), 0.70, embedded=True)

        # 4. Obfuscated "name (at) domain (dot) com"
        if self.deobfuscate:
            for pattern in OBFUSCATED_PATTERNS:
                for local, dom in pattern.findall(visible):
                    local = local.strip(" .,;:")
                    if not local or "@" in local:
                        continue
                    domain = DOT_WORDS.sub(".", dom).replace(" ", "").strip(".")
                    if domain.count(".") >= 1:
                        add(f"{local}@{domain}", 0.65, deobfuscated=True)
            # JS concatenation: 'name' + '@' + 'domain.com'
            for m in re.finditer(
                r"""['"]([A-Za-z0-9._%+-]{1,64})['"]\s*\+\s*['"]@?['"]\s*\+\s*['"]@?([A-Za-z0-9.-]+\.[A-Za-z]{2,})['"]""",
                text,
            ):
                add(f"{m.group(1)}@{m.group(2)}", 0.70, js_concat=True)

        return list(found.values())

    # ------------------------------------------------------------------ #
    # Phone
    # ------------------------------------------------------------------ #
    def phones(self, text: str, url: str = "") -> list[Contact]:
        out: dict[str, Contact] = {}

        def add(raw: str, conf: float) -> None:
            norm = self._normalise_phone(raw)
            if not norm:
                return
            prev = out.get(norm)
            if prev is None or conf > prev.confidence:
                out[norm] = Contact(
                    kind=ContactKind.PHONE, value=norm, source_url=url,
                    confidence=conf, meta={"raw": raw.strip()[:40]},
                )

        for m in TEL_RE.findall(text):
            add(m, 0.95)
        # Strip URLs first: page/app IDs inside links (e.g. a 15-digit Facebook
        # page id) otherwise look exactly like international numbers.
        visible = URL_IN_TEXT_RE.sub(" ", self.visible_text(text))
        for m in PHONE_RE.finditer(visible):
            raw = m.group(0)
            if not self._has_dialing_evidence(raw, visible, m.start()):
                continue
            add(raw, 0.6)
        return list(out.values())

    @staticmethod
    def _has_dialing_evidence(raw: str, text: str, pos: int) -> bool:
        """Require a positive signal that a digit run is really a phone number.

        Loose digit matching is the single biggest source of false positives:
        order numbers, licence IDs, dates and statistics all look like phones.
        A real number carries at least one of:

        * an international prefix (``+`` / ``00``)
        * internal grouping (spaces, dashes, brackets)
        * a nearby cue word ("Tel:", "Call us", "WhatsApp", "هاتف")
        """
        s = raw.strip()
        if s.startswith(("+", "00")):
            return True
        # Grouped digits: "555 123 4567", "(020) 7946", "0800-123-4567".
        digit_groups = re.findall(r"\d+", s)
        if len(digit_groups) >= 2 or "(" in s:
            return True
        # Otherwise look for a cue word immediately before the number.
        window = text[max(0, pos - 40):pos].lower()
        return bool(re.search(
            r"(?:tel|phone|call|mobile|cell|fax|whats\s?app|contact|hotline|"
            r"telefon|téléphone|teléfono|telefono|هاتف|جوال|اتصل)"
            r"[\s.:\-–—]*$",
            window,
        ))

    @staticmethod
    def _looks_like_number_list(raw: str) -> bool:
        """True for prose digit runs (code samples, tables, Fibonacci…).

        Real phone numbers have at most a few groups and use a consistent
        separator; ``8 13 21 34 55 89`` does not.
        """
        s = raw.strip()
        # Year-prefixed identifiers and ISO-ish dates: "2026-42533", "2024/11/03".
        if re.match(r"^(?:19|20)\d{2}\s*[-/]\s*\d", s):
            return True
        # Day-first and month-first dates: "09-02-2025", "31/07/2024", "1.6.2024".
        if re.fullmatch(r"\d{1,2}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*(?:19|20)\d{2}", s):
            return True
        # Trailing-year dates written without separators in the year: "09-02-25".
        if re.fullmatch(r"\d{1,2}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{2}", s):
            return True
        # Dotted quads are IP addresses / version strings, not numbers to call.
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){2,3}", s):
            return True
        groups = re.findall(r"\d+", raw)
        digits = sum(len(g) for g in groups)
        if len(groups) > 5:
            return True
        # E.164 tops out at 15 digits, and anything that long without an
        # explicit '+' country code is a list of numbers, not a phone.
        if digits > 12 and not raw.strip().startswith(("+", "00")):
            return True
        # 4+ evenly sized groups of 3+ digits reads as tabular data — but only
        # once it is too long to be a real number ("042 111 222 333" is valid,
        # "144 233 377 610 987" is not).
        if (
            len(groups) >= 4
            and len({len(g) for g in groups}) == 1
            and len(groups[0]) >= 3
            and digits > 12
        ):
            return True
        # 4+ uniform 2-digit groups with no country code is a sequence, not a
        # phone number (French-style "01 23 45 67 89" keeps its leading 0).
        if len(groups) >= 4 and all(len(g) == 2 for g in groups) and not raw.strip().startswith(("+", "0")):
            return True
        return False

    @staticmethod
    def _normalise_phone(raw: str) -> str | None:
        if Extractor._looks_like_number_list(raw):
            return None
        s = re.sub(r"(?i)\s*(?:ext|x|extn|poste)\.?\s*(\d{1,5})\s*$", r" x\1", raw.strip())
        ext = ""
        if " x" in s:
            s, _, ext = s.partition(" x")
            ext = f" x{ext.strip()}"
        digits = re.sub(r"[^\d+]", "", s)
        if digits.startswith("00"):
            digits = "+" + digits[2:]
        core = digits.lstrip("+")
        if not core.isdigit():
            return None
        n = len(core)
        if n < 7 or n > 15:
            return None
        # An international number needs country code + subscriber digits; a "+"
        # followed by only a few digits is a truncated fragment, not a number.
        if digits.startswith("+") and n < 10:
            return None
        if len(set(core)) <= 2:  # 000000000 / 1111111
            return None
        if core.startswith(("19", "20")) and n in (8, 10) and core[:4].isdigit():
            year = int(core[:4])
            if 1900 <= year <= 2099:  # looks like a date, not a phone
                return None
        return ("+" + core if digits.startswith("+") else core) + ext

    # ------------------------------------------------------------------ #
    # Socials
    # ------------------------------------------------------------------ #
    def socials(self, text: str, url: str = "") -> list[Contact]:
        out: dict[tuple[str, str], Contact] = {}
        for kind, pattern in SOCIAL_PATTERNS:
            for m in pattern.finditer(text):
                handle = m.group(1)
                if not handle or handle.lower() in {
                    "home", "login", "signup", "help", "privacy", "terms",
                    "pages", "profile.php", "sharer.php", "search", "explore",
                }:
                    continue
                if kind is ContactKind.GITHUB and m.lastindex and m.group(m.lastindex or 1):
                    pass
                value = self._social_url(kind, handle)
                key = (str(kind), value.casefold())
                if key not in out:
                    out[key] = Contact(
                        kind=kind, value=value, source_url=url, confidence=0.8,
                        meta={"handle": handle},
                    )
        return list(out.values())

    @staticmethod
    def _social_url(kind: ContactKind, handle: str) -> str:
        handle = handle.rstrip("/").strip()
        base = {
            ContactKind.LINKEDIN: "https://www.linkedin.com/",
            ContactKind.TWITTER: "https://twitter.com/",
            ContactKind.FACEBOOK: "https://www.facebook.com/",
            ContactKind.INSTAGRAM: "https://www.instagram.com/",
            ContactKind.TIKTOK: "https://www.tiktok.com/",
            ContactKind.YOUTUBE: "https://www.youtube.com/",
            ContactKind.GITHUB: "https://github.com/",
            ContactKind.MEDIUM: "https://medium.com/",
            ContactKind.TELEGRAM: "https://t.me/",
            ContactKind.WHATSAPP: "https://wa.me/",
        }[kind]
        if kind is ContactKind.LINKEDIN and not handle.startswith(("in/", "company/", "school/", "pub/")):
            handle = f"in/{handle}"
        return base + handle

    # ------------------------------------------------------------------ #
    # Page metadata & structured data
    # ------------------------------------------------------------------ #
    @staticmethod
    def visible_text(html_text: str) -> str:
        body = SCRIPT_STYLE_RE.sub(" ", html_text)
        body = TAG_RE.sub(" ", body)
        return html.unescape(body)

    @staticmethod
    def embedded_state(html_text: str) -> str:
        """Recover the text hidden inside SPA state blobs and JSON islands.

        Next.js, Nuxt, Remix and Apollo ship the page content as escaped JSON
        in ``<script>`` tags. :meth:`visible_text` deletes those, so without
        this a client-rendered page looks empty to the extractor.
        """
        chunks: list[str] = []
        for pattern in (STATE_BLOB_RE, JSON_SCRIPT_RE):
            for m in pattern.finditer(html_text):
                blob = m.group(1)
                if blob and len(blob) < 4_000_000:
                    chunks.append(blob)
        if not chunks:
            return ""
        text = " ".join(chunks)
        # Undo the JSON/HTML escaping that hides "@" and "." from the regexes.
        text = UNICODE_AT_RE.sub("@", text)
        text = UNICODE_DOT_RE.sub(".", text)
        text = text.replace("\\/", "/").replace('\\"', '"').replace("\\n", " ")
        return html.unescape(text)

    def metadata(self, text: str, url: str = "") -> dict[str, Any]:
        meta: dict[str, Any] = {}
        if m := TITLE_RE.search(text):
            meta["title"] = clean_text(TAG_RE.sub("", m.group(1)), 200)
        if m := META_DESC_RE.search(text):
            meta["description"] = clean_text(m.group(1), 400)
        if m := META_SITE_RE.search(text):
            meta["name"] = clean_text(m.group(1), 120)
        meta.update(self.jsonld(text))
        # og:site_name and JSON-LD give the *organisation*; a <title> is usually
        # an article headline ("Top 10 Best Schools in Riyadh 2026 | Ranking").
        # Only fall back to the title when it reads like a brand, not a story.
        if not meta.get("name"):
            candidate = self.brand_from_title(meta.get("title"), url)
            if candidate:
                meta["name"] = candidate
        return {k: v for k, v in meta.items() if v}

    #: Headline giveaways — listicles, guides and rankings are never brands.
    _HEADLINE_RE = re.compile(
        r"\b(top|best|guide|ranking|rankings|rating|ratings|review|reviews|"
        r"list of|how to|what is|why |vs\.?|cheap|compare|near me|"
        r"ultimate|complete|ideas|tips|examples|cost|price|prices|fees)\b",
        re.I,
    )

    @classmethod
    def brand_from_title(cls, title: str | None, url: str = "") -> str | None:
        """Extract an organisation name from a page title, or ``None``.

        Titles are commonly ``"Some Headline | Brand"``. We take the segment
        that looks like a brand and reject anything headline-shaped.
        """
        if not title:
            return None
        parts = [p.strip() for p in re.split(r"\s*[|–—·»:]\s*|\s+-\s+", title) if p.strip()]
        # "Home", "Welcome", "Index" are navigation labels, never brand names.
        parts = [p for p in parts
                 if p.lower().strip(" .") not in {
                     "home", "homepage", "welcome", "index", "main", "start",
                     "untitled", "page", "default", "site"}]
        if not parts:
            return None
        host = registrable_domain(url).split(".")[0].replace("-", "") if url else ""

        def looks_like_brand(seg: str) -> bool:
            if len(seg) > 60 or cls._HEADLINE_RE.search(seg):
                return False
            return len(seg.split()) <= 6

        # A segment matching the domain is almost certainly the brand.
        if host:
            for seg in parts:
                if seg.lower().replace(" ", "").replace("-", "").startswith(host[:6]):
                    return seg if looks_like_brand(seg) else None
        # Otherwise prefer the last segment (the usual "Headline | Brand" slot).
        for seg in (parts[-1], parts[0]):
            if looks_like_brand(seg):
                return seg
        return None

    def jsonld(self, text: str) -> dict[str, Any]:
        """Pull name/address/geo/rating/phone out of schema.org JSON-LD."""
        import json

        out: dict[str, Any] = {}
        for block in JSONLD_RE.findall(text)[:12]:
            raw = block.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                try:
                    data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
                except Exception:
                    continue
            for node in self._walk_jsonld(data):
                t = node.get("@type") or node.get("type") or ""
                types = {t} if isinstance(t, str) else set(t or [])
                types = {str(x).lower() for x in types}
                if not types & {
                    "organization", "localbusiness", "corporation", "person",
                    "restaurant", "store", "professionalservice", "medicalbusiness",
                    "website", "webpage", "dentist", "hotel", "school",
                }:
                    continue
                out.setdefault("name", clean_text(node.get("name"), 120))
                out.setdefault("description", clean_text(node.get("description"), 400))
                tel = node.get("telephone")
                if tel and "telephone" not in out:
                    out["telephone"] = clean_text(str(tel), 40)
                mail = node.get("email")
                if mail and "email" not in out:
                    out["email"] = str(mail).replace("mailto:", "").strip()
                addr = node.get("address")
                if addr and "address" not in out:
                    if isinstance(addr, dict):
                        parts = [
                            addr.get(k)
                            for k in (
                                "streetAddress", "addressLocality", "addressRegion",
                                "postalCode", "addressCountry",
                            )
                        ]
                        out["address"] = clean_text(
                            ", ".join(str(p) for p in parts if p and isinstance(p, (str, int)))
                        )
                    elif isinstance(addr, str):
                        out["address"] = clean_text(addr)
                geo = node.get("geo")
                if isinstance(geo, dict):
                    with_ = lambda k: geo.get(k) or geo.get(k.lower())  # noqa: E731
                    try:
                        out.setdefault("latitude", float(with_("latitude")))
                        out.setdefault("longitude", float(with_("longitude")))
                    except (TypeError, ValueError):
                        pass
                agg = node.get("aggregateRating")
                if isinstance(agg, dict):
                    try:
                        out.setdefault("rating", float(agg.get("ratingValue")))
                    except (TypeError, ValueError):
                        pass
                    try:
                        out.setdefault("reviews", int(float(agg.get("reviewCount") or agg.get("ratingCount"))))
                    except (TypeError, ValueError):
                        pass
        return {k: v for k, v in out.items() if v is not None}

    @staticmethod
    def _walk_jsonld(data: Any, depth: int = 0) -> Iterable[dict[str, Any]]:
        if depth > 6:
            return
        if isinstance(data, dict):
            yield data
            for key in ("@graph", "mainEntity", "itemListElement", "publisher", "author"):
                if key in data:
                    yield from Extractor._walk_jsonld(data[key], depth + 1)
        elif isinstance(data, list):
            for item in data[:50]:
                yield from Extractor._walk_jsonld(item, depth + 1)

    # ------------------------------------------------------------------ #
    # Link discovery
    # ------------------------------------------------------------------ #
    @staticmethod
    def _is_contact_url(url: str) -> bool:
        low = url.lower()
        return any(h in low for h in CONTACT_HINTS[:20])

    def links(self, text: str, base_url: str, internal_only: bool = True) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for href in HREF_RE.findall(text):
            u = normalise_url(href, base_url)
            if not u or u in seen:
                continue
            seen.add(u)
            path = urlsplit(u).path.lower()
            if any(path.endswith(ext) for ext in SKIP_EXTENSIONS):
                continue
            if internal_only and not same_site(u, base_url):
                continue
            out.append(u)
        return out

    def contact_links(self, text: str, base_url: str, limit: int = 8) -> list[str]:
        """Rank internal links by how likely they are to hold contact data."""
        scored: list[tuple[int, str]] = []
        for u in self.links(text, base_url, internal_only=True):
            low = u.lower()
            path = urlsplit(low).path
            score = 0
            for i, hint in enumerate(CONTACT_HINTS):
                if hint in path:
                    score += 100 - i
                    break
            if score == 0:
                continue
            depth = path.count("/")
            score -= depth * 3
            if len(path) < 30:
                score += 5
            scored.append((score, u))
        scored.sort(key=lambda t: (-t[0], len(t[1])))
        return [u for _, u in scored[:limit]]

    #: URL/title shapes that mean "this page lists other businesses".
    _DIRECTORY_URL_RE = re.compile(
        r"/(?:schools?|companies|businesses|listings?|directory|directories|"
        r"category|categories|cities|city|places|top-?\d+|best-|list-of-|"
        r"guides?)(?:/|$|-)",
        re.I,
    )

    def is_directory_page(self, text: str, url: str, contacts_found: int = 0) -> bool:
        """True when a page indexes other organisations rather than being one.

        Directory and listicle pages are what search engines surface for
        queries like "schools in Riyadh". The valuable leads are the sites they
        link *to*, so the crawler needs to recognise and follow through them.
        """
        outbound = self.outbound_orgs(text, url)
        if len(outbound) >= 5:
            return True
        # Fewer links but an unmistakable listing URL, and no contacts of its own.
        if len(outbound) >= 3 and self._DIRECTORY_URL_RE.search(url):
            return True
        if contacts_found == 0 and len(outbound) >= 3:
            title = (TITLE_RE.search(text) or [None, ""])[1] if TITLE_RE.search(text) else ""
            if self._HEADLINE_RE.search(title or ""):
                return True
        return False

    def outbound_orgs(self, text: str, base_url: str, limit: int = 60) -> list[str]:
        """External, scrapeable sites linked from a page — candidate leads.

        Filters out the CDNs, social networks and infrastructure hosts that
        every page links to, leaving plausible organisation homepages.
        """
        src = registrable_domain(base_url)
        seen: dict[str, str] = {}
        for link in self.links(text, base_url, internal_only=False):
            dom = registrable_domain(link)
            if not dom or dom == src or dom in seen:
                continue
            if not self.is_scrapeable(link):
                continue
            if dom in _INFRA_DOMAINS or any(dom.endswith("." + s) for s in ("gov", "gov.sa")):
                continue
            parts = urlsplit(link)
            # Prefer the homepage of each external organisation.
            seen[dom] = f"{parts.scheme}://{parts.netloc}/"
            if len(seen) >= limit:
                break
        return list(seen.values())

    @staticmethod
    def is_scrapeable(url: str) -> bool:
        """False for social/marketplace/CDN hosts and binary endpoints."""
        host = registrable_domain(url)
        if not host or host in SKIP_DOMAINS:
            return False
        path = urlsplit(url).path.lower()
        return not any(path.endswith(ext) for ext in SKIP_EXTENSIONS)

    # ------------------------------------------------------------------ #
    def extract_all(self, text: str, url: str) -> tuple[list[Contact], dict[str, Any]]:
        """One-shot extraction honouring the ``want`` setting."""
        contacts: list[Contact] = []
        s = self.s
        if s is None or getattr(s, "wants_email", True):
            contacts += self.emails(text, url)
        if s is None or getattr(s, "wants_phone", True):
            contacts += self.phones(text, url)
        if s is None or getattr(s, "wants_social", True):
            contacts += self.socials(text, url)
        meta = self.metadata(text, url)
        if tel := meta.get("telephone"):
            if norm := self._normalise_phone(tel):
                contacts.append(
                    Contact(ContactKind.PHONE, norm, url, 0.9, {"source": "jsonld"})
                )
        if mail := meta.get("email"):
            if self.valid_email(mail.lower()):
                contacts.append(
                    Contact(ContactKind.EMAIL, mail.lower(), url, 0.9, {"source": "jsonld"})
                )
        return contacts, meta
