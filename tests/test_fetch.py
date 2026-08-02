"""Tests for ``nestick fetch`` dump formats."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nestick.fetch import assets, cookies, links, markdown, text  # noqa: E402
from nestick.models import Response  # noqa: E402

SAMPLE = """
<!doctype html>
<html>
<head>
  <title>Acme &amp; Co</title>
  <link rel="stylesheet" href="/css/app.css">
  <script src="https://cdn.example.net/lib.js"></script>
</head>
<body>
  <h1>Acme &amp; Co</h1>
  <p>Welcome to <strong>Acme</strong>. Visit <a href="/about">the about page</a>
     or <a href="https://external.example.com/ref">External</a>.</p>
  <ul><li>First item</li><li>Second <em>item</em></li></ul>
  <pre><code>print("hi")</code></pre>
  <img src="img/logo.png" alt="Acme logo">
  <iframe src="https://maps.example.com/embed"></iframe>
</body>
</html>
"""


class TestText:
    def test_strips_markup(self):
        out = text(SAMPLE)
        assert "Welcome to Acme" in out
        assert "First item" in out
        assert "<p>" not in out and "&amp;" not in out
        assert out.endswith("\n")

    def test_skips_script_content(self):
        html = "<p>keep</p><script>var x = 1;</script><style>.a{}</style><p>end</p>"
        out = text(html)
        assert "keep" in out and "end" in out
        assert "var x" not in out


class TestMarkdown:
    def test_headings(self):
        out = markdown("<h1>Top</h1><h3>Sub</h3>")
        assert "# Top" in out and "### Sub" in out

    def test_links_resolved(self):
        out = markdown(SAMPLE, base_url="https://acme.example.com/")
        assert "[the about page](https://acme.example.com/about)" in out
        assert "[External](https://external.example.com/ref)" in out

    def test_lists_and_emphasis(self):
        out = markdown("<ul><li>First <b>bold</b></li><li>Second</li></ul>")
        assert "- First **bold**" in out
        assert "- Second" in out

    def test_code_block(self):
        out = markdown("<pre><code>print(1)</code></pre>")
        assert "```" in out and "print(1)" in out

    def test_image(self):
        out = markdown('<img src="/a.png" alt="logo">', base_url="https://x.example.com/")
        assert "![logo](https://x.example.com/a.png)" in out

    def test_no_doctype_bleed(self):
        out = markdown(SAMPLE)
        assert "doctype" not in out.lower() and "html" not in out.lower().split()[0]


class TestLinks:
    def test_all_anchors_resolved_and_deduped(self):
        html = ('<a href="/a">A</a><a href="https://x.example.com/a">dup</a>'
                '<a href="#frag">skip</a><a href="javascript:void(0)">skip2</a>')
        out = links(html, base_url="https://x.example.com/")
        assert out == ["https://x.example.com/a"]

    def test_external_kept(self):
        out = links(SAMPLE, base_url="https://acme.example.com/")
        assert "https://external.example.com/ref" in out


class TestAssets:
    def test_kinds_detected(self):
        out = assets(SAMPLE, base_url="https://acme.example.com/")
        kinds = {a["kind"] for a in out}
        assert kinds == {"stylesheet", "script", "image", "iframe"}
        assert {"kind": "stylesheet", "url": "https://acme.example.com/css/app.css"} in out
        assert {"kind": "script", "url": "https://cdn.example.net/lib.js"} in out
        assert {"kind": "image", "url": "https://acme.example.com/img/logo.png"} in out
        assert {"kind": "iframe", "url": "https://maps.example.com/embed"} in out

    def test_ndjson_serialisable(self):
        for item in assets(SAMPLE, base_url="https://acme.example.com/"):
            json.dumps(item)  # must not raise


class TestCookies:
    def test_parses_cookie_jar(self):
        resp = Response(
            url="https://x.example.com", status=200, text="",
            headers={"set-cookie": "sid=abc123; HttpOnly; Path=/; SameSite=Lax"},
        )
        out = cookies(resp)
        assert out == [{
            "name": "sid", "value": "abc123", "httponly": True,
            "path": "/", "samesite": "Lax", "secure": False,
        }]

    def test_empty_without_header(self):
        resp = Response(url="https://x.example.com", status=200, text="")
        assert cookies(resp) == []


# --------------------------------------------------------------------------- #
# End-to-end: `nestick fetch` against a local HTTP server
# --------------------------------------------------------------------------- #
class _Page(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # silence access log
        pass

    def do_GET(self) -> None:  # noqa: N802
        body = SAMPLE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Set-Cookie", "session=deadbeef; HttpOnly; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def page_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


def _run_fetch(page_server: str, *args: str) -> subprocess.CompletedProcess:
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(root))
    return subprocess.run(
        [sys.executable, "-m", "nestick", "fetch", page_server, *args],
        capture_output=True, env=env, cwd=root, timeout=60,
    )


def test_cli_text_dump(page_server):
    r = _run_fetch(page_server, "--dump", "text", "--allow-private", "--quiet")
    assert r.returncode == 0, r.stderr.decode(errors="replace")
    assert "Welcome to Acme" in r.stdout.decode("utf-8")

def test_cli_links_dump(page_server):
    r = _run_fetch(page_server, "--dump", "links", "--allow-private", "--quiet")
    assert r.returncode == 0
    out = r.stdout.decode("utf-8")
    assert f"{page_server}about" in out

def test_cli_cookies_dump(page_server):
    r = _run_fetch(page_server, "--dump", "cookies", "--allow-private", "--quiet")
    assert r.returncode == 0
    jar = json.loads(r.stdout.decode("utf-8"))
    assert any(c["name"] == "session" and c["value"] == "deadbeef"
               and c["httponly"] for c in jar)

def test_cli_rejects_unknown_dump(page_server):
    r = _run_fetch(page_server, "--dump", "nope", "--allow-private")
    assert r.returncode != 0
