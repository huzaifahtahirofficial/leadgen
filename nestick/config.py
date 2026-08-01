"""Configuration objects, user-agent pool and crawl heuristics."""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# User agents — modern desktop pool (superset of the Ruby + Puppeteer lists).
# --------------------------------------------------------------------------- #
USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.2478.67",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 OPR/111.0.0.0",
)

#: Sec-CH-UA hints paired with Chromium UAs for a coherent fingerprint.
CLIENT_HINTS: dict[str, str] = {
    "126": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
    "125": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
    "124": '"Chromium";v="124", "Microsoft Edge";v="124", "Not-A.Brand";v="99"',
    "123": '"Google Chrome";v="123", "Chromium";v="123", "Not:A-Brand";v="99"',
}

# --------------------------------------------------------------------------- #
# Crawl heuristics
# --------------------------------------------------------------------------- #

#: Pages most likely to carry contact details — crawled first, scored highest.
#:
#: Order matters: earlier entries score higher. Legal pages sit high because
#: modern JS-heavy sites often publish *no* address on /contact (that is just a
#: form) while privacy policies and imprints are legally obliged to carry one.
CONTACT_HINTS: tuple[str, ...] = (
    "contact", "contact-us", "contactus", "kontakt", "iletisim", "iletişim",
    "bize-ulasin", "contacto", "contatti", "nous-contacter",
    "impressum", "imprint", "legal-notice", "mentions-legales",
    "privacy-policy", "privacy", "datenschutz", "gizlilik",
    "legal", "terms", "terms-of-service", "dpa", "gdpr",
    "about", "about-us", "aboutus", "hakkimizda", "team", "our-team", "staff",
    "people", "leadership", "management",
    "support", "help", "customer-service", "sales",
    "press", "media", "careers", "jobs", "locations", "offices", "franchise",
    "security", "abuse", "trust",
)

#: Never worth crawling for leads — social aggregators, marketplaces, CDNs.
SKIP_DOMAINS: frozenset[str] = frozenset(
    {
        "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
        "youtube.com", "youtu.be", "tiktok.com", "pinterest.com", "reddit.com",
        "wikipedia.org", "wikimedia.org", "google.com", "gstatic.com",
        "googleusercontent.com", "doubleclick.net", "amazon.com", "ebay.com",
        "aliexpress.com", "alibaba.com", "trendyol.com", "hepsiburada.com",
        "n11.com", "sahibinden.com", "daraz.pk", "olx.com.pk", "yelp.com",
        "tripadvisor.com", "booking.com", "indeed.com", "glassdoor.com",
        "medium.com", "blogspot.com", "wordpress.com", "wix.com", "github.io",
        "apple.com", "microsoft.com", "cloudflare.com", "jsdelivr.net",
        "unpkg.com", "bootstrapcdn.com", "fontawesome.com", "w3.org",
        "schema.org", "archive.org", "t.me", "whatsapp.com", "play.google.com",
    }
)

#: Binary / non-HTML endpoints skipped before a request is even made.
SKIP_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip",
        ".rar", ".7z", ".tar", ".gz", ".bz2", ".exe", ".dmg", ".pkg", ".apk",
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp",
        ".tif", ".tiff", ".mp3", ".mp4", ".avi", ".mov", ".wmv", ".flv",
        ".webm", ".wav", ".ogg", ".css", ".js", ".json", ".xml", ".rss",
        ".woff", ".woff2", ".ttf", ".eot", ".map", ".csv",
    }
)

#: Junk mailbox domains / vendor addresses that pollute e-mail harvests.
EMAIL_BLOCKLIST: frozenset[str] = frozenset(
    {
        "example.com", "example.org", "email.com", "domain.com", "yourdomain.com",
        "yoursite.com", "sitename.com", "test.com", "sample.com", "wix.com",
        "wordpress.com", "squarespace.com", "godaddy.com", "sentry.io",
        "sentry-next.wixpress.com", "wixpress.com", "google-analytics.com",
        "googlemail.example", "yandex-team.ru", "jquery.com", "bootstrapcdn.com",
        "fontawesome.com", "w3.org", "schema.org", "adobe.com", "mysite.com",
        "your-email.com", "yourcompany.com", "companyname.com", "placeholder.com",
    }
)

