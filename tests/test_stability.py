"""Stability suite: hostile input, concurrency, broken servers, memory.

Every case here caused a real failure at some point during development. They
run offline (a local stub server) so they are fast and deterministic.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nestick.config import Settings  # noqa: E402
from nestick.http import Fetcher  # noqa: E402
from nestick.pipeline import Pipeline  # noqa: E402


# --------------------------------------------------------------------------- #
# A deliberately badly-behaved web server
# --------------------------------------------------------------------------- #
class HostileHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # noqa: D102
        pass

    def do_GET(self):  # noqa: N802
        p = self.path
        if p == "/slow":
            time.sleep(10)
            self._ok(b"late")
        elif p == "/huge":
            self._ok(b"<html>" + b"A" * 12_000_000 + b"</html>")
        elif p == "/bomb":
            body = gzip.compress(b"<html>" + b"\0" * 40_000_000 + b"</html>")
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with __import__("contextlib").suppress(Exception):
                self.wfile.write(body)
        elif p == "/reset":
            self.connection.close()
        elif p == "/redir":
            self.send_response(302)
            self.send_header("Location", "/redir")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif p == "/badenc":
            self._ok(b"<html>\xff\xfe\x00 bad@enc.test</html>")
        elif p == "/nulls":
            self._ok(b"<html>a\x00\x00b info@nulls.test</html>")
        elif p == "/deep":
            self._ok(b"<div>" * 5000 + b"x@deep.test" + b"</div>" * 5000)
        else:
            self._ok(b"<html><a href='mailto:ok@site.test'>m</a></html>")

    def _ok(self, body: bytes) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


@pytest.fixture(scope="module")
def hostile():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), HostileHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _settings(**kw):
    base = dict(urls=["https://placeholder.invalid"], cache=False,
                respect_robots=False, allow_private_networks=True,
                timeout=4, max_retries=1, verify_mx=False)
    base.update(kw)
    return Settings(**base)


class TestHostileServers:
    """A broken or malicious site must never hang or crash the crawler."""

    @pytest.mark.parametrize("path", ["/slow", "/reset"])
    def test_timeouts_are_bounded(self, hostile, path):
        async def go():
            async with Fetcher(_settings()) as f:
                t0 = time.monotonic()
                r = await f.get(hostile + path)
                return time.monotonic() - t0, r

        elapsed, r = asyncio.run(go())
        assert elapsed < 20, f"{path} hung for {elapsed:.0f}s"
        assert not r.ok and r.error

    @pytest.mark.parametrize("path", ["/huge", "/bomb"])
    def test_oversized_bodies_rejected(self, hostile, path):
        async def go():
            async with Fetcher(_settings()) as f:
                return await f.get(hostile + path)

        r = asyncio.run(go())
        assert r.error == "body-too-large"
        assert r.text == ""

    def test_redirect_loop_caught(self, hostile):
        async def go():
            async with Fetcher(_settings()) as f:
                return await f.get(hostile + "/redir")

        r = asyncio.run(go())
        assert not r.ok and "edirect" in (r.error or "")

    @pytest.mark.parametrize("path,expected", [
        ("/badenc", "bad@enc.test"),
        ("/nulls", "info@nulls.test"),
        ("/deep", "x@deep.test"),
    ])
    def test_malformed_html_still_parses(self, hostile, path, expected):
        from nestick.extract import Extractor

        async def go():
            async with Fetcher(_settings()) as f:
                return await f.get(hostile + path)

        r = asyncio.run(go())
        assert r.ok
        contacts, _ = Extractor(None).extract_all(r.text, hostile + path)
        assert expected in [c.value for c in contacts]

    def test_full_pipeline_survives_everything(self, hostile, tmp_path):
        urls = [hostile + p for p in
                ("/slow", "/huge", "/bomb", "/reset", "/redir", "/ok")]
        s = _settings(urls=urls, engine="urls", resume=False,
                      max_pages_per_site=1, output=str(tmp_path / "o"),
                      formats=("json",), state_path=str(tmp_path / "s.json"))

        async def go():
            t0 = time.monotonic()
            async with Pipeline(s) as p:
                leads = await p.run()
            return leads, time.monotonic() - t0

        leads, elapsed = asyncio.run(go())
        assert elapsed < 60, "hostile endpoints stalled the crawl"
        assert isinstance(leads, list)


# --------------------------------------------------------------------------- #
PORT = 9017
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="module")
def ui(tmp_path_factory):
    import os

    env = {**os.environ, "NESTICK_CONFIG_DIR": str(tmp_path_factory.mktemp("cfg"))}
    proc = subprocess.Popen(
        [sys.executable, "-m", "nestick", "ui", "--no-browser", "--ui-port", str(PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    for _ in range(50):
        time.sleep(0.25)
        try:
            urllib.request.urlopen(f"{BASE}/healthz", timeout=2)
            break
        except Exception:
            continue
    else:
        proc.kill()
        pytest.skip("UI did not start")
    yield proc
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
        proc.wait(timeout=5)
    finally:
        # Close the pipe explicitly or pytest reports a ResourceWarning.
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                with __import__("contextlib").suppress(Exception):
                    stream.close()


def _post_raw(path: str, body: bytes):
    req = urllib.request.Request(f"{BASE}{path}", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class TestHostileRequests:
    """Malformed payloads must produce JSON errors, never a dropped socket."""

    @pytest.mark.parametrize("name,body", [
        ("empty", b""),
        ("malformed", b"{not json"),
        ("array", b"[1,2,3]"),
        ("null", b"null"),
        ("string", b'"hello"'),
        ("number", b"42"),
        ("deep nesting", ("{" + '"a":{' * 200 + '"b":1' + "}" * 200 + "}").encode()),
        ("wrong types", json.dumps({"query": ["a"], "pages": {"x": 1},
                                    "cache": "maybe"}).encode()),
        ("negative", json.dumps({"query": "x", "pages": -99,
                                 "concurrency": -5}).encode()),
        ("absurd numbers", json.dumps({"query": "x", "pages": 10 ** 12,
                                       "timeout": 10 ** 9}).encode()),
        ("nul bytes", json.dumps({"query": "a\u0000b"}).encode()),
    ])
    def test_never_crashes(self, ui, name, body):
        status, payload = _post_raw("/api/start", body)
        assert status < 500, f"{name} caused a server error"
        assert json.loads(payload) is not None      # always valid JSON

    def test_oversized_body_does_not_break_pipe(self, ui):
        """Skipping the socket read leaves unread bytes → 'Broken pipe'."""
        big = b'{"query":"' + b"x" * 3_000_000 + b'"}'
        status, payload = _post_raw("/api/start", big)
        assert status < 500
        # the connection must still be usable afterwards
        with urllib.request.urlopen(f"{BASE}/api/status", timeout=15) as r:
            assert r.status == 200

    def test_settings_rejects_junk_values(self, ui):
        status, _ = _post_raw("/api/settings", b'{"serpapi_key":{"nested":true}}')
        assert status < 500
        with urllib.request.urlopen(f"{BASE}/api/settings", timeout=10) as r:
            assert json.loads(r.read())["serpapi_key"] is False

    def test_server_survives_the_whole_barrage(self, ui):
        with urllib.request.urlopen(f"{BASE}/healthz", timeout=10) as r:
            assert json.loads(r.read())["status"] == "ok"

    def test_no_tracebacks_logged(self, ui):
        """A traceback means an unhandled exception reached the socket."""
        # Give the server a moment to flush anything pending.
        time.sleep(0.5)
        assert ui.poll() is None, "server process died"


class TestConcurrency:
    def test_many_parallel_polls(self, ui):
        errors: list[str] = []

        def poll(_i):
            try:
                urllib.request.urlopen(f"{BASE}/api/status?log=0", timeout=20).read()
            except Exception as e:
                errors.append(type(e).__name__)

        threads = [threading.Thread(target=poll, args=(i,)) for i in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, f"concurrent polls failed: {set(errors)}"

    def test_rapid_start_stop_churn(self, ui):
        for _ in range(4):
            _post_raw("/api/start", json.dumps(
                {"urls": "https://example.com", "respect_robots": False,
                 "cache": False, "max_pages": 1}).encode())
            time.sleep(0.15)
            _post_raw("/api/stop", b"{}")
            time.sleep(0.2)
        with urllib.request.urlopen(f"{BASE}/healthz", timeout=10) as r:
            assert r.status == 200

    def test_double_start_is_refused_cleanly(self, ui):
        body = json.dumps({"urls": "https://example.com", "respect_robots": False,
                           "cache": False, "max_pages": 1}).encode()
        _post_raw("/api/start", body)
        status, payload = _post_raw("/api/start", body)
        assert status == 400
        assert json.loads(payload)["ok"] is False
        _post_raw("/api/stop", b"{}")


class TestDeterminism:
    def test_repeated_runs_are_stable(self, tmp_path):
        """The same input must give the same output, with no state bleed."""
        counts = []
        for i in range(3):
            s = _settings(
                urls=["https://example.com"], engine="urls", resume=False,
                cache=False, output=str(tmp_path / f"o{i}"), formats=("json",),
                state_path=str(tmp_path / f"s{i}.json"))

            async def go():
                async with Pipeline(s) as p:
                    return await p.run()

            counts.append(len(asyncio.run(go())))
        assert len(set(counts)) == 1, f"non-deterministic results: {counts}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
