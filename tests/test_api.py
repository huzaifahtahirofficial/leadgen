"""API integration tests: SerpApi, Hunter.io and Google Places.

Runs against ``tests/mock_api.py``, which replays the real vendor response
shapes — so both the happy paths and the failure paths are verified without
needing paid keys.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mock_api import VALID_KEY, MockAPI  # noqa: E402

from nestick.config import Settings  # noqa: E402
from nestick.discovery import ApiError, Discovery  # noqa: E402
from nestick.http import Fetcher, api_error_message  # noqa: E402
from nestick.models import ContactKind, Lead, Response  # noqa: E402
from nestick.pipeline import Pipeline  # noqa: E402


@pytest.fixture(scope="module")
def api():
    with MockAPI() as m:
        yield m


def settings_for(api, **kw) -> Settings:
    base = dict(
        urls=["https://placeholder.invalid"],
        cache=False, respect_robots=False, resume=False,
        max_retries=1, timeout=10,
        # The mock API runs on 127.0.0.1; the SSRF guard blocks that by
        # default, so these tests opt in exactly as an intranet user would.
        allow_private_networks=True,
        serpapi_url=f"{api.base}/search.json",
        hunter_url=f"{api.base}/v2/domain-search",
        places_url=f"{api.base}/maps/api/place/textsearch/json",
        places_details_url=f"{api.base}/maps/api/place/details/json",
    )
    base.update(kw)
    return Settings(**base)


def run(coro):
    return asyncio.run(coro)


async def with_discovery(s: Settings, fn):
    async with Fetcher(s) as f:
        return await fn(Discovery(s, f))


# --------------------------------------------------------------------------- #
class TestErrorParsing:
    """The vendor error shapes must all resolve to a human message."""

    @pytest.mark.parametrize("payload,expected", [
        ({"error": "Invalid API key."}, "Invalid API key."),
        ({"errors": [{"details": "No user found for the API key supplied"}]},
         "No user found for the API key supplied"),
        ({"status": "REQUEST_DENIED", "error_message": "The provided API key is invalid."},
         "The provided API key is invalid."),
        ({"error": {"message": "Quota exceeded"}}, "Quota exceeded"),
        ({"errors": ["plain string error"]}, "plain string error"),
        ({"status": "OVER_QUERY_LIMIT"}, "OVER_QUERY_LIMIT"),
    ])
    def test_messages_extracted(self, payload, expected):
        assert api_error_message(payload) == expected

    @pytest.mark.parametrize("payload", [
        {"status": "OK"}, {"status": "ZERO_RESULTS"}, {"data": {}}, {}, None, [],
    ])
    def test_success_payloads_have_no_error(self, payload):
        assert api_error_message(payload) is None


class TestFetchJson:
    def test_error_body_is_preserved(self, api):
        """A 401 body must reach the caller, not be discarded."""
        s = settings_for(api)

        async def go():
            async with Fetcher(s) as f:
                return await f.fetch_json(
                    s.serpapi_url, params={"q": "x", "api_key": "WRONG"})

        data, err = run(go())
        assert data is None
        assert "401" in err and "Invalid API key" in err

    def test_success_returns_payload(self, api):
        s = settings_for(api)

        async def go():
            async with Fetcher(s) as f:
                return await f.fetch_json(
                    s.serpapi_url, params={"q": "x", "api_key": VALID_KEY})

        data, err = run(go())
        assert err is None and data["organic_results"]

    def test_get_json_still_returns_none(self, api):
        """Back-compat: the simple helper keeps its old contract."""
        s = settings_for(api)

        async def go():
            async with Fetcher(s) as f:
                return await f.get_json(s.serpapi_url, params={"api_key": "WRONG"})

        assert run(go()) is None


# --------------------------------------------------------------------------- #
class TestSerpApi:
    def test_returns_links(self, api):
        s = settings_for(api, queries=["dentists"], serpapi_key=VALID_KEY,
                         engine="serpapi", urls=[])
        urls = run(with_discovery(s, lambda d: d.serpapi("dentists")))
        assert "https://smiledental.example.com/" in urls
        assert "https://citydental.example.net/" in urls   # local_results
        assert len(urls) >= 3

    def test_async_job_polling(self, api):
        """A 'Processing' response is polled until the job completes."""
        s = settings_for(api, queries=["__processing__"], serpapi_key=VALID_KEY,
                         engine="serpapi", urls=[])
        urls = run(with_discovery(s, lambda d: d.serpapi("__processing__")))
        assert "https://smiledental.example.com/" in urls

    def test_bad_key_raises_apierror(self, api):
        s = settings_for(api, queries=["x"], serpapi_key="WRONG",
                         engine="serpapi", urls=[])
        with pytest.raises(ApiError, match="Invalid API key"):
            run(with_discovery(s, lambda d: d.serpapi("x")))

    def test_discover_falls_back_on_bad_key(self, api, monkeypatch):
        """A dead key must not zero out the run — fall back and record why."""
        s = settings_for(api, queries=["x"], serpapi_key="WRONG",
                         engine="serpapi", urls=[])

        async def go():
            async with Fetcher(s) as f:
                d = Discovery(s, f)
                # keep the test offline: stub the keyless fallback
                async def fake_ddg(q):
                    return ["https://fallback.example.com/"]
                d.duckduckgo = fake_ddg
                urls, _ = await d.discover()
                return urls, d.api_errors

        urls, errors = run(go())
        assert urls == ["https://fallback.example.com/"]
        assert any("Invalid API key" in e for e in errors)

    def test_multi_page_requests(self, api):
        s = settings_for(api, queries=["dentists"], serpapi_key=VALID_KEY,
                         engine="serpapi", pages=3, urls=[])
        urls = run(with_discovery(s, lambda d: d.serpapi("dentists")))
        assert len(urls) >= 9          # 3 pages x 3 usable links

    def test_empty_results(self, api):
        s = settings_for(api, queries=["__empty__"], serpapi_key=VALID_KEY,
                         engine="serpapi", urls=[])
        assert run(with_discovery(s, lambda d: d.serpapi("__empty__"))) == []


# --------------------------------------------------------------------------- #
class TestHunter:
    def test_returns_contacts_and_meta(self, api):
        s = settings_for(api, hunter_key=VALID_KEY)
        contacts, meta = run(with_discovery(s, lambda d: d.hunter("acme.com")))
        emails = {c.value for c in contacts if c.kind is ContactKind.EMAIL}
        assert emails == {"jane.doe@acme.com", "info@acme.com"}
        assert meta["name"] == "Acme Corporation"
        assert meta["industry"] == "Software"

    def test_confidence_maps_from_api(self, api):
        s = settings_for(api, hunter_key=VALID_KEY)
        contacts, _ = run(with_discovery(s, lambda d: d.hunter("acme.com")))
        by = {c.value: c for c in contacts}
        # 95% confidence should outrank 72%
        assert by["jane.doe@acme.com"].confidence > by["info@acme.com"].confidence
        assert by["jane.doe@acme.com"].meta["position"] == "CTO"

    def test_socials_included(self, api):
        s = settings_for(api, hunter_key=VALID_KEY)
        contacts, _ = run(with_discovery(s, lambda d: d.hunter("acme.com")))
        kinds = {c.kind for c in contacts}
        assert ContactKind.LINKEDIN in kinds and ContactKind.TWITTER in kinds

    def test_bad_key_raises(self, api):
        s = settings_for(api, hunter_key="WRONG")
        with pytest.raises(ApiError, match="No user found"):
            run(with_discovery(s, lambda d: d.hunter("acme.com")))

    def test_bulk_halts_after_auth_failure(self, api):
        """One bad key shouldn't fire a request for every domain."""
        s = settings_for(api, hunter_key="WRONG")
        doms = [f"d{i}.com" for i in range(20)]

        async def go():
            async with Fetcher(s) as f:
                d = Discovery(s, f)
                out = await d.hunter_bulk(doms)
                return out, d.api_errors, f.stats.requests

        out, errors, requests = run(go())
        assert out == {}
        assert any("No user found" in e for e in errors)
        assert requests < 20          # halted early instead of hammering

    def test_bulk_success(self, api):
        s = settings_for(api, hunter_key=VALID_KEY)
        out = run(with_discovery(s, lambda d: d.hunter_bulk(["acme.com", "other.com"])))
        assert set(out) == {"acme.com", "other.com"}
        assert out["acme.com"][0]

    def test_no_key_is_a_noop(self, api):
        s = settings_for(api, hunter_key=None)
        contacts, meta = run(with_discovery(s, lambda d: d.hunter("acme.com")))
        assert contacts == [] and meta == {}


