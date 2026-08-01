"""Typed data models shared across the engine."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterable


class ContactKind(str, Enum):
    """Every class of artefact the extractor knows how to recognise."""

    EMAIL = "email"
    PHONE = "phone"
    LINKEDIN = "linkedin"
    TWITTER = "twitter"
    FACEBOOK = "facebook"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    YOUTUBE = "youtube"
    GITHUB = "github"
    MEDIUM = "medium"
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    ADDRESS = "address"
    WEBSITE = "website"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


SOCIAL_KINDS: frozenset[ContactKind] = frozenset(
    {
        ContactKind.LINKEDIN,
        ContactKind.TWITTER,
        ContactKind.FACEBOOK,
        ContactKind.INSTAGRAM,
        ContactKind.TIKTOK,
        ContactKind.YOUTUBE,
        ContactKind.GITHUB,
        ContactKind.MEDIUM,
        ContactKind.TELEGRAM,
        ContactKind.WHATSAPP,
    }
)


@dataclass(slots=True)
class Contact:
    """A single extracted artefact plus the provenance needed to trust it."""

    kind: ContactKind
    value: str
    source_url: str = ""
    #: 0.0 - 1.0 heuristic confidence (role address, obfuscation, page type...).
    confidence: float = 0.5
    #: Free-form notes, e.g. ``{"deobfuscated": True, "label": "Sales"}``.
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        """Deduplication key: kind + case-folded value."""
        return (str(self.kind), self.value.casefold())

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = str(self.kind)
        return d


@dataclass(slots=True)
class Lead:
    """One organisation / domain with everything discovered about it."""

    domain: str
    url: str = ""
    name: str | None = None
    title: str | None = None
    description: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    rating: float | None = None
    reviews: int | None = None
    category: str | None = None
    source: str = "crawl"
    contacts: list[Contact] = field(default_factory=list)
    pages_crawled: int = 0
    fetched_at: float = field(default_factory=time.time)
    errors: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # Contact helpers
    # ------------------------------------------------------------------ #
    def add(self, contacts: Iterable[Contact]) -> int:
        """Merge ``contacts`` in, keeping the highest-confidence duplicate.

        Returns the number of genuinely new artefacts added.
        """
        index = {c.key: c for c in self.contacts}
        added = 0
        for c in contacts:
            existing = index.get(c.key)
            if existing is None:
                index[c.key] = c
                added += 1
            elif c.confidence > existing.confidence:
                existing.confidence = c.confidence
                existing.source_url = c.source_url or existing.source_url
                existing.meta.update(c.meta)
        self.contacts = sorted(
            index.values(), key=lambda c: (str(c.kind), -c.confidence, c.value)
        )
        return added

    def of(self, kind: ContactKind) -> list[str]:
        return [c.value for c in self.contacts if c.kind is kind]

    @property
    def emails(self) -> list[str]:
        return self.of(ContactKind.EMAIL)

    @property
    def phones(self) -> list[str]:
        return self.of(ContactKind.PHONE)

    @property
    def socials(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for c in self.contacts:
            if c.kind in SOCIAL_KINDS:
                out.setdefault(str(c.kind), []).append(c.value)
        return out

    @property
    def score(self) -> float:
        """Lead quality 0-100: e-mail rich, phone, socials, metadata."""
        s = 0.0
        emails = [c for c in self.contacts if c.kind is ContactKind.EMAIL]
        if emails:
            s += 40 + min(len(emails) - 1, 4) * 3
            s += 10 * max((c.confidence for c in emails), default=0.0)
        if self.phones:
            s += 15
        s += min(len(self.socials) * 5, 20)
        if self.name or self.title:
            s += 5
        if self.address:
            s += 5
        return round(min(s, 100.0), 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "url": self.url,
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "address": self.address,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "rating": self.rating,
            "reviews": self.reviews,
            "category": self.category,
            "source": self.source,
            "score": self.score,
            "emails": self.emails,
            "phones": self.phones,
            "socials": self.socials,
            "pages_crawled": self.pages_crawled,
            "fetched_at": self.fetched_at,
            "errors": self.errors,
            "extra": self.extra,
            "contacts": [c.to_dict() for c in self.contacts],
        }


@dataclass(slots=True)
class Response:
    """Normalised HTTP response returned by :class:`nestick.http.Fetcher`."""

    url: str
    status: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    elapsed: float = 0.0
    from_cache: bool = False
    error: str | None = None
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300 and bool(self.text)


@dataclass(slots=True)
class Stats:
    """Live counters rendered by the CLI dashboard."""

    requests: int = 0
    cache_hits: int = 0
    failures: int = 0
    retries: int = 0
    bytes_down: int = 0
    pages_parsed: int = 0
    emails_found: int = 0
    leads: int = 0
    started: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return max(time.monotonic() - self.started, 1e-9)

    @property
    def rps(self) -> float:
        return self.requests / self.elapsed

    def as_row(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "failures": self.failures,
            "retries": self.retries,
            "mb_down": round(self.bytes_down / 1_048_576, 2),
            "pages_parsed": self.pages_parsed,
            "emails_found": self.emails_found,
            "leads": self.leads,
            "elapsed_s": round(self.elapsed, 1),
            "req_per_s": round(self.rps, 2),
        }
