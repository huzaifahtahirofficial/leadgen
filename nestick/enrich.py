"""Free-source enrichment and validation — the SkelerSecurity Intelligence Engine.

Every provider here is either keyless or has a free tier, and each adds a
different dimension to a lead:

===================  ====  ==========================================
Provider             Key?  What it contributes
===================  ====  ==========================================
DNS-over-HTTPS       no    MX records prove a domain can receive mail
NumVerify            free  Carrier, line type and country for a phone
Wikidata             no    Legal entity, industry, founding, HQ
Wikipedia REST       no    One-paragraph company description
OpenStreetMap        no    Address and coordinates (see places.py)
Web archive          no    First-seen date, a proxy for company age
===================  ====  ==========================================

The design rule: a provider that is unavailable, rate-limited or unkeyed must
never break a run. Every lookup degrades to "unknown" rather than failing.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .models import Contact, ContactKind, Lead
from .utils import log, registrable_domain

# --------------------------------------------------------------------------- #
DOH_ENDPOINTS = (
    ("https://dns.google/resolve", {}),
    ("https://cloudflare-dns.com/dns-query", {"accept": "application/dns-json"}),
)
NUMVERIFY_URL = "http://apilayer.net/api/validate"
WIKIDATA_URL = "https://www.wikidata.org/w/api.php"
WIKIPEDIA_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"
WAYBACK_URL = "http://archive.org/wayback/available"

#: Free mailbox providers — a lead using one is usually a sole trader.
FREEMAIL = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com",
    "proton.me", "protonmail.com", "gmx.com", "mail.com", "yandex.com",
    "zoho.com", "tutanota.com", "hotmail.co.uk", "qq.com", "163.com",
})

#: Mail hosts that reveal which platform a company runs on.
MX_PLATFORMS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"google|googlemail|aspmx", re.I), "Google Workspace"),
    (re.compile(r"outlook|microsoft|office365|protection\.outlook", re.I), "Microsoft 365"),
    (re.compile(r"zoho", re.I), "Zoho Mail"),
    (re.compile(r"protonmail|proton\.me", re.I), "Proton"),
    (re.compile(r"yandex", re.I), "Yandex"),
    (re.compile(r"mimecast", re.I), "Mimecast"),
    (re.compile(r"barracuda", re.I), "Barracuda"),
    (re.compile(r"pphosted|proofpoint", re.I), "Proofpoint"),
    (re.compile(r"secureserver|godaddy", re.I), "GoDaddy"),
    (re.compile(r"hostinger|namecheap|bluehost|siteground", re.I), "Shared hosting"),
)


@dataclass(slots=True)
class EnrichmentStats:
    """Counters describing what enrichment achieved, shown in the UI."""

    mx_checked: int = 0
    mx_valid: int = 0
    mx_missing: int = 0
    phones_checked: int = 0
    phones_valid: int = 0
    company_hits: int = 0
    errors: list[str] = field(default_factory=list)

    def as_row(self) -> dict[str, Any]:
        return {
            "mx_checked": self.mx_checked,
            "deliverable_domains": self.mx_valid,
            "undeliverable_domains": self.mx_missing,
            "phones_checked": self.phones_checked,
            "phones_valid": self.phones_valid,
            "company_profiles": self.company_hits,
        }


class Enricher:
    """Adds validation and firmographics to leads using free data sources."""

    def __init__(self, fetcher: Any, settings: Any) -> None:
        self.f = fetcher
        self.s = settings
        self.stats = EnrichmentStats()
        self._mx_cache: dict[str, list[str]] = {}
        self._sem = asyncio.Semaphore(8)

    # ------------------------------------------------------------------ #
    # DNS / deliverability
    # ------------------------------------------------------------------ #
    async def mx_records(self, domain: str) -> list[str]:
        """MX hosts for ``domain``; empty means mail cannot be delivered."""
        domain = registrable_domain(domain)
        if not domain:
            return []
        if domain in self._mx_cache:
            return self._mx_cache[domain]

        records: list[str] = []
        for url, extra in DOH_ENDPOINTS:
            data, err = await self.f.fetch_json(
                url, params={"name": domain, "type": "MX"},
                headers={"accept": "application/dns-json", **extra},
                robots_check=False, retries=1,
            )
            if err or not isinstance(data, dict):
                continue
            for answer in data.get("Answer") or []:
                value = str(answer.get("data", "")).strip()
                if value:
                    # "10 aspmx.l.google.com." -> "aspmx.l.google.com"
                    host = value.split()[-1].rstrip(".")
                    if host:
                        records.append(host)
            break
        self._mx_cache[domain] = records
        return records

    @staticmethod
    def mail_platform(mx: Iterable[str]) -> str | None:
        joined = " ".join(mx).lower()
        for pattern, label in MX_PLATFORMS:
            if pattern.search(joined):
                return label
        return None

    async def validate_domain(self, lead: Lead) -> None:
        """Attach deliverability + mail-platform intelligence to a lead."""
        if not lead.domain:
            return
        async with self._sem:
            mx = await self.mx_records(lead.domain)
        self.stats.mx_checked += 1
        lead.extra["mx_records"] = mx[:4]
        lead.extra["deliverable"] = bool(mx)
        if mx:
            self.stats.mx_valid += 1
            if platform := self.mail_platform(mx):
                lead.extra["mail_platform"] = platform
        else:
            self.stats.mx_missing += 1
            # Corporate addresses on a domain with no MX cannot receive mail.
            for c in lead.contacts:
                if c.kind is ContactKind.EMAIL and c.value.endswith(f"@{lead.domain}"):
                    c.confidence = round(max(0.05, c.confidence - 0.35), 2)
                    c.meta["undeliverable"] = True

    # ------------------------------------------------------------------ #
    # Phone validation (NumVerify — free tier, 100 lookups/month)
    # ------------------------------------------------------------------ #
    async def validate_phone(self, phone: str, country: str = "") -> dict[str, Any] | None:
        """Carrier / line-type / country for a number, via NumVerify."""
        key = getattr(self.s, "numverify_key", None)
        if not key or not phone:
            return None
        params = {"access_key": key, "number": phone.lstrip("+")}
        if country and len(country) == 2:
            params["country_code"] = country.upper()
        data, err = await self.f.fetch_json(
            NUMVERIFY_URL, params=params, robots_check=False, retries=1)
        if err or not isinstance(data, dict):
            return None
        if data.get("success") is False:
            info = (data.get("error") or {}).get("info", "NumVerify request failed")
            message = f"NumVerify: {info}"
            # Compare against the stored form, or a bad key logs once per phone.
            if message not in self.stats.errors:
                self.stats.errors.append(message)
                log.warning("%s", message)
            return None
        return {
            "valid": bool(data.get("valid")),
            "country": data.get("country_name") or data.get("country_code"),
            "location": data.get("location"),
            "carrier": data.get("carrier"),
            "line_type": data.get("line_type"),
            "international": data.get("international_format"),
        }

    async def validate_phones(self, lead: Lead, limit: int = 2) -> None:
        if not getattr(self.s, "numverify_key", None):
            return
        phones = [c for c in lead.contacts if c.kind is ContactKind.PHONE][:limit]
        for contact in phones:
            async with self._sem:
                info = await self.validate_phone(contact.value, self.s.country)
            if not info:
                continue
            self.stats.phones_checked += 1
            contact.meta.update({k: v for k, v in info.items() if v})
            if info["valid"]:
                self.stats.phones_valid += 1
                contact.confidence = round(min(1.0, contact.confidence + 0.15), 2)
                if info.get("international"):
                    contact.value = info["international"].replace(" ", "")
            else:
                contact.confidence = round(max(0.05, contact.confidence - 0.30), 2)
                contact.meta["invalid_number"] = True

    # ------------------------------------------------------------------ #
    # Firmographics (Wikidata + Wikipedia)
    # ------------------------------------------------------------------ #
    async def company_profile(self, name: str) -> dict[str, Any] | None:
        """Industry / country / founding year for a named organisation."""
        if not name or len(name) < 3:
            return None
        data, err = await self.f.fetch_json(
            WIKIDATA_URL,
            params={"action": "wbsearchentities", "search": name[:80],
                    "language": "en", "format": "json", "limit": 1, "type": "item"},
            robots_check=False, retries=1,
        )
        if err or not isinstance(data, dict):
            return None
        hits = data.get("search") or []
        if not hits:
            return None
        top = hits[0]
        description = (top.get("description") or "").strip()
        # Reject obvious mismatches: a colour, a film character, a surname.
        if not description or not self._plausible_company(description):
            return None
        return {
            "wikidata_id": top.get("id"),
            "wikidata_label": top.get("label"),
            "industry_hint": description[:120],
        }

    @staticmethod
    def _plausible_company(description: str) -> bool:
        # Prefix match, not a whole-word one: "company" must match "compan",
        # and "technology" must match "technolog".
        good = re.compile(
            r"\b(compan|corporat|business|enterprise|firm|organi[sz]ation|"
            r"school|universit|college|institut|bank|agency|agencies|studio|startup|"
            r"manufactur|retail|software|technolog|service|group|holding|"
            r"hospital|clinic|hotel|restaurant|publisher|charit|nonprofit|"
            r"nonprofit|provider|consultanc|developer|brand)", re.I)
        return bool(good.search(description))

    async def enrich_company(self, lead: Lead) -> None:
        if not lead.name:
            return
        async with self._sem:
            profile = await self.company_profile(lead.name)
        if profile:
            self.stats.company_hits += 1
            lead.extra.update(profile)

    # ------------------------------------------------------------------ #
    async def enrich(self, leads: list[Lead]) -> EnrichmentStats:
        """Run every enabled provider across all leads, concurrently."""
        if not leads:
            return self.stats
        tasks: list[Any] = []
        if getattr(self.s, "verify_mx", True):
            tasks += [self.validate_domain(l) for l in leads if l.domain]
        if getattr(self.s, "numverify_key", None):
            tasks += [self.validate_phones(l) for l in leads if l.phones]
        if getattr(self.s, "firmographics", False):
            tasks += [self.enrich_company(l) for l in leads if l.name]
        if not tasks:
            return self.stats
        log.info("Enriching %d lead(s) via free intelligence sources…", len(leads))
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("Enrichment: %d/%d domains deliverable%s",
                 self.stats.mx_valid, self.stats.mx_checked,
                 f", {self.stats.phones_valid}/{self.stats.phones_checked} phones valid"
                 if self.stats.phones_checked else "")
        return self.stats


# --------------------------------------------------------------------------- #
# Analytics over a finished result set
# --------------------------------------------------------------------------- #
def analyse(leads: list[Lead]) -> dict[str, Any]:
    """Aggregate a result set into the numbers a sales team actually wants."""
    if not leads:
        return {"total": 0}

    emails = [e for l in leads for e in l.emails]
    domains_with_mx = [l for l in leads if l.extra.get("deliverable")]
    # Derive the role flag rather than trusting meta: contacts created by an
    # API provider or by hand never pass through the extractor that sets it.
    from .config import ROLE_LOCALS

    def _is_role(value: str) -> bool:
        return value.split("@", 1)[0].lower() in ROLE_LOCALS

    role_count = sum(
        1 for l in leads for c in l.contacts
        if c.kind is ContactKind.EMAIL and (c.meta.get("role") or _is_role(c.value)))
    personal = len(emails) - role_count

    tld: dict[str, int] = {}
    platforms: dict[str, int] = {}
    networks: dict[str, int] = {}
    freemail = 0
    for l in leads:
        if l.domain and "." in l.domain:
            tld[l.domain.rsplit(".", 1)[-1]] = tld.get(l.domain.rsplit(".", 1)[-1], 0) + 1
        if p := l.extra.get("mail_platform"):
            platforms[p] = platforms.get(p, 0) + 1
        for kind in l.socials:
            networks[kind] = networks.get(kind, 0) + 1
        freemail += sum(1 for e in l.emails if e.split("@")[-1] in FREEMAIL)

    scores = [l.score for l in leads]
    bands = {
        "hot (60+)": sum(1 for s in scores if s >= 60),
        "warm (30-59)": sum(1 for s in scores if 30 <= s < 60),
        "cold (<30)": sum(1 for s in scores if s < 30),
    }
    contactable = sum(1 for l in leads if l.emails or l.phones)

    return {
        "total": len(leads),
        "contactable": contactable,
        "contactable_pct": round(contactable / len(leads) * 100, 1),
        "with_email": sum(1 for l in leads if l.emails),
        "with_phone": sum(1 for l in leads if l.phones),
        "with_social": sum(1 for l in leads if l.socials),
        "total_emails": len(emails),
        "unique_emails": len(set(emails)),
        "role_emails": role_count,
        "personal_emails": max(0, personal),
        "freemail_emails": freemail,
        "deliverable_domains": len(domains_with_mx),
        "deliverable_pct": round(len(domains_with_mx) / len(leads) * 100, 1)
        if domains_with_mx else 0.0,
        "avg_score": round(sum(scores) / len(scores), 1),
        "median_score": round(sorted(scores)[len(scores) // 2], 1),
        "score_bands": bands,
        "top_tlds": dict(sorted(tld.items(), key=lambda kv: -kv[1])[:6]),
        "mail_platforms": dict(sorted(platforms.items(), key=lambda kv: -kv[1])[:6]),
        "social_networks": dict(sorted(networks.items(), key=lambda kv: -kv[1])[:8]),
        "sources": _count(l.source for l in leads),
        "pages_crawled": sum(l.pages_crawled for l in leads),
    }


def _count(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