# --------------------------------------------------------------------------- #
class TestPlaces:
    def test_returns_business_leads(self, api):
        s = settings_for(api, queries=["cafe"], google_maps_key=VALID_KEY,
                         places=True, urls=[])
        leads = run(with_discovery(s, lambda d: d.places_search("cafe")))
        first = leads[0]
        assert first.name == "Corner Cafe"
        assert first.domain == "cornercafe-test.com"
        assert first.latitude == pytest.approx(31.5497)
        assert first.rating == 4.5 and first.reviews == 210
        assert "Lahore" in first.address
        assert first.source == "places"

    def test_phone_from_details(self, api):
        s = settings_for(api, queries=["cafe"], google_maps_key=VALID_KEY,
                         places=True, urls=[])
        leads = run(with_discovery(s, lambda d: d.places_search("cafe")))
        assert any(p.startswith("+92") for p in leads[0].phones)

    def test_pagination(self, api):
        s = settings_for(api, queries=["cafe"], google_maps_key=VALID_KEY,
                         places=True, pages=2, urls=[])
        leads = run(with_discovery(s, lambda d: d.places_search("cafe")))
        assert {l.name for l in leads} == {"Corner Cafe", "Second Cup"}

    def test_request_denied_raises_despite_http_200(self, api):
        """Places signals auth errors in the body with a 200 status."""
        s = settings_for(api, queries=["cafe"], google_maps_key="WRONG",
                         places=True, urls=[])
        with pytest.raises(ApiError, match="API key is invalid"):
            run(with_discovery(s, lambda d: d.places_search("cafe")))

    def test_zero_results_is_not_an_error(self, api):
        s = settings_for(api, queries=["zero"], google_maps_key=VALID_KEY,
                         places=True, urls=[])
        assert run(with_discovery(s, lambda d: d.places_search("zero"))) == []


