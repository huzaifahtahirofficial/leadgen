"""Tests for the browser control panel (server + JSON API)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nestick.web import server as web


# --------------------------------------------------------------------------- #
# Unit-level: settings mapping and config storage
# --------------------------------------------------------------------------- #
class TestFormMapping:
    def _jm(self, tmp_path, monkeypatch):
        monkeypatch.setattr(web, "CONFIG_FILE", tmp_path / "config.json")
        monkeypatch.setattr(web, "CONFIG_DIR", tmp_path)
        return web.JobManager()

    def test_urls_split_on_newlines_and_commas(self, tmp_path, monkeypatch):
        jm = self._jm(tmp_path, monkeypatch)
        s = jm._settings_from_form({"urls": "https://a.com, https://b.com\nhttps://c.com"})
        assert len(s.urls) == 3

    def test_multiline_queries(self, tmp_path, monkeypatch):
        jm = self._jm(tmp_path, monkeypatch)
        s = jm._settings_from_form({"query": "dentists lahore\nplumbers london"})
        assert s.queries == ["dentists lahore", "plumbers london"]

    def test_empty_form_rejected(self, tmp_path, monkeypatch):
        jm = self._jm(tmp_path, monkeypatch)
        with pytest.raises(ValueError):
            jm._settings_from_form({"query": "  ", "urls": ""})

    def test_numeric_bounds_clamped(self, tmp_path, monkeypatch):
        jm = self._jm(tmp_path, monkeypatch)
        s = jm._settings_from_form({"urls": "https://a.com", "pages": "999",
                                    "concurrency": "9999", "depth": "77"})
        assert s.pages == 20 and s.concurrency == 128 and s.depth == 3

    def test_bad_numbers_fall_back(self, tmp_path, monkeypatch):
        jm = self._jm(tmp_path, monkeypatch)
        s = jm._settings_from_form({"urls": "https://a.com", "pages": "abc",
                                    "concurrency": ""})
        assert s.pages == 1 and s.concurrency == 24

    def test_want_checkboxes(self, tmp_path, monkeypatch):
        jm = self._jm(tmp_path, monkeypatch)
        s = jm._settings_from_form({"urls": "https://a.com", "want_email": True})
        assert s.want == ("email",)
        s2 = jm._settings_from_form({"urls": "https://a.com"})
        assert set(s2.want) == {"email", "phone", "social"}  # none ticked -> all

    def test_stored_keys_used_as_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setattr(web, "CONFIG_FILE", tmp_path / "config.json")
        monkeypatch.setattr(web, "CONFIG_DIR", tmp_path)
        web.save_config({"hunter_key": "stored-key"})
        jm = web.JobManager()
        s = jm._settings_from_form({"urls": "https://a.com"})
        assert s.hunter_key == "stored-key"
        s2 = jm._settings_from_form({"urls": "https://a.com", "hunter_key": "form-key"})
        assert s2.hunter_key == "form-key"  # form wins


class TestConfigStore:
    def test_roundtrip_and_filtering(self, tmp_path, monkeypatch):
        monkeypatch.setattr(web, "CONFIG_FILE", tmp_path / "c.json")
        monkeypatch.setattr(web, "CONFIG_DIR", tmp_path)
        web.save_config({"hunter_key": " k1 ", "evil": "drop-me"})
        cfg = web.load_config()
        assert cfg == {"hunter_key": "k1"}   # trimmed, non-secrets discarded

    def test_missing_file_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(web, "CONFIG_FILE", tmp_path / "nope.json")
        assert web.load_config() == {}


class TestRingLog:
    def test_incremental_reads(self):
        h = web.RingLogHandler(limit=5)
        import logging
        for i in range(3):
            h.emit(logging.LogRecord("x", logging.INFO, "f", 1, f"m{i}", None, None))
        lines, idx = h.since(0)
        assert len(lines) == 3 and idx == 3
        lines2, idx2 = h.since(idx)
        assert lines2 == [] and idx2 == 3

    def test_ring_is_bounded(self):
        import logging
        h = web.RingLogHandler(limit=4)
        for i in range(20):
            h.emit(logging.LogRecord("x", logging.INFO, "f", 1, f"m{i}", None, None))
        assert len(h.lines) <= 4


# --------------------------------------------------------------------------- #
# Integration: a real server process driven over HTTP
# --------------------------------------------------------------------------- #
PORT = 8837
BASE = f"http://127.0.0.1:{PORT}"


def _get(path: str, timeout: float = 15):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode())


def _post(path: str, payload: dict, timeout: float = 15):
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    env_dir = tmp_path_factory.mktemp("cfg")
    import os

    env = {**os.environ, "NESTICK_CONFIG_DIR": str(env_dir)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "nestick", "ui", "--no-browser", "--ui-port", str(PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )
    for _ in range(40):
        time.sleep(0.25)
        try:
            _get("/healthz", timeout=2)
            break
        except Exception:
            continue
    else:
        proc.kill()
        pytest.skip("UI server did not start")
    yield proc
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


class TestServerRoutes:
    def test_healthz(self, server):
        assert _get("/healthz")["status"] == "ok"

    def test_dashboard_and_assets(self, server):
        for path, needle in [("/", b"Nestick"), ("/static/app.css", b":root"),
                             ("/static/app.js", b"api/start")]:
            with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as r:
                assert r.status == 200 and needle in r.read()

    def test_status_shape(self, server):
        st = _get("/api/status")
        for key in ("running", "finished", "stats", "summary", "leads", "log"):
            assert key in st

    def test_empty_start_rejected(self, server):
        with pytest.raises(urllib.error.HTTPError) as e:
            _post("/api/start", {})
        assert e.value.code == 400

    def test_path_traversal_blocked(self, server):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{BASE}/static/../../../etc/passwd", timeout=5)
        assert e.value.code in (403, 404)

    def test_unknown_route_404(self, server):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{BASE}/api/nope", timeout=5)
        assert e.value.code == 404

    def test_download_before_run_404(self, server):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{BASE}/api/download/csv", timeout=5)
        assert e.value.code == 404

    def test_settings_roundtrip(self, server):
        _post("/api/settings", {"serpapi_key": "unit-test-key"})
        assert _get("/api/settings")["serpapi_key"] is True
        # never echo the secret back
        req = urllib.request.Request(f"{BASE}/api/settings")
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read()
        assert b"unit-test-key" not in body


class TestApiErrorSurfacing:
    """A bad API key must be reported to the user, not silently swallowed."""

    def test_status_exposes_api_errors(self, server, monkeypatch):
        import os
        sys.path.insert(0, str(Path(__file__).parent))
        from mock_api import MockAPI

        with MockAPI() as api:
            # point the running server's job at the mock via a fresh JobManager
            from nestick.config import Settings
            from nestick.discovery import Discovery
            from nestick.http import Fetcher
            import asyncio

            s = Settings(
                queries=["cafe"], urls=[], cache=False, respect_robots=False,
                google_maps_key="WRONG", places=True, engine="duckduckgo",
                allow_private_networks=True,
                places_url=f"{api.base}/maps/api/place/textsearch/json",
                places_details_url=f"{api.base}/maps/api/place/details/json",
            )

            async def go():
                async with Fetcher(s) as f:
                    d = Discovery(s, f)
                    async def none(q): return []
                    d.duckduckgo = none
                    await d.discover()
                    return d.api_errors

            errors = asyncio.run(go())
        assert any("API key is invalid" in e for e in errors)

    def test_status_field_present(self, server):
        assert "api_errors" in _get("/api/status")


class TestFullJob:
    """Runs a real (tiny, local-only) scrape through the API."""

    def test_job_lifecycle(self, server):
        r = _post("/api/start", {
            "urls": "https://example.com\nhttps://www.iana.org",
            "want_email": True, "want_phone": True, "want_social": True,
            "respect_robots": False, "cache": False,
            "concurrency": 8, "max_pages": 2,
        })
        assert r["ok"] is True

        # double-start is refused while running
        with pytest.raises(urllib.error.HTTPError):
            _post("/api/start", {"urls": "https://example.com"})

        st = {}
        for _ in range(60):
            time.sleep(0.5)
            st = _get("/api/status?log=0")
            if not st["running"] and st["finished"]:
                break
        assert st["error"] is None
        assert st["finished"] is True
        assert st["stats"]["requests"] > 0
        assert st["summary"]["leads"] >= 1

    def test_downloads_after_run(self, server):
        # depends on the previous test having produced files
        for fmt, magic in (("csv", b"domain"), ("xlsx", b"PK"), ("json", b"{")):
            with urllib.request.urlopen(f"{BASE}/api/download/{fmt}", timeout=20) as r:
                body = r.read()
            assert r.status == 200 and len(body) > 50
            assert body.startswith(magic) or magic in body[:200]

    def test_jsonl_generated_on_demand(self, server):
        """A format not produced by the run is exported lazily."""
        with urllib.request.urlopen(f"{BASE}/api/download/jsonl", timeout=20) as r:
            first = r.read().split(b"\n")[0]
        assert first.startswith(b"{")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


class TestJsonErrorsAndKeepAlive:
    """Regressions behind the user-reported 'Could not reach the server.'

    Two causes: the server answered errors with an HTML page (so the UI's
    response.json() threw and reported a network failure), and it spoke
    HTTP/1.0, closing the socket after every reply — which Windows browsers
    surface as intermittent fetch failures while polling.
    """

    def test_403_is_json_not_html(self, server):
        req = urllib.request.Request(
            f"{BASE}/api/start", data=json.dumps({"urls": "x"}).encode(),
            headers={"Content-Type": "application/json", "Origin": "https://evil.com"},
            method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("cross-site request should be refused")
        except urllib.error.HTTPError as e:
            assert e.code == 403
            assert "json" in (e.headers.get("Content-Type") or "")
            body = json.loads(e.read())          # must not raise
            assert body["ok"] is False and body["error"]

    def test_unknown_api_route_is_json(self, server):
        try:
            urllib.request.urlopen(f"{BASE}/api/does-not-exist", timeout=10)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
            assert json.loads(e.read())["error"]

    def test_download_before_run_is_json(self, server):
        try:
            urllib.request.urlopen(f"{BASE}/api/download/jsonl", timeout=10)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                assert json.loads(e.read())["error"]

    def test_http11_keepalive(self, server):
        """Two requests must share one socket, or polling gets flaky."""
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        conn.request("GET", "/api/status")
        r1 = conn.getresponse(); r1.read()
        conn.request("GET", "/api/status")     # reuses the connection
        r2 = conn.getresponse(); r2.read()
        conn.close()
        assert r1.status == 200 and r2.status == 200
        assert r2.version == 11, "server must speak HTTP/1.1"

    def test_many_sequential_polls_never_fail(self, server):
        """Simulates the dashboard polling loop."""
        for _ in range(25):
            with urllib.request.urlopen(f"{BASE}/api/status?log=0", timeout=10) as r:
                assert r.status == 200


class TestStaticAssetIntegrity:
    """Guards against malformed CSS/JS shipping to users.

    A regex rewrite once left an unclosed `@media` block, which made browsers
    discard every rule after it — the layout collapsed and the guided tour
    rendered at the bottom of the page instead of anchored to elements.
    """

    @staticmethod
    def _css() -> str:
        return (Path(__file__).resolve().parents[1]
                / "nestick" / "web" / "static" / "app.css").read_text()

    def test_braces_balanced(self):
        css = self._css()
        assert css.count("{") == css.count("}"), "unbalanced braces in app.css"

    def test_no_unclosed_rule(self):
        depth = 0
        for line_no, line in enumerate(self._css().splitlines(), 1):
            depth += line.count("{") - line.count("}")
            assert depth >= 0, f"stray closing brace at line {line_no}"
            assert depth <= 2, f"rule nested too deep at line {line_no}"
        assert depth == 0, "a rule or @media block was never closed"

    def test_no_empty_rules(self):
        import re
        assert not re.findall(r"\{\s*\}", self._css())

    def test_every_variable_resolves(self):
        import re
        css = self._css()
        defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", css))
        used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
        assert not (used - defined), f"undefined CSS variables: {used - defined}"

    def test_media_query_closed(self):
        css = self._css()
        assert "@media" in css
        tail = css[css.index("@media"):]
        assert tail.count("{") == tail.count("}"), "@media block is not closed"

    def test_javascript_parses(self):
        """Catch syntax errors in app.js if node is available."""
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed")
        js = (Path(__file__).resolve().parents[1]
              / "nestick" / "web" / "static" / "app.js")
        r = subprocess.run([node, "--check", str(js)], capture_output=True)
        assert r.returncode == 0, r.stderr.decode()[:400]

    def test_html_tags_balanced(self):
        import re
        html = (Path(__file__).resolve().parents[1]
                / "nestick" / "web" / "static" / "index.html").read_text()
        for tag in ("div", "section", "aside", "details", "table", "form"):
            opens = len(re.findall(rf"<{tag}[\s>]", html))
            closes = len(re.findall(rf"</{tag}>", html))
            assert opens == closes, f"<{tag}> mismatch: {opens} open, {closes} closed"
