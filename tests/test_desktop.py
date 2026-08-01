"""Regression tests for the desktop launcher.

The bug these guard against: on Windows, launching Chrome usually hands the URL
to an already-running Chrome instance and the launched process exits within
milliseconds. Treating that exit as "the user closed the app" tore the server
down before the tab had loaded, which the user saw as ERR_CONNECTION_REFUSED.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import nestick.desktop as desktop  # noqa: E402
from nestick.web.server import JobManager  # noqa: E402


@pytest.fixture
def instant_exit_browser(tmp_path, monkeypatch):
    """A fake 'browser' that exits immediately, like Chrome handing off."""
    script = tmp_path / "fake-browser"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(desktop, "_find_browser", lambda: str(script))
    return script


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class TestTrafficTracking:
    def test_jobmanager_starts_with_no_hits(self, tmp_path, monkeypatch):
        monkeypatch.setattr("nestick.web.server.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("nestick.web.server.CONFIG_FILE", tmp_path / "c.json")
        jm = JobManager()
        assert jm.hits == 0 and jm.last_hit == 0.0

    def test_wait_returns_false_without_traffic(self, tmp_path, monkeypatch):
        monkeypatch.setattr("nestick.web.server.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("nestick.web.server.CONFIG_FILE", tmp_path / "c.json")
        jm = JobManager()
        t0 = time.monotonic()
        assert desktop._wait_for_first_request(jm, timeout=0.6) is False
        assert time.monotonic() - t0 >= 0.5

    def test_wait_returns_true_once_hit(self, tmp_path, monkeypatch):
        monkeypatch.setattr("nestick.web.server.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("nestick.web.server.CONFIG_FILE", tmp_path / "c.json")
        jm = JobManager()

        def hit():
            time.sleep(0.3)
            jm.hits += 1
            jm.last_hit = time.monotonic()

        threading.Thread(target=hit, daemon=True).start()
        assert desktop._wait_for_first_request(jm, timeout=5.0) is True


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX shell stub")
class TestServerSurvivesBrowserHandoff:
    def test_page_loads_after_browser_exits(self, instant_exit_browser):
        """The core regression: server must outlive the launched process."""
        port = _free_port()
        result: dict[str, object] = {}

        def run():
            result["rc"] = desktop.run(port=port, mode="chrome")

        threading.Thread(target=run, daemon=True).start()

        # The fake browser exits at once; the page must still be served.
        deadline = time.monotonic() + 15
        body = None
        while time.monotonic() < deadline:
            time.sleep(0.4)
            try:
                body = urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/", timeout=3).read()
                break
            except Exception:
                continue
        assert body, "ERR_CONNECTION_REFUSED: server died with the browser process"
        assert b"Nestick" in body

    def test_api_reachable_after_handoff(self, instant_exit_browser):
        port = _free_port()
        threading.Thread(
            target=lambda: desktop.run(port=port, mode="chrome"), daemon=True).start()
        deadline = time.monotonic() + 15
        ok = False
        while time.monotonic() < deadline:
            time.sleep(0.4)
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/status", timeout=3).read()
                ok = True
                break
            except Exception:
                continue
        assert ok

    def test_shuts_down_when_traffic_stops(self, instant_exit_browser, monkeypatch):
        """It must not linger forever once the window is really gone."""
        monkeypatch.setattr(desktop._serve_until_idle, "__defaults__", (2.0,))
        port = _free_port()
        done: dict[str, object] = {}

        def run():
            done["rc"] = desktop.run(port=port, mode="chrome")

        t = threading.Thread(target=run, daemon=True)
        t.start()
        for _ in range(30):
            time.sleep(0.4)
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3).read()
                break
            except Exception:
                continue
        t.join(timeout=20)
        assert not t.is_alive(), "server never shut down after the window closed"
        assert done.get("rc") == 0


class TestModes:
    def test_server_mode_needs_no_browser(self, monkeypatch):
        """--mode server must never depend on a browser being installed."""
        monkeypatch.setattr(desktop, "_find_browser", lambda: None)
        port = _free_port()
        threading.Thread(
            target=lambda: desktop.run(port=port, mode="server"), daemon=True).start()
        ok = False
        for _ in range(25):
            time.sleep(0.3)
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3)
                ok = True
                break
            except Exception:
                continue
        assert ok

    def test_chrome_mode_errors_without_browser(self, monkeypatch):
        monkeypatch.setattr(desktop, "_find_browser", lambda: None)
        assert desktop.run(port=_free_port(), mode="chrome") == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