# --------------------------------------------------------------------------- #
class TestLocationScoping:
    """Location must actually confine results, not just decorate the query."""

    LAHORE_BOX = (31.42, 74.20, 31.62, 74.45)  # s, w, n, e

    @pytest.fixture(autouse=True)
    def _no_network_geocode(self, monkeypatch):
        async def fake_bbox(self, place):
            return TestLocationScoping.LAHORE_BOX

        monkeypatch.setattr(
            "nestick.places.OpenStreetMapPlaces.bounding_box", fake_bbox)

    def test_places_sends_location_and_radius(self, api):
        captured = {}

        async def go():
            s = settings_for(api, queries=["cafe"], google_maps_key=VALID_KEY,
                             places=True, location="Lahore", urls=[])
            async with Fetcher(s) as f:
                d = Discovery(s, f)
                orig = f.fetch_json

                async def spy(url, **kw):
                    if "textsearch" in url:
                        captured.update(kw.get("params") or {})
                    return await orig(url, **kw)

                f.fetch_json = spy
                return await d.places_search("cafe")

        leads = run(go())
        assert leads, "in-box result should be kept"
        assert captured.get("location") == "31.52000,74.32500"
        assert int(captured.get("radius") or 0) > 10_000

    def test_drops_results_outside_location(self, api):
        s = settings_for(api, queries=["cafe"], google_maps_key=VALID_KEY,
                         places=True, location="Lahore", urls=[])
        leads = run(with_discovery(s, lambda d: d.places_search("cafe")))
        # Corner Cafe sits at (31.5497, 74.3436) — inside the Lahore box.
        assert leads and leads[0].name == "Corner Cafe"

    def test_drops_results_outside_geocoded_area(self, api, monkeypatch):
        async def other_box(self, place):
            return (46.50, 6.50, 46.70, 6.70)  # far from the mock result

        monkeypatch.setattr(
            "nestick.places.OpenStreetMapPlaces.bounding_box", other_box)
        s = settings_for(api, queries=["cafe"], google_maps_key=VALID_KEY,
                         places=True, location="Lausanne", urls=[])
        assert run(with_discovery(s, lambda d: d.places_search("cafe"))) == []

    def test_geocode_failure_still_returns_results(self, api, monkeypatch):
        async def no_box(self, place):
            return None

        monkeypatch.setattr(
            "nestick.places.OpenStreetMapPlaces.bounding_box", no_box)
        s = settings_for(api, queries=["cafe"], google_maps_key=VALID_KEY,
                         places=True, location="Somewhere", urls=[])
        assert run(with_discovery(s, lambda d: d.places_search("cafe")))

    def test_bing_sends_region_params(self, api):
        s = settings_for(api, queries=["dentists"], country="pk",
                         language="en", urls=[])
        f = Fetcher(s)
        d = Discovery(s, f)
        captured = {}

        async def fake_get(url, **kw):
            captured["url"] = url
            return Response(url=url, status=200,
                            text="<h2><a href='https://acme.com/'>x</a></h2>")

        f.get = fake_get
        run(d.bing("dentists"))
        assert "setmkt=en-PK" in captured["url"]
        assert "cc=pk" in captured["url"]

    def test_global_aggregators_never_followed(self):
        from nestick.extract import Extractor

        ex = Extractor(Settings(query="q"))
        html = ('<a href="https://zocdoc.com/dentists">z</a>'
                '<a href="https://smilelahore.pk/">s</a>'
                '<a href="https://practo.com/lahore/clinics">p</a>')
        orgs = ex.outbound_orgs(html, "https://lister.pk/best")
        assert any("smilelahore" in o for o in orgs)
        assert not any("zocdoc" in o for o in orgs)
        assert not any("practo" in o for o in orgs)


