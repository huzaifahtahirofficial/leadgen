"""Places providers: Google Places, with a keyless OpenStreetMap fallback.

Google Places is excellent but needs a billable key. When no key is configured
(or the key fails), Nestick falls back to OpenStreetMap:

* **Nominatim** geocodes the location into a bounding box.
* **Overpass** returns every matching POI in that box, with the
  ``website`` / ``phone`` / ``email`` tags contributors have added.

Both are free, keyless, and legitimate — used within their published rate
limits and with an identifying User-Agent, as their policies require.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Iterable

from .models import Contact, ContactKind, Lead
from .utils import clean_text, log, normalise_url, registrable_domain

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
)

#: Identifies the client, as the OSM usage policy requires.
OSM_UA = "Nestick/1.0 (contact-and-lead scraper; +https://github.com/nestick)"

#: Map everyday search words onto the OSM tags that represent them.
CATEGORY_TAGS: tuple[tuple[re.Pattern[str], tuple[tuple[str, str], ...]], ...] = (
    (re.compile(r"\b(school|schools|academy|academies|kindergarten)\b", re.I),
     (("amenity", "school"), ("amenity", "kindergarten"))),
    (re.compile(r"\b(universit|college|institute)\w*\b", re.I),
     (("amenity", "university"), ("amenity", "college"))),
    (re.compile(r"\b(restaurant|dining|eatery|food)\b", re.I),
     (("amenity", "restaurant"), ("amenity", "fast_food"))),
    (re.compile(r"\b(cafe|cafes|coffee|coffeeshop)\b", re.I),
     (("amenity", "cafe"),)),
    (re.compile(r"\b(hotel|hotels|motel|hostel|accommodation)\b", re.I),
     (("tourism", "hotel"), ("tourism", "guest_house"))),
    (re.compile(r"\b(hospital|clinic|medical|healthcare)\b", re.I),
     (("amenity", "hospital"), ("amenity", "clinic"))),
    (re.compile(r"\b(dentist|dentists|dental)\b", re.I),
     (("amenity", "dentist"),)),
    (re.compile(r"\b(pharmacy|pharmacies|chemist)\b", re.I),
     (("amenity", "pharmacy"),)),
    (re.compile(r"\b(doctor|doctors|physician|gp)\b", re.I),
     (("amenity", "doctors"),)),
    (re.compile(r"\b(lawyer|lawyers|solicitor|attorney|law firm)\b", re.I),
     (("office", "lawyer"),)),
    (re.compile(r"\b(gym|gyms|fitness)\b", re.I),
     (("leisure", "fitness_centre"),)),
    (re.compile(r"\b(bank|banks)\b", re.I), (("amenity", "bank"),)),
    (re.compile(r"\b(garage|mechanic|car repair)\b", re.I),
     (("shop", "car_repair"),)),
    (re.compile(r"\b(salon|barber|hairdresser)\b", re.I),
     (("shop", "hairdresser"), ("shop", "beauty"))),
    (re.compile(r"\b(bakery|bakeries)\b", re.I), (("shop", "bakery"),)),
    (re.compile(r"\b(real estate|realtor|estate agent)\b", re.I),
     (("office", "estate_agent"),)),
    (re.compile(r"\b(agency|agencies|marketing|consult\w*|company|companies|"
                r"startup|software|studio)\b", re.I),
     (("office", "company"), ("office", "it"), ("office", "consulting"),
      ("office", "advertising_agency"))),
    (re.compile(r"\b(shop|shops|store|stores|retail)\b", re.I),
     (("shop", "yes"),)),
)

#: Strip the category words so only the place name remains for geocoding.
_LOCATION_SPLIT = re.compile(r"\b(?:in|near|around|at)\b", re.I)


def tags_for_query(query: str) -> list[tuple[str, str]]:
    """Best-guess OSM tags for a natural-language query."""
    out: list[tuple[str, str]] = []
    for pattern, tags in CATEGORY_TAGS:
        if pattern.search(query):
            out.extend(tags)
    return out or [("office", "company"), ("shop", "yes")]


def location_from_query(query: str, explicit: str | None = None) -> str | None:
    """Pull the place name out of "dentists in Lahore" -> "Lahore"."""
    if explicit:
        return explicit.strip()
    parts = _LOCATION_SPLIT.split(query, maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        return parts[1].strip()
    return None


class OpenStreetMapPlaces:
    """Keyless business search backed by Nominatim + Overpass."""

    #: Nominatim's policy is a maximum of one request per second.
    _NOMINATIM_GAP = 1.1
    _last_nominatim = 0.0
    _lock = asyncio.Lock()

    def __init__(self, fetcher: Any, settings: Any) -> None:
        self.f = fetcher
        self.s = settings

    # ------------------------------------------------------------------ #
    async def bounding_box(self, place: str) -> tuple[float, float, float, float] | None:
        """Geocode ``place`` to ``(south, west, north, east)``."""
        async with self._lock:
            wait = self._NOMINATIM_GAP - (time.monotonic() - OpenStreetMapPlaces._last_nominatim)
            if wait > 0:
                await asyncio.sleep(wait)
            OpenStreetMapPlaces._last_nominatim = time.monotonic()

        data, err = await self.f.fetch_json(
            NOMINATIM_URL,
            params={"q": place, "format": "jsonv2", "limit": 1},
            headers={"User-Agent": OSM_UA, "Accept-Language": self.s.language},
            robots_check=False,
        )
        if err or not data:
            log.debug("Nominatim failed for %r: %s", place, err)
            return None
        first = data[0] if isinstance(data, list) and data else None
        if not first or "boundingbox" not in first:
            return None
        try:
            s, n, w, e = (float(x) for x in first["boundingbox"])
        except (TypeError, ValueError):
            return None
        log.debug("Geocoded %r -> bbox %s", place, (s, w, n, e))
        return (s, w, n, e)

    # ------------------------------------------------------------------ #
    @staticmethod
    def build_query(bbox: tuple[float, float, float, float],
                    tags: Iterable[tuple[str, str]], limit: int) -> str:
        s, w, n, e = bbox
        box = f"{s},{w},{n},{e}"
        clauses = []
        for key, value in tags:
            selector = f'["{key}"]' if value == "yes" else f'["{key}"="{value}"]'
            clauses.append(f"node{selector}({box});")
            clauses.append(f"way{selector}({box});")
        return (f"[out:json][timeout:60];\n({''.join(clauses)});\n"
                f"out tags center {limit};")

    async def search(self, query: str, limit: int = 200) -> list[Lead]:
        """Return business leads for ``query`` using OSM data only."""
        place = location_from_query(query, self.s.location)
        if not place:
            log.info("OSM places needs a location — add one, e.g. 'cafes in Lahore'")
            return []

        bbox = await self.bounding_box(place)
        if not bbox:
            log.warning("Could not geocode %r via OpenStreetMap", place)
            return []

        body = self.build_query(bbox, tags_for_query(query), limit)
        elements: list[dict[str, Any]] = []
        for endpoint in OVERPASS_URLS:
            data, err = await self.f.fetch_json(
                endpoint, method="POST", data={"data": body},
                headers={"User-Agent": OSM_UA}, robots_check=False, retries=1,
            )
            if data and not err:
                elements = data.get("elements") or []
                break
            log.debug("Overpass %s failed: %s", endpoint, err)
        if not elements:
            log.warning("Overpass returned nothing for %r", query)
            return []

        leads = [self._to_lead(el) for el in elements]
        leads = [l for l in leads if l is not None]
        log.info("OpenStreetMap: %d place(s) for %r (%d with a website)",
                 len(leads), query, sum(1 for l in leads if l.url))
        return leads

    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_lead(el: dict[str, Any]) -> Lead | None:
        tags = el.get("tags") or {}
        name = clean_text(tags.get("name") or tags.get("official_name")
                          or tags.get("name:en"), 120)
        website = (tags.get("website") or tags.get("contact:website")
                   or tags.get("url") or "")
        phone = (tags.get("phone") or tags.get("contact:phone")
                 or tags.get("contact:mobile") or "")
        email = (tags.get("email") or tags.get("contact:email") or "")
        if not (name or website):
            return None

        url = normalise_url(website if "://" in website else f"https://{website}") if website else ""
        centre = el.get("center") or {}
        lat = el.get("lat", centre.get("lat"))
        lon = el.get("lon", centre.get("lon"))

        address = ", ".join(
            str(tags[k]) for k in (
                "addr:housenumber", "addr:street", "addr:district",
                "addr:city", "addr:postcode", "addr:country")
            if tags.get(k)
        ) or None

        lead = Lead(
            domain=registrable_domain(url) if url else "",
            url=url or "",
            name=name,
            address=clean_text(address),
            latitude=float(lat) if lat is not None else None,
            longitude=float(lon) if lon is not None else None,
            category=tags.get("amenity") or tags.get("shop") or tags.get("office")
            or tags.get("tourism"),
            source="openstreetmap",
            extra={"osm_id": el.get("id"), "osm_type": el.get("type")},
        )

        from .extract import Extractor

        # A single tag can hold several numbers: "+966 11 x;+966 55 y".
        for raw in re.split(r"[;,]", phone):
            norm = Extractor._normalise_phone(raw) if raw.strip() else None
            if norm:
                lead.add([Contact(ContactKind.PHONE, norm, url, 0.9,
                                  {"source": "openstreetmap"})])
        for raw in re.split(r"[;,]", email):
            raw = raw.strip().lower()
            if raw and Extractor(None).valid_email(raw):
                lead.add([Contact(ContactKind.EMAIL, raw, url, 0.85,
                                  {"source": "openstreetmap"})])
        return lead
