"""Discovery: turn a query into URLs, and enrich leads via external APIs.

Sources
-------
* **SerpApi** — paged Google results, async polling (from ``clauneck.rb``)
* **DuckDuckGo HTML** — keyless fallback so the tool works out of the box
* **Bing HTML** — second keyless fallback
* **Google Places** — business leads with geo/rating (from ``app.js``)
* **Hunter.io** — domain → e-mail enrichment (from ``app.js``)
"""

from __future__ import annotations

import asyncio
import re
from html import unescape
from typing import Any, Iterable
from urllib.parse import parse_qs, quote_plus, urlsplit

from .config import Settings
from .extract import Extractor
from .http import Fetcher, api_error_message
from .models import Contact, ContactKind, Lead
from .utils import clean_text, dedupe, log, normalise_url, registrable_domain


class ApiError(RuntimeError):
    """A third-party API refused the request (bad key, quota, bad params)."""

DDG_RESULT_RE = re.compile(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"', re.I)
DDG_ANY_RE = re.compile(r'href="(/l/\?uddg=[^"]+|https?://duckduckgo\.com/l/\?[^"]+)"', re.I)
# Bing no longer wraps results in <li class="b_algo">; anchors live directly in
# <h2> and point at /ck/a redirects with the real URL base64'd in u=…
BING_RESULT_RE = re.compile(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"', re.I | re.S)
BING_CK_RE = re.compile(r"[?&]u=a1([A-Za-z0-9+/=]+)")
#: Phrases that mark a block/challenge page rather than real results.
BLOCK_MARKERS = (
    "anomaly", "captcha", "unusual traffic", "are you a robot",
    "verify you", "challenge", "temporarily blocked", "robot check",
)


class Discovery:
    """Produces seed URLs (and optionally full leads) for the pipeline."""

    def __init__(self, settings: Settings, fetcher: Fetcher) -> None:
        self.s = settings
        self.f = fetcher
        self.ex = Extractor(settings)
        #: Human-readable API failures collected during the run (deduplicated).
        self.api_errors: list[str] = []

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    async def discover(self) -> tuple[list[str], list[Lead]]:
        """Return ``(seed_urls, prebuilt_leads)``."""
        urls: list[str] = []
        leads: list[Lead] = []

        for raw in self.s.urls:
            u = normalise_url(raw if "://" in raw else f"https://{raw}")
            if u:
                urls.append(u)

        engine = self.s.engine
        if engine == "urls" or (not self.s.queries):
            return list(dedupe(urls)), leads

        if engine == "auto":
            engine = "serpapi" if self.s.serpapi_key else "duckduckgo"

        for query in self.s.queries:
            if self.s.places:
                try:
                    places = await self.places_any(query)
                    leads += places
                    urls += [l.url for l in places if l.url]
                except ApiError as exc:
                    self._note_api_error(str(exc))
            try:
                if engine == "serpapi" and self.s.serpapi_key:
                    try:
                        urls += await self.serpapi(query)
                    except ApiError as exc:
                        # A bad key must not silently produce zero leads:
                        # report it, then fall back to a keyless engine.
                        self._note_api_error(str(exc))
                        log.warning("Falling back to DuckDuckGo for %r", query)
                        urls += await self.duckduckgo(query)
                elif engine == "bing":
                    urls += await self.bing(query)
                else:
                    found = await self.duckduckgo(query)
                    if not found and engine != "bing":
                        log.info("DuckDuckGo empty — trying Bing for %r", query)
                        found = await self.bing(query)
                    urls += found
            except Exception as exc:  # noqa: BLE001
                log.warning("Discovery failed for %r: %s", query, exc)

        clean = [u for u in dedupe(urls) if u and self.ex.is_scrapeable(u)]
        return clean, leads

    def _note_api_error(self, message: str) -> None:
        """Record an API failure once and log it prominently."""
        if message not in self.api_errors:
            self.api_errors.append(message)
            log.error("%s", message)

    # ------------------------------------------------------------------ #
    # SerpApi
    # ------------------------------------------------------------------ #
    async def serpapi(self, query: str) -> list[str]:
        """Fetch N pages concurrently, handling SerpApi's async job polling."""
        num = min(max(self.s.results_per_page, 10), 100)

        async def one(page: int) -> list[str]:
            params = {
                "engine": "google",
                "q": query,
                "start": page * num,
                "num": num,
                "hl": self.s.language,
                "gl": self.s.country,
                "api_key": self.s.serpapi_key,
                "no_cache": "true",
            }
            if self.s.location:
                params["location"] = self.s.location
            data, err = await self.f.fetch_json(
                self.s.serpapi_url, params=params
            )
            if err or not data:
                raise ApiError(f"SerpApi: {err or 'no response'}")
            # Async job → poll the json_endpoint until it is done.
            for _ in range(60):
                status = (data.get("search_metadata") or {}).get("status", "Success")
                if status != "Processing":
                    break
                endpoint = (data.get("search_metadata") or {}).get("json_endpoint")
                if not endpoint:
                    break
                await asyncio.sleep(0.5)
                data = await self.f.get_json(endpoint) or data
            if msg := api_error_message(data):
                raise ApiError(f"SerpApi: {msg}")
            out: list[str] = []
            for r in data.get("organic_results") or []:
                link = r.get("link") or r.get("cached_page_link")
                if link:
                    out.append(link)
            for r in data.get("local_results", {}).get("places", []) if isinstance(
                data.get("local_results"), dict
            ) else []:
                if w := r.get("website"):
                    out.append(w)
            return out

        pages = await asyncio.gather(*(one(p) for p in range(self.s.pages)))
        urls = [normalise_url(u) for group in pages for u in group]
        found = [u for u in urls if u]
        log.info("SerpApi: %d URLs for %r", len(found), query)
        return found

    # ------------------------------------------------------------------ #
    # Keyless engines
    # ------------------------------------------------------------------ #
    async def duckduckgo(self, query: str) -> list[str]:
        out: list[str] = []
        q = query if not self.s.location else f"{query} {self.s.location}"
        for page in range(self.s.pages):
            url = (
                "https://html.duckduckgo.com/html/?q="
                f"{quote_plus(q)}&s={page * 30}&kl={self.s.country}-{self.s.language}"
            )
            r = await self.f.get(url, robots_check=False, use_cache=False,
                                 headers={"Referer": "https://duckduckgo.com/"})
            if not r.ok:
                self._note_blocked("DuckDuckGo", r.status)
                break
            hrefs = DDG_RESULT_RE.findall(r.text) or DDG_ANY_RE.findall(r.text)
            page_urls = [self._unwrap_ddg(h) for h in hrefs]
            page_urls = [u for u in page_urls if u]
            if not page_urls and self._looks_blocked(r.text):
                self._note_blocked("DuckDuckGo")
                break
            out += page_urls
            if len(page_urls) < 5:
                break
            await asyncio.sleep(0.8)
        log.info("DuckDuckGo: %d URLs for %r", len(out), query)
        return out

    @staticmethod
    def _unwrap_ddg(href: str) -> str | None:
        href = unescape(href)
        if href.startswith("//"):
            href = "https:" + href
        if "uddg=" in href:
            qs = parse_qs(urlsplit(href).query)
            if target := qs.get("uddg", [None])[0]:
                return normalise_url(target)
        return normalise_url(href)

    async def bing(self, query: str) -> list[str]:
        out: list[str] = []
        q = query if not self.s.location else f"{query} {self.s.location}"
        for page in range(self.s.pages):
            url = f"https://www.bing.com/search?q={quote_plus(q)}&first={page * 10 + 1}&count=30"
            r = await self.f.get(url, robots_check=False, use_cache=False)
            if not r.ok:
                self._note_blocked("Bing", r.status)
                break
            hits = [self._decode_bing(h) for h in BING_RESULT_RE.findall(r.text)]
            hits = [h for h in hits if h]
            if not hits and self._looks_blocked(r.text):
                self._note_blocked("Bing")
                break
            out += hits
            if not hits:
                break
            await asyncio.sleep(0.8)
        log.info("Bing: %d URLs for %r", len(out), query)
        return out

    @staticmethod
    def _decode_bing(href: str) -> str | None:
        """Resolve a ``bing.com/ck/a`` redirect to the actual destination URL."""
        href = unescape(href).replace("&amp;", "&")
        m = BING_CK_RE.search(href)
        if m:
            import base64

            try:
                pad = m.group(1) + "=" * (-len(m.group(1)) % 4)
                target = base64.b64decode(pad).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                target = ""
            if target:
                return normalise_url(target)
            return None
        if "bing.com" in urlsplit(href).netloc and "/ck/a" not in href:
            return None
        return normalise_url(href)

    @staticmethod
    def _looks_blocked(text: str) -> bool:
        low = (text or "").lower()
        return any(m in low for m in BLOCK_MARKERS)

    def _note_blocked(self, engine: str, status: int | None = None) -> None:
        """Record a block/challenge so the UI explains why a run found nothing."""
        code = f" (HTTP {status})" if status else ""
        msg = (f"{engine} returned a block or challenge page{code} — datacenter "
               f"IPs are often rate-limited. Add a SerpApi key, use the Places "
               f"option (free OpenStreetMap), or run from a residential IP.")
        self._note_api_error(msg)

    # ------------------------------------------------------------------ #
    # Google Places (port of app.js behaviour, server-side)
    # ------------------------------------------------------------------ #
    async def places_any(self, query: str) -> list[Lead]:
        """Google Places when a key is configured, OpenStreetMap otherwise.

        Falling back keeps ``--places`` useful without a billable key: OSM data
        is contributor-maintained, so coverage is thinner, but it is free,
        keyless and legitimate.
        """
        if self.s.google_maps_key:
            try:
                leads = await self.places_search(query)
                if leads:
                    return leads
                log.info("Google Places returned nothing — trying OpenStreetMap")
            except ApiError as exc:
                self._note_api_error(str(exc))
                log.warning("Falling back to OpenStreetMap for places")
        if not self.s.osm_fallback:
            return []
        from .places import OpenStreetMapPlaces

        return await OpenStreetMapPlaces(self.f, self.s).search(
            query, limit=self.s.osm_limit)

    async def places_search(self, query: str) -> list[Lead]:
        """Text Search + Details, paginated, returning business leads."""
        key = self.s.google_maps_key
        if not key:
            return []
        leads: list[Lead] = []
        token: str | None = None
        for _ in range(min(self.s.pages, 3)):  # Places caps at 3 pages / 60 results
            params: dict[str, Any] = {"key": key, "query": query}
            if self.s.location:
                params["query"] = f"{query} {self.s.location}"
            if token:
                params = {"key": key, "pagetoken": token}
                await asyncio.sleep(2)  # token activation delay
            data, err = await self.f.fetch_json(
                self.s.places_url, params=params
            )
            if err or not data:
                raise ApiError(f"Google Places: {err or 'no response'}")
            if data.get("status") not in ("OK", "ZERO_RESULTS"):
                raise ApiError(
                    f"Google Places: {api_error_message(data) or data.get('status')}"
                )
            results = data.get("results") or []
            details = await asyncio.gather(
                *(self._place_details(r.get("place_id"), key) for r in results)
            )
            for r, d in zip(results, details):
                geo = (r.get("geometry") or {}).get("location") or {}
                website = (d or {}).get("website")
                lead = Lead(
                    domain=registrable_domain(website) if website else "",
                    url=normalise_url(website) or "" if website else "",
                    name=clean_text(r.get("name"), 120),
                    address=clean_text(r.get("formatted_address")),
                    latitude=geo.get("lat"),
                    longitude=geo.get("lng"),
                    rating=r.get("rating"),
                    reviews=r.get("user_ratings_total"),
                    category=", ".join((r.get("types") or [])[:3]) or None,
                    source="places",
                    extra={"place_id": r.get("place_id"),
                           "business_status": r.get("business_status")},
                )
                # Prefer the international form: it carries the country code,
                # so it normalises to E.164 unambiguously.
                phone = (d or {}).get("international_phone_number") or (d or {}).get(
                    "formatted_phone_number"
                )
                if phone:
                    norm = Extractor._normalise_phone(phone)
                    if norm:
                        lead.add([Contact(ContactKind.PHONE, norm, lead.url, 0.95,
                                          {"source": "places"})])
                leads.append(lead)
            token = data.get("next_page_token")
            if not token:
                break
        log.info("Places: %d businesses for %r", len(leads), query)
        return leads

    async def _place_details(self, place_id: str | None, key: str) -> dict[str, Any] | None:
        if not place_id:
            return None
        data = await self.f.get_json(
            self.s.places_details_url,
            params={
                "key": key,
                "place_id": place_id,
                "fields": "website,formatted_phone_number,international_phone_number,url",
            },
        )
        return (data or {}).get("result")

    # ------------------------------------------------------------------ #
    # Hunter.io enrichment
    # ------------------------------------------------------------------ #
    async def hunter(self, domain: str) -> tuple[list[Contact], dict[str, Any]]:
        """Domain-search enrichment; returns contacts plus org metadata."""
        if not self.s.hunter_key or not domain:
            return [], {}
        data, err = await self.f.fetch_json(
            self.s.hunter_url,
            params={"api_key": self.s.hunter_key, "domain": domain, "limit": 25},
        )
        if err:
            raise ApiError(f"Hunter.io: {err}")
        if not data or "data" not in data:
            return [], {}
        d = data["data"]
        contacts: list[Contact] = []
        for e in d.get("emails") or []:
            value = (e.get("value") or "").lower()
            if not value:
                continue
            conf = float(e.get("confidence") or 50) / 100
            name = " ".join(
                x for x in (e.get("first_name"), e.get("last_name")) if x
            ) or None
            contacts.append(
                Contact(
                    kind=ContactKind.EMAIL, value=value,
                    source_url=f"hunter:{domain}",
                    confidence=round(min(0.99, 0.5 + conf / 2), 2),
                    meta={
                        "source": "hunter", "name": name,
                        "position": e.get("position"), "department": e.get("department"),
                        "verification": (e.get("verification") or {}).get("status"),
                    },
                )
            )
        for kind, key in (
            (ContactKind.TWITTER, "twitter"),
            (ContactKind.FACEBOOK, "facebook"),
            (ContactKind.LINKEDIN, "linkedin"),
            (ContactKind.INSTAGRAM, "instagram"),
        ):
            if val := d.get(key):
                url = val if str(val).startswith("http") else Extractor._social_url(kind, str(val))
                contacts.append(Contact(kind, url, f"hunter:{domain}", 0.85,
                                        {"source": "hunter"}))
        meta = {
            k: v for k, v in {
                "name": clean_text(d.get("organization"), 120),
                "country": d.get("country"),
                "industry": d.get("industry"),
                "company_type": d.get("company_type"),
            }.items() if v
        }
        return contacts, meta

    async def hunter_bulk(self, domains: Iterable[str]) -> dict[str, tuple[list[Contact], dict]]:
        doms = [d for d in dedupe(domains) if d]
        if not doms or not self.s.hunter_key:
            return {}
        sem = asyncio.Semaphore(5)  # respect Hunter rate limits
        halted = asyncio.Event()  # a bad key fails for every domain — stop early

        async def one(d: str):
            if halted.is_set():
                return None
            async with sem:
                if halted.is_set():
                    return None
                try:
                    return d, await self.hunter(d)
                except ApiError as exc:
                    self._note_api_error(str(exc))
                    halted.set()
                    return None
                except Exception as exc:  # noqa: BLE001
                    log.debug("Hunter lookup failed for %s: %s", d, exc)
                    return None

        results = await asyncio.gather(*(one(d) for d in doms))
        return {d: r for item in results if item for d, r in [item]}
