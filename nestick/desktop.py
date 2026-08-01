"""Desktop application entry point.

Starts the local control-panel server and shows it in a window:

1. **Native window** via ``pywebview`` if it is installed (best experience).
2. **Chrome/Edge app window** (``--app=``) — a real chromeless window, no deps.
3. **Default browser** tab as the final fallback.

The server is bound to ``127.0.0.1`` on a free port and shuts down with the app.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer
from typing import Any

APP_NAME = "Nestick Tech Lead Generator"


# --------------------------------------------------------------------------- #
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_until_up(port: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError):
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        time.sleep(0.15)
    return False


def _find_browser() -> str | None:
    """Locate a Chromium-family binary that supports --app windows."""
    candidates = [
        "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
        "microsoft-edge", "msedge", "brave-browser", "vivaldi",
    ]
    if sys.platform == "darwin":
        for path in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ):
            if os.path.isfile(path):
                return path
    elif os.name == "nt":
        pf = [os.environ.get("PROGRAMFILES", r"C:\Program Files"),
              os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
              os.environ.get("LOCALAPPDATA", "")]
        for base in pf:
            for rel in (r"Google\Chrome\Application\chrome.exe",
                        r"Microsoft\Edge\Application\msedge.exe"):
                path = os.path.join(base, rel)
                if os.path.isfile(path):
                    return path
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    return None


# --------------------------------------------------------------------------- #
def run(port: int = 0, mode: str = "auto", width: int = 1280, height: int = 820) -> int:
    """Launch the desktop app. ``mode``: auto | native | chrome | browser | server."""
    from .web.server import JobManager, make_handler

    port = port or _free_port()
    jobs = JobManager()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(jobs))
    url = f"http://127.0.0.1:{port}"

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True,
                                     name="nestick-http")
    server_thread.start()
    if not _wait_until_up(port):
        print("Error: the local server did not start.", file=sys.stderr)
        return 1

    print(f"\n  {APP_NAME} is running at {url}")

    try:
        if mode == "server":
            print("  Server mode — press Ctrl-C to stop.\n", flush=True)
            _block()
            return 0

        # 1. native window ------------------------------------------------ #
        if mode in ("auto", "native"):
            try:
                import webview  # type: ignore

                print("  Opening native window…\n", flush=True)
                webview.create_window(APP_NAME, url, width=width, height=height,
                                      min_size=(980, 640))
                webview.start()
                return 0
            except ImportError:
                if mode == "native":
                    print("  pywebview is not installed  (pip install pywebview)",
                          file=sys.stderr)
                    return 1
            except Exception as exc:  # pragma: no cover - platform dependent
                print(f"  Native window unavailable ({exc}); falling back.",
                      file=sys.stderr)

        # 2. chrome app window -------------------------------------------- #
        if mode in ("auto", "chrome"):
            browser = _find_browser()
            if browser:
                profile = os.path.join(tempfile.gettempdir(), "nestick-app-profile")
                proc = subprocess.Popen(
                    [browser, f"--app={url}", f"--user-data-dir={profile}",
                     f"--window-size={width},{height}", "--no-first-run",
                     "--no-default-browser-check"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                print("  Opening app window…\n", flush=True)
                # A launched Chrome usually hands the URL to an already-running
                # Chrome and exits within milliseconds. Waiting on that process
                # would tear the server down before the tab has even loaded,
                # which the user sees as ERR_CONNECTION_REFUSED. So wait for the
                # page to actually be served, then keep running until Ctrl-C or
                # the window closes.
                if _wait_for_first_request(jobs, timeout=25.0):
                    print("  Connected. Close this window (or press Ctrl-C) to quit.\n",
                          flush=True)
                    _serve_until_idle(proc, jobs)
                    return 0
                # Nothing ever connected — fall through to the default browser.
                print("  App window did not connect; opening your default browser…",
                      file=sys.stderr, flush=True)
                with contextlib.suppress(Exception):
                    proc.terminate()
            elif mode == "chrome":
                print("  No Chromium-family browser found.", file=sys.stderr)
                return 1

        # 3. default browser ---------------------------------------------- #
        print("  Opening in your default browser…", flush=True)
        with contextlib.suppress(Exception):
            webbrowser.open(url)
        if not _wait_for_first_request(jobs, timeout=20.0):
            print(f"  If no window appeared, open this address yourself:\n    {url}",
                  flush=True)
        print("  Press Ctrl-C to stop.\n", flush=True)
        _block()
        return 0
    finally:
        httpd.shutdown()
        with contextlib.suppress(Exception):
            httpd.server_close()


def _wait_for_first_request(jobs: Any, timeout: float = 25.0) -> bool:
    """Block until the UI actually fetches a page, or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if getattr(jobs, "hits", 0) > 0:
            return True
        time.sleep(0.2)
    return False


def _serve_until_idle(proc: Any, jobs: Any, grace: float = 8.0) -> None:
    """Keep serving while the app window is open.

    The browser process is not a reliable signal on Windows, so instead we
    watch for traffic: the dashboard polls ``/api/status`` roughly every second,
    so a gap longer than ``grace`` means the window is gone.
    """
    try:
        while True:
            time.sleep(1.0)
            if proc is not None and proc.poll() is None:
                continue  # a real, still-running browser process: keep going
            idle = time.monotonic() - getattr(jobs, "last_hit", 0.0)
            if idle > grace:
                print("  Window closed — shutting down.")
                return
    except KeyboardInterrupt:
        print("\n  Shutting down…")


def _block() -> None:
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n  Shutting down…")


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="nestick-app",
                                description=f"{APP_NAME} — SkelerSecurity Intelligence Engine")
    p.add_argument("--mode", default=os.environ.get("NESTICK_APP_MODE", "auto"),
                   choices=["auto", "native", "chrome", "browser", "server"],
                   help="how to display the app (default: auto)")
    p.add_argument("--port", type=int, default=int(os.environ.get("NESTICK_APP_PORT", 0)),
                   help="fixed port (default: pick a free one)")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=820)
    a = p.parse_args(argv)
    return run(port=a.port, mode=a.mode, width=a.width, height=a.height)


if __name__ == "__main__":
    raise SystemExit(main())