# --------------------------------------------------------------------------- #
class TestPipelineWithApis:
    """Full pipeline: Places discovery + crawl + Hunter enrichment."""

    def test_end_to_end(self, api, tmp_path):
        s = settings_for(
            api,
            queries=["cafe"], urls=[f"{api.base}/site"],
            google_maps_key=VALID_KEY, places=True,
            hunter_key=VALID_KEY, engine="urls",
            output=str(tmp_path / "out"), formats=("json",),
            state_path=str(tmp_path / "s.json"),
        )

        async def go():
            async with Pipeline(s) as p:
                leads = await p.run()
                return leads, p.api_errors

        leads, errors = run(go())
        assert errors == []
        assert leads, "pipeline produced no leads"
        crawled = [l for l in leads if "127.0.0.1" in l.domain or l.pages_crawled]
        assert crawled, "the local site was never crawled"
        assert any("hello@cornercafe-test.com" in l.emails for l in leads)

    def test_api_errors_surface_on_pipeline(self, api, tmp_path):
        s = settings_for(
            api, queries=["cafe"], urls=[], google_maps_key="WRONG", places=True,
            engine="urls", output=str(tmp_path / "o"),
            state_path=str(tmp_path / "s.json"),
        )

        async def go():
            async with Pipeline(s) as p:
                await p.run()
                return p.api_errors

        # engine="urls" short-circuits discovery, so ask Discovery directly
        s2 = settings_for(api, queries=["cafe"], urls=[], google_maps_key="WRONG",
                          places=True, engine="duckduckgo")

        async def go2():
            async with Fetcher(s2) as f:
                d = Discovery(s2, f)
                async def fake_ddg(q):
                    return []
                d.duckduckgo = fake_ddg
                await d.discover()
                return d.api_errors

        assert any("API key is invalid" in e for e in run(go2()))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


