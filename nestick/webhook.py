"""Admission control — validating and mutating webhooks for ScrapeJob.

Modelled on Harvester's ``pkg/webhook/resources``: every resource passes through
mutation (fill in defaults, normalise) and validation (reject the impossible)
*before* the controller ever acts on it. Catching a typo here costs
milliseconds; catching it after a 40-minute crawl costs the crawl.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from .resources import NAME_RE, ConditionType, ScrapeJob

VALID_ENGINES = ("auto", "serpapi", "duckduckgo", "bing", "urls")
VALID_FORMATS = ("csv", "json", "jsonl", "xlsx", "md", "sqlite", "db", "excel",
                 "markdown", "all")
VALID_WANT = ("email", "phone", "social", "all")
SCHEDULE_RE = re.compile(
    r"^(@(hourly|daily|weekly|monthly)|every\s+\d+\s*(s|m|h|d|sec|min|hour|day)s?)$",
    re.I,
)


class AdmissionError(ValueError):
    """The resource was rejected by a validating webhook."""


@dataclass(slots=True)
class Review:
    """Outcome of admission: allowed plus any warnings and applied patches."""

    allowed: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    patches: list[str] = field(default_factory=list)

    def deny(self, msg: str) -> None:
        self.allowed = False
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def patched(self, msg: str) -> None:
        self.patches.append(msg)

    def raise_for_status(self) -> None:
        if not self.allowed:
            raise AdmissionError("; ".join(self.errors))

    def summary(self) -> str:
        bits = []
        if self.errors:
            bits.append("errors: " + "; ".join(self.errors))
        if self.warnings:
            bits.append("warnings: " + "; ".join(self.warnings))
        return " | ".join(bits) or "ok"


# --------------------------------------------------------------------------- #
# Mutating webhook
# --------------------------------------------------------------------------- #
def mutate(job: ScrapeJob, review: Review | None = None) -> Review:
    """Normalise a job in place: defaults, canonical values, safe bounds."""
    r = review or Review()
    s = job.spec
    job.ensure_defaults()

    # metadata -------------------------------------------------------- #
    name = job.metadata.get("name", "")
    slug = re.sub(r"[^a-z0-9-]+", "-", str(name).strip().lower()).strip("-")
    if slug and slug != name:
        job.metadata["name"] = slug
        r.patched(f"metadata.name normalised to {slug!r}")

    # scalars ---------------------------------------------------------- #
    if s.engine:
        low = s.engine.strip().lower()
        aliases = {"ddg": "duckduckgo", "google": "serpapi", "serp": "serpapi"}
        low = aliases.get(low, low)
        if low != s.engine:
            s.engine = low
            r.patched(f"spec.engine → {low!r}")

    s.queries = [q.strip() for q in s.queries if str(q).strip()]
    s.urls = [_normalise_url(u) for u in s.urls if str(u).strip()]
    s.urls = [u for u in s.urls if u]

    # If only URLs are given, the engine must be "urls".
    if s.urls and not s.queries and s.engine == "auto":
        s.engine = "urls"
        r.patched("spec.engine → 'urls' (no queries given)")

    # de-duplicate while preserving order
    for attr in ("queries", "urls", "want"):
        seen: set[str] = set()
        vals = []
        for v in getattr(s, attr):
            if v not in seen:
                seen.add(v)
                vals.append(v)
        if len(vals) != len(getattr(s, attr)):
            r.patched(f"spec.{attr} de-duplicated")
        setattr(s, attr, vals)

    s.output.formats = [f.strip().lower() for f in s.output.formats if str(f).strip()]
    if not s.output.formats:
        s.output.formats = ["csv", "json"]
        r.patched("spec.output.formats defaulted to [csv, json]")
    if not s.want:
        s.want = ["email", "phone", "social"]
        r.patched("spec.want defaulted to all types")

    # clamp numeric fields into workable ranges ------------------------ #
    clamps: list[tuple[Any, str, float, float]] = [
        (s, "pages", 1, 50),
        (s, "minConfidence", 0.0, 1.0),
        (s, "maxEmailsPerLead", 0, 10_000),
        (s, "backoffLimit", 0, 10),
        (s.crawl, "concurrency", 1, 256),
        (s.crawl, "perHost", 1, 32),
        (s.crawl, "maxPagesPerSite", 1, 200),
        (s.crawl, "depth", 0, 5),
        (s.crawl, "timeout", 1.0, 300.0),
        (s.crawl, "retries", 0, 10),
        (s.crawl, "delay", 0.0, 300.0),
    ]
    for obj, attr, lo, hi in clamps:
        raw = getattr(obj, attr)
        try:
            num = type(lo)(raw)
        except (TypeError, ValueError):
            num = lo
        fixed = max(lo, min(num, hi))
        if fixed != raw:
            setattr(obj, attr, fixed)
            r.patched(f"{attr} clamped {raw!r} → {fixed}")

    if s.schedule:
        s.schedule = s.schedule.strip().lower()
    return r


# --------------------------------------------------------------------------- #
# Validating webhook
# --------------------------------------------------------------------------- #
def validate(job: ScrapeJob, review: Review | None = None) -> Review:
    """Reject a job that cannot possibly run correctly."""
    r = review or Review()
    s = job.spec

    if not NAME_RE.match(job.name or ""):
        r.deny(f"metadata.name {job.name!r} must be lowercase alphanumeric or '-' "
               "(RFC 1123), max 63 chars")

    if not s.queries and not s.urls:
        r.deny("spec must set at least one of: queries, urls")

    if s.engine not in VALID_ENGINES:
        r.deny(f"spec.engine {s.engine!r} invalid; choose from {list(VALID_ENGINES)}")

    if s.engine == "urls" and not s.urls:
        r.deny("spec.engine is 'urls' but spec.urls is empty")

    if s.engine == "serpapi" and not s.queries:
        r.deny("spec.engine is 'serpapi' but spec.queries is empty")

    bad_formats = [f for f in s.output.formats if f not in VALID_FORMATS]
    if bad_formats:
        r.deny(f"spec.output.formats has unknown entries {bad_formats}; "
               f"valid: {list(VALID_FORMATS)}")

    bad_want = [w for w in s.want if w not in VALID_WANT]
    if bad_want:
        r.deny(f"spec.want has unknown entries {bad_want}; valid: {list(VALID_WANT)}")

    if not s.output.path or str(s.output.path).strip() in (".", "/"):
        r.deny("spec.output.path must be a file basename or path")

    for u in s.urls:
        parts = urlsplit(u)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            r.deny(f"spec.urls entry {u!r} is not a valid http(s) URL")

    if s.schedule and not SCHEDULE_RE.match(s.schedule):
        r.deny(f"spec.schedule {s.schedule!r} invalid; use @hourly/@daily/@weekly/"
               "@monthly or 'every 30m'")

    if s.places and not s.queries:
        r.deny("spec.places needs at least one query")

    # --- warnings: legal but likely a mistake -------------------------- #
    if not s.crawl.respectRobots:
        r.warn("spec.crawl.respectRobots is false — you are ignoring robots.txt")
    if s.crawl.concurrency > 64 and s.crawl.delay == 0:
        r.warn(f"concurrency {s.crawl.concurrency} with no delay may get you rate-limited")
    if s.crawl.perHost > 8:
        r.warn(f"perHost {s.crawl.perHost} is aggressive against a single site")
    if s.pages > 10:
        r.warn(f"spec.pages={s.pages} will consume a lot of SERP quota")
    if s.maxEmailsPerLead == 0:
        r.warn("maxEmailsPerLead=0 disables the staff-directory cap")
    if s.crawl.maxPagesPerSite > 50:
        r.warn(f"maxPagesPerSite={s.crawl.maxPagesPerSite} makes each site slow")

    job.status.set_condition(
        ConditionType.VALIDATED, r.allowed,
        "AdmissionPassed" if r.allowed else "AdmissionDenied",
        "; ".join(r.errors) if r.errors else "spec accepted",
    )
    return r


def admit(job: ScrapeJob) -> Review:
    """Run the full chain: mutate, then validate."""
    review = Review()
    mutate(job, review)
    validate(job, review)
    return review


def _normalise_url(raw: str) -> str | None:
    from .utils import normalise_url

    raw = str(raw).strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    return normalise_url(raw)


#: Registry mirroring Harvester's per-resource webhook layout.
WEBHOOKS: dict[str, dict[str, Callable[..., Review]]] = {
    "ScrapeJob": {"mutate": mutate, "validate": validate},
}
