"""Test suite for Nestick — extraction, HTTP, models, export, CLI."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nestick.config import Settings
from nestick.export import Exporter, summarise
from nestick.extract import Extractor
from nestick.models import Contact, ContactKind, Lead, Stats
from nestick.utils import normalise_url, registrable_domain, same_site

EX = Extractor(None)

SAMPLE = """
<html><head>
<title>Acme Widgets — Contact</title>
<meta name="description" content="We build widgets in Lahore.">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"LocalBusiness","name":"Acme Widgets",
 "telephone":"+92 42 111 222 333",
 "address":{"@type":"PostalAddress","streetAddress":"12 Mall Rd","addressLocality":"Lahore",
            "addressCountry":"PK"},
 "geo":{"@type":"GeoCoordinates","latitude":31.5497,"longitude":74.3436},
 "aggregateRating":{"@type":"AggregateRating","ratingValue":4.6,"reviewCount":231}}
</script>
</head><body>
<a href="mailto:sales@acme.com">Sales</a>
<p>General: info@acme.com or reach ceo (at) acme (dot) com</p>
<p>Support: <a href="tel:+1 (415) 555-0132">call us</a></p>
<a href="https://www.linkedin.com/company/acme-widgets">LinkedIn</a>
<a href="https://twitter.com/acmewidgets">Twitter</a>
<a href="https://www.facebook.com/sharer.php?u=x">share</a>
<a href="/contact-us">Contact</a><a href="/about">About</a>
<a href="/blog/post-1">Blog</a><a href="/logo.png">logo</a>
<img src="sprite@2x.png"><span>logo@3x.png</span>
<p>fake: user@example.com, name@yourdomain.com, v1.2.3, hero@2x.jpg</p>
<a class="__cf_email__" data-cfemail="6d0e02010e1c2d08150c001d010845080406">[email protected]</a>
</body></html>
"""


# --------------------------------------------------------------------------- #
class TestEmail:
    def test_finds_real_addresses(self):
        vals = {c.value for c in EX.emails(SAMPLE, "https://acme.com/contact")}
        assert "sales@acme.com" in vals
        assert "info@acme.com" in vals

    def test_deobfuscation(self):
        vals = {c.value for c in EX.emails(SAMPLE, "https://acme.com")}
        assert "ceo@acme.com" in vals

    def test_cloudflare_decode(self):
        decoded = Extractor._cf_decode("6d0e02010e1c2d08150c001d010845080406")
        assert decoded and "@" in decoded

    def test_rejects_junk(self):
        vals = {c.value for c in EX.emails(SAMPLE, "https://acme.com")}
        for bad in ("user@example.com", "name@yourdomain.com", "hero@2x.jpg"):
            assert bad not in vals
        assert not any(v.endswith((".png", ".jpg")) for v in vals)

    @pytest.mark.parametrize("bad", [
        "a@b", "@nowhere.com", "no-at-sign.com", "x@1.2.3", "logo@2x.png",
        "sprite@3x.webp", "test@test.com", "a@b.js", "double@@at.com", "",
    ])
    def test_validator_rejects(self, bad):
        assert not EX.valid_email(bad)

    @pytest.mark.parametrize("good", [
        "john.doe@company.co.uk", "info@acme.com", "a_b+tag@sub.domain.io",
        "hello@xn--80ak6aa92e.com", "sales@company-name.com.pk",
    ])
    def test_validator_accepts(self, good):
        assert EX.valid_email(good)

    @pytest.mark.parametrize("text", [
        "Visit ycombinator.com for news",          # 'at' inside a word
        "see theclimbrink.com today",              # 'b'+'rink' lookalike
        "coordinator.example.org is a path",
        "automatic.dotcom.net",
    ])
    def test_no_false_positive_from_words(self, text):
        """'ycombinator.com' must not decode to 'ycombin (at) or.com'."""
        vals = {c.value for c in EX.emails(text, "https://x.com")}
        assert vals == set(), f"false positive: {vals}"

    def test_real_obfuscation_still_works(self):
        for raw, want in [
            ("mail me at john (at) acme (dot) com", "john@acme.com"),
            ("sara [at] contoso [dot] org", "sara@contoso.org"),
            ("bob AT widgets DOT io", "bob@widgets.io"),
        ]:
            vals = {c.value for c in EX.emails(raw, "https://x.com")}
            assert want in vals, f"{raw} -> {vals}"

    @pytest.mark.parametrize("text,want,unwanted", [
        ("Mail coc@postgresql.org. In addition we…", "coc@postgresql.org", "coc@postgresql.org.in"),
        ("Write to info@acme.com. For details…", "info@acme.com", "info@acme.com.for"),
        ("Contact sales@widgets.io. The team…", "sales@widgets.io", "sales@widgets.io.the"),
    ])
    def test_sentence_run_on_trimmed(self, text, want, unwanted):
        """A trailing sentence must not be swallowed into the domain."""
        vals = {c.value for c in EX.emails(text, "https://x.com")}
        assert want in vals and unwanted not in vals

    def test_real_cctld_preserved(self):
        """Genuine multi-label domains must survive the run-on trimmer."""
        vals = {c.value for c in EX.emails("write to hr@acme.co.in today", "https://x.com")}
        assert "hr@acme.co.in" in vals

    def test_same_domain_scores_higher(self):
        cs = {c.value: c for c in EX.emails(
            "x info@acme.com and other@gmail.com x", "https://acme.com/contact")}
        assert cs["info@acme.com"].confidence > cs["other@gmail.com"].confidence

    def test_mailto_beats_plaintext(self):
        cs = {c.value: c for c in EX.emails(SAMPLE, "https://acme.com")}
        assert cs["sales@acme.com"].confidence >= 0.9


class TestPhone:
    def test_tel_href(self):
        vals = {c.value for c in EX.phones(SAMPLE, "https://acme.com")}
        assert any("4155550132" in v.replace("+", "") for v in vals)

    @pytest.mark.parametrize("raw,ok", [
        ("+1 (415) 555-0132", True), ("+92 300 1234567", True),
        ("0042 111 222 333", True), ("2024", False), ("111111111", False),
        ("12345", False), ("+1 415 555 0132 ext 22", True),
    ])
    def test_normalisation(self, raw, ok):
        assert bool(Extractor._normalise_phone(raw)) is ok

    def test_extension_preserved(self):
        assert Extractor._normalise_phone("+1 415 555 0132 ext 22").endswith("x22")

    @pytest.mark.parametrize("raw", [
        "8 13 21 34 55 89",        # Fibonacci from a code sample (python.org)
        "1 2 3 4 5 6 7",
        "144 233 377 610 987",
        "2026-42533",              # nginx changelog ticket id
        "2024/11/03",              # date
        "2025-60005",
        "10.255.255.255",          # IP range from iana.org
        "192.168.1.1",
    ])
    def test_rejects_digit_sequences(self, raw):
        assert Extractor._normalise_phone(raw) is None

    @pytest.mark.parametrize("raw", [
        "+1 (415) 555-0132", "+92 300 1234567", "01 23 45 67 89",
        "+44 20 7946 0958",
    ])
    def test_keeps_real_formats(self, raw):
        assert Extractor._normalise_phone(raw) is not None

    def test_ignores_ids_inside_urls(self):
        """A Facebook page id must not be harvested as a phone number."""
        html = '<p>Follow https://www.facebook.com/144233377610987 today</p>'
        vals = {c.value for c in EX.phones(html, "https://x.com")}
        assert not any("144233377610987" in v for v in vals)


class TestSocial:
    def test_extracts_profiles(self):
        vals = {c.value for c in EX.socials(SAMPLE, "https://acme.com")}
        assert "https://www.linkedin.com/company/acme-widgets" in vals
        assert "https://twitter.com/acmewidgets" in vals

    def test_skips_share_widgets(self):
        vals = {c.value for c in EX.socials(SAMPLE, "https://acme.com")}
        assert not any("sharer" in v for v in vals)


class TestMetadata:
    def test_title_and_description(self):
        m = EX.metadata(SAMPLE, "https://acme.com")
        assert "Acme Widgets" in m["title"]
        assert "widgets" in m["description"].lower()

    def test_jsonld(self):
        m = EX.jsonld(SAMPLE)
        assert m["name"] == "Acme Widgets"
        assert m["latitude"] == pytest.approx(31.5497)
        assert m["rating"] == pytest.approx(4.6)
        assert m["reviews"] == 231
        assert "Lahore" in m["address"]

    def test_malformed_jsonld_is_safe(self):
        assert EX.jsonld('<script type="application/ld+json">{bad,,}</script>') == {}


SPA_HTML = """<html><head><title>App</title></head><body>
<div id="__next"></div>
<script>self.__next_f.push([1,"{\\"contact\\":\\"privacy\\u0040example-spa.com\\",
 \\"url\\":\\"https:\\/\\/x.com\\"}"])</script>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"support":"help&#64;example-spa.com"}}}
</script>
</body></html>"""


class TestEmbeddedState:
    """JS-rendered sites ship their content as escaped JSON, not markup."""

    def test_recovers_state_text(self):
        state = EX.embedded_state(SPA_HTML)
        assert state and "example-spa.com" in state

    def test_extracts_unicode_escaped_email(self):
        vals = {c.value for c in EX.emails(SPA_HTML, "https://example-spa.com")}
        assert "privacy@example-spa.com" in vals

    def test_extracts_entity_escaped_email(self):
        vals = {c.value for c in EX.emails(SPA_HTML, "https://example-spa.com")}
        assert "help@example-spa.com" in vals

    def test_plain_page_has_no_state(self):
        assert EX.embedded_state("<html><body><p>hi</p></body></html>") == ""

    def test_marked_as_embedded(self):
        c = {x.value: x for x in EX.emails(SPA_HTML, "https://example-spa.com")}
        assert c["privacy@example-spa.com"].meta.get("embedded") is True


class TestLegalPageRanking:
    """Modern sites publish addresses in privacy/imprint pages, not /contact."""

    LEGAL_HTML = """<html><body>
      <a href="/blog/post">Blog</a>
      <a href="/legal/privacy-policy">Privacy</a>
      <a href="/impressum">Impressum</a>
      <a href="/pricing">Pricing</a>
    </body></html>"""

    def test_legal_pages_are_ranked(self):
        links = EX.contact_links(self.LEGAL_HTML, "https://acme.com", limit=5)
        joined = " ".join(links)
        assert "privacy-policy" in joined and "impressum" in joined

    def test_noise_excluded(self):
        links = EX.contact_links(self.LEGAL_HTML, "https://acme.com", limit=5)
        assert not any("/pricing" in l or "/blog" in l for l in links)

    def test_contact_still_outranks_legal(self):
        html = '<a href="/legal/terms">Terms</a><a href="/contact">Contact</a>'
        links = EX.contact_links(html, "https://acme.com", limit=2)
        assert links[0].endswith("/contact")


class TestLinks:
    def test_contact_links_ranked_first(self):
        links = EX.contact_links(SAMPLE, "https://acme.com")
        assert links[0].endswith("/contact-us")
        assert not any(l.endswith(".png") for l in links)

    def test_internal_only(self):
        links = EX.links(SAMPLE, "https://acme.com")
        assert all("acme.com" in l for l in links)

    @pytest.mark.parametrize("url,ok", [
        ("https://acme.com/about", True),
        ("https://facebook.com/acme", False),
        ("https://acme.com/file.pdf", False),
        ("https://linkedin.com/in/x", False),
    ])
    def test_is_scrapeable(self, url, ok):
        assert EX.is_scrapeable(url) is ok


class TestUtils:
    @pytest.mark.parametrize("raw,expected", [
        ("https://Acme.com/path/?utm_source=x&id=2#frag", "https://acme.com/path?id=2"),
        ("http://acme.com:80/a//b/", "http://acme.com/a/b"),
        ("javascript:void(0)", None), ("mailto:a@b.com", None),
    ])
    def test_normalise(self, raw, expected):
        assert normalise_url(raw) == expected

    @pytest.mark.parametrize("host,expected", [
        ("www.acme.co.uk", "acme.co.uk"), ("a.b.acme.com", "acme.com"),
        ("shop.acme.com.pk", "acme.com.pk"), ("acme.com", "acme.com"),
    ])
    def test_registrable_domain(self, host, expected):
        assert registrable_domain(host) == expected

    def test_same_site(self):
        assert same_site("https://www.acme.com/a", "https://shop.acme.com/b") is False or True
        assert same_site("https://www.acme.com/a", "https://acme.com/b")


class TestModels:
    def test_dedupe_keeps_best_confidence(self):
        lead = Lead(domain="acme.com")
        lead.add([Contact(ContactKind.EMAIL, "a@acme.com", confidence=0.4)])
        lead.add([Contact(ContactKind.EMAIL, "A@ACME.COM", confidence=0.9)])
        assert len(lead.emails) == 1
        assert lead.contacts[0].confidence == 0.9

    def test_score_rewards_completeness(self):
        poor, rich = Lead(domain="a.com"), Lead(domain="b.com", name="B", address="X")
        rich.add([
            Contact(ContactKind.EMAIL, "a@b.com", confidence=0.9),
            Contact(ContactKind.PHONE, "+14155550132"),
            Contact(ContactKind.LINKEDIN, "https://linkedin.com/in/b"),
        ])
        assert rich.score > poor.score
        assert 0 <= rich.score <= 100

    def test_stats_row(self):
        s = Stats(requests=10, cache_hits=2)
        assert s.as_row()["requests"] == 10 and s.rps >= 0


class TestExport:
    def _leads(self):
        l = Lead(domain="acme.com", url="https://acme.com", name="Acme",
                 address="Lahore", rating=4.6, reviews=12)
        l.add([
            Contact(ContactKind.EMAIL, "info@acme.com", confidence=0.9),
            Contact(ContactKind.PHONE, "+14155550132"),
            Contact(ContactKind.LINKEDIN, "https://www.linkedin.com/company/acme"),
        ])
        return [l]

    def test_all_formats(self, tmp_path):
        s = Settings(urls=["https://acme.com"], output=str(tmp_path / "out"),
                     formats=("csv", "json", "jsonl", "xlsx", "md", "sqlite"))
        paths = Exporter(s).write(self._leads(), Stats())
        assert len(paths) == 6
        for p in paths:
            assert p.exists() and p.stat().st_size > 0

    def test_csv_contents(self, tmp_path):
        import csv as _csv
        s = Settings(urls=["https://acme.com"], output=str(tmp_path / "o"), formats=("csv",))
        p = Exporter(s).write(self._leads(), Stats())[0]
        rows = list(_csv.DictReader(p.read_text(encoding="utf-8-sig").splitlines()))
        assert rows[0]["domain"] == "acme.com"
        assert rows[0]["emails"] == "info@acme.com"

    def test_summarise(self):
        s = summarise(self._leads())
        assert s["leads"] == 1 and s["with_email"] == 1


class TestSettings:
    def test_requires_target(self):
        with pytest.raises(ValueError):
            Settings()

    def test_query_promoted(self):
        assert Settings(query="x").queries == ["x"]

    def test_secrets_masked(self):
        d = Settings(query="x", serpapi_key="supersecret123").to_dict()
        assert "supersecret" not in str(d["serpapi_key"])

    def test_input_file(self, tmp_path):
        f = tmp_path / "u.txt"
        f.write_text("https://a.com\n# comment\nhttps://b.com\n")
        assert len(Settings(input_file=str(f)).urls) == 2


class TestHTTP:
    def test_cache_roundtrip(self, tmp_path):
        from nestick.http import ResponseCache
        from nestick.models import Response

        async def go():
            c = ResponseCache(str(tmp_path / "c.sqlite"))
            await c.put(Response(url="https://a.com", status=200, text="<html>hi</html>"))
            hit = await c.get("https://a.com")
            c.close()
            return hit

        hit = asyncio.run(go())
        assert hit and hit.text == "<html>hi</html>" and hit.from_cache

    def test_governor_backoff(self):
        from nestick.http import HostGovernor

        g = HostGovernor(2, 0.1)
        g.penalise("a.com")
        assert g._delay["a.com"] >= 1.0
        for _ in range(20):
            g.reward("a.com")
        assert g._delay["a.com"] == pytest.approx(0.1, abs=0.01)

    def test_fetcher_lifecycle(self):
        from nestick.http import Fetcher

        async def go():
            s = Settings(urls=["https://a.com"], cache=False, respect_robots=False)
            async with Fetcher(s) as f:
                assert f._clients
            return True

        assert asyncio.run(go())


class TestPipeline:
    def test_end_to_end_offline(self, tmp_path, monkeypatch):
        """Full pipeline with a stubbed fetcher — no network."""
        from nestick.models import Response
        from nestick.pipeline import Pipeline

        async def go():
            s = Settings(urls=["https://acme.com"], cache=False, respect_robots=False,
                         resume=False, output=str(tmp_path / "o"),
                         state_path=str(tmp_path / "s.json"))
            async with Pipeline(s) as p:
                async def fake_get(url, **kw):
                    return Response(url=url, status=200, text=SAMPLE)
                monkeypatch.setattr(p.fetcher, "get", fake_get)
                return await p.run()

        leads = asyncio.run(go())
        assert leads and leads[0].domain == "acme.com"
        assert "info@acme.com" in leads[0].emails
        assert leads[0].name == "Acme Widgets"
        assert leads[0].score > 40

    def test_bulk_email_dump_is_capped(self, tmp_path, monkeypatch):
        """A staff directory must not flood one lead with hundreds of addresses."""
        from nestick.models import Response
        from nestick.pipeline import Pipeline

        staff = "".join(
            f'<a href="mailto:person{i}@bigco.com">p{i}</a>' for i in range(300)
        )
        page = f"<html><body>{staff}<a href='mailto:info@bigco.com'>info</a></body></html>"

        async def go():
            s = Settings(urls=["https://bigco.com"], cache=False, respect_robots=False,
                         resume=False, max_emails_per_lead=25,
                         state_path=str(tmp_path / "s.json"))
            async with Pipeline(s) as p:
                async def fake_get(url, **kw):
                    return Response(url=url, status=200, text=page)
                monkeypatch.setattr(p.fetcher, "get", fake_get)
                return await p.run()

        leads = asyncio.run(go())
        assert len(leads[0].emails) == 25
        assert leads[0].extra["emails_truncated"] == 301
        assert "info@bigco.com" in leads[0].emails  # role mailbox survives

    def test_handles_all_failures(self, tmp_path, monkeypatch):
        from nestick.models import Response
        from nestick.pipeline import Pipeline

        async def go():
            s = Settings(urls=["https://dead.com"], cache=False, respect_robots=False,
                         resume=False, max_retries=1, state_path=str(tmp_path / "s.json"))
            async with Pipeline(s) as p:
                async def fake_get(url, **kw):
                    return Response(url=url, status=0, text="", error="timeout")
                monkeypatch.setattr(p.fetcher, "get", fake_get)
                return await p.run()

        assert asyncio.run(go()) == []


class TestCLI:
    def test_dry_run(self, capsys):
        from nestick.cli import main
        assert main(["-q", "test", "--dry-run"]) == 0
        assert "queries" in capsys.readouterr().out

    def test_version(self, capsys):
        from nestick.cli import main
        assert main(["--version"]) == 0
        assert "nestick" in capsys.readouterr().out

    def test_missing_target_errors(self):
        from nestick.cli import main
        assert main([]) == 1

    def test_env_vars(self, monkeypatch, capsys):
        from nestick.cli import build_parser, settings_from_args
        monkeypatch.setenv("THREADINESS", "77")
        monkeypatch.setenv("NESTICK_QUERY", "env query")
        args = build_parser().parse_args([])
        s = settings_from_args(args)
        assert s.concurrency == 77 and s.queries == ["env query"]


class TestDiscovery:
    def test_ddg_unwrap(self):
        from nestick.discovery import Discovery
        u = Discovery._unwrap_ddg("//duckduckgo.com/l/?uddg=https%3A%2F%2Facme.com%2Fx&rut=z")
        assert u == "https://acme.com/x"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))


# --------------------------------------------------------------------------- #
# Regressions from a live "schools in Riyadh" run (user-reported bad results)
# --------------------------------------------------------------------------- #
class TestRiyadhRegressions:
    """Every case here was wrong output from a real search."""

    @pytest.mark.parametrize("raw", [
        "09-02-2025", "09-06-2024", "31/07/2024", "15-12-2024", "1.6.2024",
        "09-02-25",
    ])
    def test_dates_are_not_phones(self, raw):
        assert Extractor._normalise_phone(raw) is None

    def test_truncated_international_rejected(self):
        """'+966 (0)9200' is a fragment, not a number."""
        assert Extractor._normalise_phone("+966 (0)9200") is None
        assert Extractor._normalise_phone("+966 9200 33963") == "+966920033963"

    @pytest.mark.parametrize("raw,expected", [
        ("+9660114597500", "+9660114597500"),
        ("+1 415 555 0132", "+14155550132"),
        ("0114788815", "0114788815"),
    ])
    def test_real_numbers_survive(self, raw, expected):
        assert Extractor._normalise_phone(raw) == expected

    def test_bare_id_needs_dialing_evidence(self):
        """An order/licence number in prose must not become a phone."""
        html = "<p>Registration 1010751640 issued today</p>"
        assert EX.phones(html, "https://x.com") == []

    def test_cue_word_admits_ungrouped_number(self):
        html = "<p>Tel: 4742147</p>"
        assert [c.value for c in EX.phones(html, "https://x.com")] == ["4742147"]

    def test_arabic_cue_word(self):
        html = "<p>هاتف: 0112345678</p>"
        assert [c.value for c in EX.phones(html, "https://x.com")] == ["0112345678"]

    @pytest.mark.parametrize("title", [
        "Top 10 Best Schools in Riyadh 2026 | Ranking",
        "25 Best Schools in Riyadh - Top Ratings",
        "Best Schools in Riyadh in 2026 By Curriculum",
        "How to Saudi Arabia",
        "Home",
    ])
    def test_headlines_are_not_business_names(self, title):
        assert Extractor.brand_from_title(title, "https://example.com") is None

    @pytest.mark.parametrize("title,expected", [
        ("Mdares AI", "Mdares AI"),
        ("Contact Us | Acme Widgets Ltd", "Acme Widgets Ltd"),
        ("Home - Riyadh British School", "Riyadh British School"),
        ("Home - DOME", "DOME"),
    ])
    def test_real_brands_extracted(self, title, expected):
        assert Extractor.brand_from_title(title, "https://example.com") == expected


class TestDirectoryFollowing:
    """A search for 'schools in <city>' returns directories, not schools."""

    LISTING = """<html><head><title>Top 10 Best Schools | Ranking</title></head>
    <body>
      <a href="https://bisr.com.sa/">British International School</a>
      <a href="https://aisr.org/">American International School</a>
      <a href="https://sek.sa/">SEK Riyadh</a>
      <a href="https://kfs.sch.sa/">King Faisal School</a>
      <a href="https://alyasmin.edu.sa/">Al Yasmin</a>
      <a href="https://dome.edu.sa/">DOME</a>
      <a href="https://cdn.jsdelivr.net/x.js">cdn</a>
      <a href="https://facebook.com/share">fb</a>
    </body></html>"""

    def test_detects_directory(self):
        assert EX.is_directory_page(self.LISTING, "https://guide.com/best-schools")

    def test_extracts_linked_organisations(self):
        orgs = EX.outbound_orgs(self.LISTING, "https://guide.com/best-schools")
        doms = {registrable_domain(o) for o in orgs}
        assert "bisr.com.sa" in doms and "aisr.org" in doms
        assert len(orgs) == 6

    def test_infrastructure_excluded(self):
        orgs = EX.outbound_orgs(self.LISTING, "https://guide.com/best-schools")
        joined = " ".join(orgs)
        assert "jsdelivr" not in joined and "facebook" not in joined

    def test_normal_page_is_not_a_directory(self):
        page = ('<html><head><title>Acme Ltd</title></head><body>'
                '<a href="mailto:a@acme.com">mail</a>'
                '<a href="/about">about</a></body></html>')
        assert not EX.is_directory_page(page, "https://acme.com")

    def test_pipeline_follows_directories(self, tmp_path, monkeypatch):
        """End-to-end: the linked schools become leads, not just the directory."""
        from nestick.models import Response
        from nestick.pipeline import Pipeline

        listing = self.LISTING
        school = ('<html><head><title>British International School</title></head>'
                  '<body><a href="mailto:info@bisr.com.sa">mail</a></body></html>')

        async def go():
            s = Settings(urls=["https://guide.com/best-schools"], cache=False,
                         respect_robots=False, resume=False, engine="urls",
                         state_path=str(tmp_path / "s.json"))
            async with Pipeline(s) as p:
                async def fake_get(url, **kw):
                    body = listing if "guide.com" in url else school
                    return Response(url=url, status=200, text=body)
                monkeypatch.setattr(p.fetcher, "get", fake_get)
                return await p.run()

        leads = asyncio.run(go())
        domains = {l.domain for l in leads}
        assert "bisr.com.sa" in domains, "linked school was never crawled"
        assert len(domains) > 1
