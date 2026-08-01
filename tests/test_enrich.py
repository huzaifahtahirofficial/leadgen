"""Tests for the free-source enrichment layer and run analytics."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nestick.config import Settings  # noqa: E402
from nestick.enrich import Enricher, analyse  # noqa: E402
from nestick.models import Contact, ContactKind, Lead  # noqa: E402


class FakeFetcher:
    """Stands in for the HTTP layer so tests never touch the network."""

    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.calls: list[str] = []

    async def fetch_json(self, url, **kw):
        self.calls.append(url)
        for key, value in self.responses.items():
            if key in url:
                if isinstance(value, Exception):
                    return None, str(value)
                return value, None
        return None, "not-stubbed"


def lead_with(domain="acme.com", emails=(), phones=(), name=None) -> Lead:
    l = Lead(domain=domain, url=f"https://{domain}", name=name)
    l.add([Contact(ContactKind.EMAIL, e, confidence=0.8) for e in emails])
    l.add([Contact(ContactKind.PHONE, p, confidence=0.6) for p in phones])
    return l


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
class TestMxValidation:
    MX_OK = {"Status": 0, "Answer": [
        {"data": "10 aspmx.l.google.com."},
        {"data": "20 alt1.aspmx.l.google.com."}]}
    MX_NONE = {"Status": 3, "Answer": []}

    def test_parses_mx_hosts(self):
        f = FakeFetcher({"dns": self.MX_OK})
        e = Enricher(f, Settings(urls=["https://a.com"]))
        assert run(e.mx_records("acme.com")) == [
            "aspmx.l.google.com", "alt1.aspmx.l.google.com"]

    def test_marks_domain_deliverable(self):
        f = FakeFetcher({"dns": self.MX_OK})
        e = Enricher(f, Settings(urls=["https://a.com"]))
        lead = lead_with(emails=["info@acme.com"])
        run(e.validate_domain(lead))
        assert lead.extra["deliverable"] is True
        assert lead.extra["mail_platform"] == "Google Workspace"
        assert e.stats.mx_valid == 1

    def test_penalises_undeliverable_domain(self):
        f = FakeFetcher({"dns": self.MX_NONE})
        e = Enricher(f, Settings(urls=["https://a.com"]))
        lead = lead_with(emails=["info@acme.com"])
        before = lead.contacts[0].confidence
        run(e.validate_domain(lead))
        assert lead.extra["deliverable"] is False
        assert lead.contacts[0].confidence < before
        assert lead.contacts[0].meta["undeliverable"] is True

    def test_caches_per_domain(self):
        f = FakeFetcher({"dns": self.MX_OK})
        e = Enricher(f, Settings(urls=["https://a.com"]))
        run(e.mx_records("acme.com"))
        run(e.mx_records("acme.com"))
        assert len(f.calls) == 1

    @pytest.mark.parametrize("host,expected", [
        (["aspmx.l.google.com"], "Google Workspace"),
        (["acme-com.mail.protection.outlook.com"], "Microsoft 365"),
        (["mx.zoho.com"], "Zoho Mail"),
        (["mxa-001.gslb.pphosted.com"], "Proofpoint"),
        (["mail.self-hosted.example"], None),
    ])
    def test_platform_detection(self, host, expected):
        assert Enricher.mail_platform(host) == expected

    def test_network_failure_is_survivable(self):
        f = FakeFetcher({})            # nothing stubbed -> error for every call
        e = Enricher(f, Settings(urls=["https://a.com"]))
        lead = lead_with(emails=["info@acme.com"])
        run(e.validate_domain(lead))
        assert lead.extra["deliverable"] is False


class TestNumVerify:
    OK = {"valid": True, "number": "14155552671", "country_name": "United States",
          "location": "Novato", "carrier": "AT&T Mobility LLC",
          "line_type": "mobile", "international_format": "+14155552671"}
    BAD_KEY = {"success": False, "error": {"code": 101, "type": "invalid_access_key",
                                           "info": "You have not supplied a valid API Access Key."}}

    def test_skipped_without_key(self):
        f = FakeFetcher({"apilayer": self.OK})
        e = Enricher(f, Settings(urls=["https://a.com"]))
        assert run(e.validate_phone("+14155552671")) is None
        assert not f.calls

    def test_returns_carrier_details(self):
        f = FakeFetcher({"apilayer": self.OK})
        e = Enricher(f, Settings(urls=["https://a.com"], numverify_key="K"))
        info = run(e.validate_phone("+14155552671"))
        assert info["valid"] and info["carrier"].startswith("AT&T")
        assert info["line_type"] == "mobile"

    def test_boosts_valid_phone_confidence(self):
        f = FakeFetcher({"apilayer": self.OK})
        e = Enricher(f, Settings(urls=["https://a.com"], numverify_key="K"))
        lead = lead_with(phones=["14155552671"])
        before = lead.contacts[0].confidence
        run(e.validate_phones(lead))
        assert lead.contacts[0].confidence > before
        assert lead.contacts[0].meta["carrier"].startswith("AT&T")

    def test_invalid_number_penalised(self):
        f = FakeFetcher({"apilayer": {**self.OK, "valid": False}})
        e = Enricher(f, Settings(urls=["https://a.com"], numverify_key="K"))
        lead = lead_with(phones=["1234567"])
        before = lead.contacts[0].confidence
        run(e.validate_phones(lead))
        assert lead.contacts[0].confidence < before
        assert lead.contacts[0].meta["invalid_number"] is True

    def test_bad_key_recorded_once(self):
        f = FakeFetcher({"apilayer": self.BAD_KEY})
        e = Enricher(f, Settings(urls=["https://a.com"], numverify_key="WRONG"))
        run(e.validate_phone("+1415"))
        run(e.validate_phone("+1416"))
        assert len(e.stats.errors) == 1
        assert "NumVerify" in e.stats.errors[0]


class TestFirmographics:
    HIT = {"search": [{"id": "Q7624104", "label": "Stripe",
                       "description": "Irish-American payment technology company"}]}
    MISS = {"search": [{"id": "Q3421342", "label": "stripe",
                        "description": "long, narrow band of colour"}]}

    def test_accepts_company_description(self):
        f = FakeFetcher({"wikidata": self.HIT})
        e = Enricher(f, Settings(urls=["https://a.com"]))
        out = run(e.company_profile("Stripe"))
        assert out["wikidata_id"] == "Q7624104"

    def test_rejects_irrelevant_match(self):
        f = FakeFetcher({"wikidata": self.MISS})
        e = Enricher(f, Settings(urls=["https://a.com"]))
        assert run(e.company_profile("Stripe")) is None

    def test_short_names_skipped(self):
        f = FakeFetcher({"wikidata": self.HIT})
        e = Enricher(f, Settings(urls=["https://a.com"]))
        assert run(e.company_profile("AB")) is None
        assert not f.calls


class TestEnrichOrchestration:
    def test_runs_only_enabled_providers(self):
        f = FakeFetcher({"dns": TestMxValidation.MX_OK})
        s = Settings(urls=["https://a.com"], verify_mx=True, firmographics=False)
        e = Enricher(f, s)
        run(e.enrich([lead_with(emails=["a@acme.com"])]))
        assert all("dns" in c for c in f.calls)

    def test_no_leads_is_a_noop(self):
        f = FakeFetcher({})
        e = Enricher(f, Settings(urls=["https://a.com"]))
        assert run(e.enrich([])).mx_checked == 0


# --------------------------------------------------------------------------- #
class TestAnalytics:
    def _set(self):
        a = lead_with("acme.com", ["info@acme.com", "jane@acme.com"], ["+14155550132"],
                      name="Acme")
        a.extra["deliverable"] = True
        a.extra["mail_platform"] = "Google Workspace"
        a.add([Contact(ContactKind.LINKEDIN, "https://linkedin.com/company/acme")])
        b = lead_with("beta.co.uk", ["hello@gmail.com"], name="Beta")
        b.extra["deliverable"] = False
        c = Lead(domain="gamma.io", name="Gamma")
        return [a, b, c]

    def test_headline_numbers(self):
        r = analyse(self._set())
        assert r["total"] == 3
        assert r["contactable"] == 2
        assert r["with_email"] == 2 and r["with_phone"] == 1

    def test_email_quality_split(self):
        r = analyse(self._set())
        assert r["role_emails"] >= 1          # info@
        assert r["freemail_emails"] == 1      # the gmail one

    def test_deliverability(self):
        r = analyse(self._set())
        assert r["deliverable_domains"] == 1

    def test_score_bands_sum_to_total(self):
        r = analyse(self._set())
        assert sum(r["score_bands"].values()) == r["total"]

    def test_breakdowns_present(self):
        r = analyse(self._set())
        assert "co.uk" in r["top_tlds"] or "com" in r["top_tlds"]
        assert r["mail_platforms"]["Google Workspace"] == 1
        assert r["social_networks"]["linkedin"] == 1

    def test_empty_input(self):
        assert analyse([])["total"] == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