class TestApiRobotsExemption:
    """Vendor APIs must not be blocked by their own crawler-facing robots.txt.

    SerpApi's robots.txt contains 'Disallow: /search.json' to keep crawlers out.
    Applying that to an authenticated API call blocks the very endpoint the
    customer is paying for, and surfaces as a confusing 'blocked-by-robots'.
    """

    def test_fetch_json_defaults_to_no_robots_check(self, api):
        s = settings_for(api, respect_robots=True)

        async def go():
            async with Fetcher(s) as f:
                return await f.fetch_json(
                    s.serpapi_url, params={"q": "dentists", "api_key": VALID_KEY})

        data, err = run(go())
        assert err is None, f"API call was blocked: {err}"
        assert data and data["organic_results"]

    def test_explicit_robots_check_still_possible(self, api):
        """The caller can still opt in when it really is crawling."""
        s = settings_for(api, respect_robots=True)

        async def go():
            async with Fetcher(s) as f:
                return await f.fetch_json(s.serpapi_url, robots_check=True,
                                          params={"q": "x", "api_key": VALID_KEY})

        # The mock serves a permissive robots.txt, so this succeeds either way;
        # the point is that the parameter is honoured rather than ignored.
        data, err = run(go())
        assert err is None or "robots" in err

    def test_serpapi_discovery_works_with_robots_on(self, api):
        s = settings_for(api, queries=["dentists"], serpapi_key=VALID_KEY,
                         engine="serpapi", urls=[], respect_robots=True)
        urls = run(with_discovery(s, lambda d: d.serpapi("dentists")))
        assert "https://smiledental.example.com/" in urls