#: Local-parts that indicate a template/placeholder rather than a real inbox.
EMAIL_LOCAL_BLOCKLIST: frozenset[str] = frozenset(
    {
        "email", "youremail", "your-email", "name", "yourname", "user",
        "username", "example", "test", "sample", "somebody", "someone",
        "firstname", "lastname", "no-reply", "noreply", "donotreply",
        "do-not-reply", "sentry", "wixpress", "core-services",
    }
)

#: Role mailboxes — lower value than a named person but still real leads.
ROLE_LOCALS: frozenset[str] = frozenset(
    {
        "info", "contact", "hello", "hi", "mail", "email", "office", "admin",
        "sales", "support", "help", "service", "enquiries", "enquiry",
        "inquiry", "team", "general", "marketing", "press", "media", "hr",
        "jobs", "careers", "billing", "accounts", "finance", "webmaster",
        "postmaster", "bilgi", "iletisim", "kontakt",
    }
)


def _env(name: str, default: Any = None) -> Any:
    return os.environ.get(name, default)


@dataclass(slots=True)
class Settings:
    """Every knob of the engine, in one validated object.

    Only ``query`` **or** ``urls`` (or ``input_file``) is strictly required.
    """

    # ---- Discovery -------------------------------------------------- #
    query: str | None = None
    queries: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    input_file: str | None = None
    pages: int = 1
    results_per_page: int = 10
    location: str | None = None
    language: str = "en"
    country: str = "us"
    engine: str = "auto"  # auto | serpapi | duckduckgo | bing | urls
    serpapi_key: str | None = field(default_factory=lambda: _env("SERPAPI_KEY"))
    hunter_key: str | None = field(default_factory=lambda: _env("HUNTER_API_KEY"))
    #: NumVerify free tier (100 lookups/month) — carrier + line type for phones.
    numverify_key: str | None = field(default_factory=lambda: _env("NUMVERIFY_KEY"))
    google_maps_key: str | None = field(default_factory=lambda: _env("GOOGLE_MAPS_KEY"))
    places: bool = False  # enrich with Google Places business data
    #: When no Google Maps key is set (or it fails), use OpenStreetMap's free
    #: Nominatim + Overpass APIs instead. Keyless and within their usage policy.
    osm_fallback: bool = True
    osm_limit: int = 200

    # ---- API endpoints (override for self-hosted proxies or testing) ---- #
    serpapi_url: str = field(
        default_factory=lambda: _env("NESTICK_SERPAPI_URL", "https://serpapi.com/search.json"))
    hunter_url: str = field(
        default_factory=lambda: _env("NESTICK_HUNTER_URL",
                                     "https://api.hunter.io/v2/domain-search"))
    places_url: str = field(
        default_factory=lambda: _env(
            "NESTICK_PLACES_URL",
            "https://maps.googleapis.com/maps/api/place/textsearch/json"))
    places_details_url: str = field(
        default_factory=lambda: _env(
            "NESTICK_PLACES_DETAILS_URL",
            "https://maps.googleapis.com/maps/api/place/details/json"))

    # ---- Crawling ---------------------------------------------------- #
    concurrency: int = 24
    per_host_concurrency: int = 3
    max_pages_per_site: int = 6
    depth: int = 1
    #: Search engines return directory/listicle pages for "X in <city>" queries.
    #: Follow the organisations those pages link to, which are the real leads.
    follow_directories: bool = True
    max_directory_links: int = 40
    directory_waves: int = 1
    timeout: float = 15.0
    connect_timeout: float = 8.0
    max_retries: int = 3
    backoff_base: float = 0.6
    backoff_max: float = 12.0
    delay: float = 0.0  # extra polite delay per host (seconds)
    jitter: float = 0.35
    max_body_bytes: int = 3_000_000
    follow_redirects: bool = True
    verify_ssl: bool = False
    #: SSRF guard. Off by default: scraped links must not reach localhost, RFC1918
    #: space or cloud metadata endpoints. Enable only for deliberate intranet work.
    allow_private_networks: bool = False
    #: Also resolve DNS before fetching, catching hostnames that deliberately
    #: point at internal IPs (DNS rebinding). Slight cost per new host.
    resolve_dns_guard: bool = True
    http2: bool = True
    respect_robots: bool = True
    user_agents: tuple[str, ...] = USER_AGENTS
    proxies: list[str] = field(default_factory=list)
    proxy_file: str | None = None

    # ---- Extraction --------------------------------------------------- #
    want: tuple[str, ...] = ("email", "phone", "social")
    deobfuscate: bool = True
    min_confidence: float = 0.0
    #: Keyless MX lookup proving a domain can actually receive mail.
    verify_mx: bool = True
    #: Wikidata/Wikipedia company profiles. Off by default: one extra request
    #: per named lead, and the match is heuristic.
    firmographics: bool = False
    #: Guard against staff-directory dumps (a careers page can list hundreds of
    #: mailboxes). Keeps the highest-confidence addresses. 0 disables the cap.
    max_emails_per_lead: int = 25

    # ---- Storage / output ---------------------------------------------- #
    output: str = "leads"  # basename; formats appended
    formats: tuple[str, ...] = ("csv", "json")
    cache: bool = True
    cache_path: str = field(default_factory=lambda: _env("NESTICK_CACHE_PATH", ".nestick_cache.sqlite"))
    cache_ttl: int = 86_400
    resume: bool = True
    #: Defaults to ``<output>.state.json`` so separate jobs never share state.
    state_path: str = ""

    # ---- UX ------------------------------------------------------------- #
    verbose: bool = False
    quiet: bool = False
    progress: bool = True
    log_file: str | None = None
    seed: int | None = None

    # ------------------------------------------------------------------ #
    def __post_init__(self) -> None:
        if self.query and self.query not in self.queries:
            self.queries.insert(0, self.query)
        if self.proxy_file:
            p = Path(self.proxy_file).expanduser()
            if p.is_file():
                self.proxies += [
                    ln.strip()
                    for ln in p.read_text("utf-8", errors="ignore").splitlines()
                    if ln.strip() and not ln.startswith("#")
                ]
        if self.input_file:
            p = Path(self.input_file).expanduser()
            if p.is_file():
                self.urls += [
                    ln.strip()
                    for ln in p.read_text("utf-8", errors="ignore").splitlines()
                    if ln.strip() and not ln.startswith("#")
                ]
        self.concurrency = max(1, int(self.concurrency))
        self.per_host_concurrency = max(1, int(self.per_host_concurrency))
        self.pages = max(1, int(self.pages))
        self.depth = max(0, int(self.depth))
        self.max_pages_per_site = max(1, int(self.max_pages_per_site))
        if isinstance(self.formats, str):
            self.formats = tuple(f.strip() for f in self.formats.split(",") if f.strip())
        if isinstance(self.want, str):
            self.want = tuple(w.strip() for w in self.want.split(",") if w.strip())
        if not self.state_path:
            base = Path(self.output).expanduser()
            if base.suffix.lower() in {".csv", ".json", ".jsonl", ".xlsx", ".md", ".db"}:
                base = base.with_suffix("")
            self.state_path = str(base.with_name(f".{base.name}.state.json"))
        if self.seed is not None:
            random.seed(self.seed)
        if not self.queries and not self.urls:
            raise ValueError("Settings needs at least one of: query / queries / urls / input_file")

    # ---- convenience -------------------------------------------------- #
    @property
    def wants_email(self) -> bool:
        return "email" in self.want or "all" in self.want

    @property
    def wants_phone(self) -> bool:
        return "phone" in self.want or "all" in self.want

    @property
    def wants_social(self) -> bool:
        return "social" in self.want or "all" in self.want

    def random_ua(self) -> str:
        return random.choice(self.user_agents)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if f.name in {"serpapi_key", "hunter_key", "google_maps_key"} and v:
                v = f"***{str(v)[-4:]}"
            if f.name == "proxies":
                v = f"{len(v)} proxies"
            out[f.name] = list(v) if isinstance(v, tuple) else v
        return out