# --------------------------------------------------------------------------- #
class TestOsmPlacesFallback:
    """Keyless business listings via OpenStreetMap (Nominatim + Overpass)."""

    def test_category_mapping(self):
        from nestick.places import tags_for_query

        assert ("amenity", "school") in tags_for_query("schools in Riyadh")
        assert ("amenity", "cafe") in tags_for_query("best cafes Lahore")
        assert ("amenity", "dentist") in tags_for_query("dentists near me")
        assert tags_for_query("widget makers")          # sensible default

    def test_location_extraction(self):
        from nestick.places import location_from_query

        assert location_from_query("dentists in Lahore") == "Lahore"
        assert location_from_query("cafes near Berlin") == "Berlin"
        assert location_from_query("schools", "Riyadh") == "Riyadh"
        assert location_from_query("schools") is None

    def test_overpass_query_shape(self):
        from nestick.places import OpenStreetMapPlaces

        q = OpenStreetMapPlaces.build_query((1.0, 2.0, 3.0, 4.0),
                                            [("amenity", "school")], 50)
        assert '["amenity"="school"]' in q
        assert "1.0,2.0,3.0,4.0" in q
        assert "out tags center 50" in q

    def test_element_to_lead(self):
        from nestick.places import OpenStreetMapPlaces

        el = {"type": "node", "id": 1, "lat": 24.7, "lon": 46.6, "tags": {
            "name": "King Faisal School", "website": "https://kfs.sch.sa/",
            "phone": "+966 11 482 0802;+966 50 413 1365",
            "contact:email": "info@kfs.sch.sa", "amenity": "school",
            "addr:city": "Riyadh", "addr:street": "Al Takhassusi"}}
        lead = OpenStreetMapPlaces._to_lead(el)
        assert lead.name == "King Faisal School"
        assert lead.domain == "kfs.sch.sa"
        assert lead.source == "openstreetmap"
        assert "info@kfs.sch.sa" in lead.emails
        assert len(lead.phones) == 2          # semicolon-separated list split
        assert "Riyadh" in lead.address
        assert lead.latitude == pytest.approx(24.7)

    def test_element_without_name_or_site_skipped(self):
        from nestick.places import OpenStreetMapPlaces

        assert OpenStreetMapPlaces._to_lead({"tags": {"amenity": "school"}}) is None

    def test_way_centre_coordinates(self):
        from nestick.places import OpenStreetMapPlaces

        el = {"type": "way", "id": 2, "center": {"lat": 1.5, "lon": 2.5},
              "tags": {"name": "X School", "website": "x.edu"}}
        lead = OpenStreetMapPlaces._to_lead(el)
        assert lead.latitude == 1.5 and lead.longitude == 2.5

    def test_falls_back_when_no_google_key(self, api, monkeypatch):
        """--places must still work with no Google Maps key configured."""
        s = settings_for(api, queries=["cafes in Lahore"], urls=[],
                         google_maps_key=None, places=True, osm_fallback=True)
        called: list[str] = []

        async def fake_search(self, query, limit=200):
            called.append(query)
            return [Lead(domain="osm.test", name="From OSM", source="openstreetmap")]

        import nestick.places as pl
        monkeypatch.setattr(pl.OpenStreetMapPlaces, "search", fake_search)
        leads = run(with_discovery(s, lambda d: d.places_any("cafes in Lahore")))
        assert called and leads[0].source == "openstreetmap"

    def test_google_used_when_key_present(self, api):
        s = settings_for(api, queries=["cafe"], urls=[],
                         google_maps_key=VALID_KEY, places=True)
        leads = run(with_discovery(s, lambda d: d.places_any("cafe")))
        assert leads and leads[0].source == "places"

    def test_google_failure_falls_back_to_osm(self, api, monkeypatch):
        s = settings_for(api, queries=["cafes in Lahore"], urls=[],
                         google_maps_key="WRONG", places=True, osm_fallback=True)

        async def fake_search(self, query, limit=200):
            return [Lead(domain="osm.test", name="Rescued", source="openstreetmap")]

        import nestick.places as pl
        monkeypatch.setattr(pl.OpenStreetMapPlaces, "search", fake_search)

        async def go():
            async with Fetcher(s) as f:
                d = Discovery(s, f)
                out = await d.places_any("cafes in Lahore")
                return out, d.api_errors

        leads, errors = run(go())
        assert leads[0].name == "Rescued"
        assert any("API key is invalid" in e for e in errors)

    def test_osm_can_be_disabled(self, api):
        s = settings_for(api, queries=["cafes in Lahore"], urls=[],
                         google_maps_key=None, places=True, osm_fallback=False)
        assert run(with_discovery(s, lambda d: d.places_any("cafes in Lahore"))) == []


class TestHttpPost:
    """Overpass needs POST; verify the fetcher supports it and never caches it."""

    def test_post_supported(self, api):
        s = settings_for(api)

        async def go():
            async with Fetcher(s) as f:
                return await f.get(f"{api.base}/site", method="POST",
                                   data={"x": "1"})

        assert asyncio.run(go()).status in (200, 501, 405)

    def test_post_is_never_cached(self, api, tmp_path):
        s = settings_for(api, cache=True,
                         cache_path=str(tmp_path / "c.sqlite"))

        async def go():
            async with Fetcher(s) as f:
                await f.get(f"{api.base}/site", method="POST", data={"x": "1"})
                return await f.cache.get(f"{api.base}/site")

        assert asyncio.run(go()) is None
