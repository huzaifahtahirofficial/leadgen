"""Command-line interface.

Flag design borrows from the Harvester API server: every meaningful option is
also settable via an environment variable, concurrency is exposed as
``--threadiness``, shutdown runs off a signal-derived context, and an optional
diagnostics port serves health/metrics/pprof-style introspection.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import threading
import time
from typing import Any

from .config import Settings
from .export import Exporter, summarise
from .models import Lead, Stats
from .pipeline import Pipeline
from .utils import log, setup_logging

BANNER = r"""
  _   _           _   _      _
 | \ | | ___  ___| |_(_) ___| | __
 |  \| |/ _ \/ __| __| |/ __| |/ /
 | |\  |  __/\__ \ |_| | (__|   <
 |_| \_|\___||___/\__|_|\___|_|\_\

   TECH LEAD GENERATOR  ·  v{version}
   SkelerSecurity Intelligence Engine
"""


def _env(name: str, default: Any = None, cast: type = str) -> Any:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        if cast is bool:
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return cast(raw)
    except (TypeError, ValueError):
        return default


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nestick",
        description="Nestick Tech Lead Generator — SkelerSecurity Intelligence Engine. "
                    "SERP discovery, site crawling and API enrichment in one tool.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  nestick -q "dentists in Lahore" --pages 2 --format csv,xlsx
  nestick -u https://acme.com https://globex.com --depth 1
  nestick -i urls.txt --threadiness 64 --want email,phone -o out/leads
  SERPAPI_KEY=xxx nestick -q "saas companies berlin" --pages 3 --format all
  HUNTER_API_KEY=xxx nestick -q "law firms nyc" --diagnostics-port 6060
  nestick fetch https://example.com --dump markdown   # inspect a single page

every flag below can also be set through its environment variable.""",
    )

    d = p.add_argument_group("discovery")
    d.add_argument("-q", "--query", action="append", default=None,
                   help="search query (repeatable) [NESTICK_QUERY]")
    d.add_argument("-u", "--urls", nargs="+", default=[],
                   help="explicit URLs to scrape")
    d.add_argument("-i", "--input-file", default=_env("NESTICK_INPUT_FILE"),
                   help="file with one URL per line [NESTICK_INPUT_FILE]")
    d.add_argument("--engine", default=_env("NESTICK_ENGINE", "auto"),
                   choices=["auto", "serpapi", "duckduckgo", "bing", "urls"],
                   help="discovery backend (default: auto) [NESTICK_ENGINE]")
    d.add_argument("-p", "--pages", type=int, default=_env("NESTICK_PAGES", 1, int),
                   help="SERP pages per query (default: 1) [NESTICK_PAGES]")
    d.add_argument("--location", default=_env("NESTICK_LOCATION"),
                   help="geographic bias for the query [NESTICK_LOCATION]")
    d.add_argument("--language", default=_env("NESTICK_LANGUAGE", "en"),
                   help="results language (default: en) [NESTICK_LANGUAGE]")
    d.add_argument("--country", default=_env("NESTICK_COUNTRY", "us"),
                   help="results country (default: us) [NESTICK_COUNTRY]")
    d.add_argument("--places", action="store_true", default=_env("NESTICK_PLACES", False, bool),
                   help="also pull business listings (Google Places if a key is "
                        "set, otherwise free OpenStreetMap) [NESTICK_PLACES]")
    d.add_argument("--no-osm", action="store_true", default=_env("NESTICK_NO_OSM", False, bool),
                   help="disable the keyless OpenStreetMap places fallback [NESTICK_NO_OSM]")

    k = p.add_argument_group("api keys")
    k.add_argument("--serpapi-key", default=_env("SERPAPI_KEY"), help="[SERPAPI_KEY]")
    k.add_argument("--hunter-key", default=_env("HUNTER_API_KEY"), help="[HUNTER_API_KEY]")
    k.add_argument("--numverify-key", default=_env("NUMVERIFY_KEY"),
                   help="NumVerify free tier: carrier + line type for phones [NUMVERIFY_KEY]")
    k.add_argument("--google-maps-key", default=_env("GOOGLE_MAPS_KEY"), help="[GOOGLE_MAPS_KEY]")

    c = p.add_argument_group("crawling")
    c.add_argument("-t", "--threadiness", "--concurrency", dest="threadiness", type=int,
                   default=_env("THREADINESS", 24, int),
                   help="global concurrent requests (default: 24) [THREADINESS]")
    c.add_argument("--per-host", type=int, default=_env("NESTICK_PER_HOST", 3, int),
                   help="max concurrent requests per host (default: 3) [NESTICK_PER_HOST]")
    c.add_argument("--max-pages", type=int, default=_env("NESTICK_MAX_PAGES", 6, int),
                   help="pages crawled per site (default: 6) [NESTICK_MAX_PAGES]")
    c.add_argument("--depth", type=int, default=_env("NESTICK_DEPTH", 1, int),
                   help="internal link depth (default: 1) [NESTICK_DEPTH]")
    c.add_argument("--timeout", type=float, default=_env("NESTICK_TIMEOUT", 15.0, float),
                   help="request timeout seconds (default: 15) [NESTICK_TIMEOUT]")
    c.add_argument("--retries", type=int, default=_env("NESTICK_RETRIES", 3, int),
                   help="max attempts per URL (default: 3) [NESTICK_RETRIES]")
    c.add_argument("--delay", type=float, default=_env("NESTICK_DELAY", 0.0, float),
                   help="polite per-host delay seconds [NESTICK_DELAY]")
    c.add_argument("--proxy", action="append", dest="proxies", default=[],
                   help="proxy URL (repeatable)")
    c.add_argument("--proxy-file", default=_env("NESTICK_PROXY_FILE"),
                   help="file with one proxy per line [NESTICK_PROXY_FILE]")
    c.add_argument("--ignore-robots", action="store_true",
                   default=_env("NESTICK_IGNORE_ROBOTS", False, bool),
                   help="do not consult robots.txt [NESTICK_IGNORE_ROBOTS]")
    c.add_argument("--no-http2", action="store_true", default=_env("NESTICK_NO_HTTP2", False, bool),
                   help="disable HTTP/2 [NESTICK_NO_HTTP2]")
    c.add_argument("--no-sitemaps", action="store_true",
                   default=_env("NESTICK_NO_SITEMAPS", False, bool),
                   help="skip robots.txt→sitemap (and Wayback) contact-page "
                        "discovery before crawling each site [NESTICK_NO_SITEMAPS]")
    c.add_argument("--allow-private", action="store_true",
                   default=_env("NESTICK_ALLOW_PRIVATE", False, bool),
                   help="permit localhost/RFC1918 targets (intranet scraping); "
                        "cloud metadata stays blocked [NESTICK_ALLOW_PRIVATE]")

    e = p.add_argument_group("extraction")
    e.add_argument("-w", "--want", default=_env("NESTICK_WANT", "email,phone,social"),
                   help="comma list: email,phone,social,all [NESTICK_WANT]")
    e.add_argument("--min-confidence", type=float, default=_env("NESTICK_MIN_CONFIDENCE", 0.0, float),
                   help="drop contacts below this score [NESTICK_MIN_CONFIDENCE]")
    e.add_argument("--no-deobfuscate", action="store_true",
                   help="skip 'name (at) domain (dot) com' decoding")
    e.add_argument("--no-verify-mx", action="store_true",
                   default=_env("NESTICK_NO_VERIFY_MX", False, bool),
                   help="skip the keyless MX deliverability check [NESTICK_NO_VERIFY_MX]")
    e.add_argument("--firmographics", action="store_true",
                   default=_env("NESTICK_FIRMOGRAPHICS", False, bool),
                   help="look up company profiles on Wikidata [NESTICK_FIRMOGRAPHICS]")

    o = p.add_argument_group("output")
    o.add_argument("-o", "--output", default=_env("NESTICK_OUTPUT", "leads"),
                   help="output basename (default: leads) [NESTICK_OUTPUT]")
    o.add_argument("-f", "--format", dest="formats", default=_env("NESTICK_FORMAT", "csv,json"),
                   help="csv,json,jsonl,xlsx,md,sqlite,all (default: csv,json) [NESTICK_FORMAT]")
    o.add_argument("--no-cache", action="store_true", default=_env("NESTICK_NO_CACHE", False, bool),
                   help="bypass the on-disk response cache [NESTICK_NO_CACHE]")
    o.add_argument("--no-resume", action="store_true", help="ignore previous run state")
    o.add_argument("--cache-ttl", type=int, default=_env("NESTICK_CACHE_TTL", 86_400, int),
                   help="cache lifetime seconds (default: 86400) [NESTICK_CACHE_TTL]")

    g = p.add_argument_group("interface")
    g.add_argument("--ui", "--gui", dest="ui", action="store_true",
                   help="launch the browser control panel instead of running a scrape")
    g.add_argument("--ui-port", type=int, default=_env("NESTICK_UI_PORT", _env("PORT", 8765, int), int),
                   help="port for the control panel (default: 8765, or $PORT on a PaaS) [NESTICK_UI_PORT]")
    g.add_argument("--ui-host",
                   default=_env("NESTICK_UI_HOST",
                                "0.0.0.0" if _env("PORT") else "127.0.0.1"),
                   help="bind address for the control panel (default: 0.0.0.0 when $PORT is set) [NESTICK_UI_HOST]")
    g.add_argument("--no-browser", action="store_true",
                   help="do not open a browser window automatically")

    m = p.add_argument_group("misc")
    m.add_argument("--diagnostics-port", type=int,
                   default=_env("NESTICK_DIAGNOSTICS_PORT", 0, int),
                   help="serve /healthz, /metrics, /stats on this port [NESTICK_DIAGNOSTICS_PORT]")
    m.add_argument("-v", "--verbose", action="store_true", default=_env("NESTICK_VERBOSE", False, bool))
    m.add_argument("--quiet", action="store_true", default=_env("NESTICK_QUIET", False, bool))
    m.add_argument("--no-progress", action="store_true", help="disable the live dashboard")
    m.add_argument("--log-file", default=_env("NESTICK_LOG_FILE"))
    m.add_argument("--seed", type=int, default=_env("NESTICK_SEED", None, int))
    m.add_argument("--dry-run", action="store_true", help="print resolved settings and exit")
    m.add_argument("--version", action="store_true")
    return p


def settings_from_args(a: argparse.Namespace) -> Settings:
    queries = a.query or ([_env("NESTICK_QUERY")] if _env("NESTICK_QUERY") else [])
    return Settings(
        queries=[q for q in queries if q],
        urls=list(a.urls),
        input_file=a.input_file,
        engine=a.engine,
        pages=a.pages,
        location=a.location,
        language=a.language,
        country=a.country,
        places=a.places,
        osm_fallback=not a.no_osm,
        serpapi_key=a.serpapi_key,
        hunter_key=a.hunter_key,
        numverify_key=a.numverify_key,
        google_maps_key=a.google_maps_key,
        concurrency=a.threadiness,
        per_host_concurrency=a.per_host,
        max_pages_per_site=a.max_pages,
        depth=a.depth,
        timeout=a.timeout,
        max_retries=a.retries,
        delay=a.delay,
        proxies=list(a.proxies or []),
        proxy_file=a.proxy_file,
        respect_robots=not a.ignore_robots,
        http2=not a.no_http2,
        sitemap_discovery=not a.no_sitemaps,
        allow_private_networks=a.allow_private,
        want=tuple(w.strip() for w in a.want.split(",") if w.strip()),
        min_confidence=a.min_confidence,
        deobfuscate=not a.no_deobfuscate,
        verify_mx=not a.no_verify_mx,
        firmographics=a.firmographics,
        output=a.output,
        formats=tuple(f.strip() for f in a.formats.split(",") if f.strip()),
        cache=not a.no_cache,
        cache_ttl=a.cache_ttl,
        resume=not a.no_resume,
        verbose=a.verbose,
        quiet=a.quiet,
        progress=not a.no_progress and not a.quiet,
        log_file=a.log_file,
        seed=a.seed,
    )


# --------------------------------------------------------------------------- #
# Diagnostics endpoint (Harvester-style side port)
# --------------------------------------------------------------------------- #
def start_diagnostics(port: int, stats: Stats, leads: dict[str, Lead]) -> None:
    """Serve /healthz, /stats and /metrics on a daemon thread."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # silence access log
            pass

        def _send(self, body: str, ctype: str = "application/json") -> None:
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            if path in ("/healthz", "/readyz", "/ping"):
                self._send('{"status":"ok"}')
            elif path == "/stats":
                self._send(json.dumps(stats.as_row(), indent=2))
            elif path == "/metrics":
                rows = stats.as_row()
                body = "".join(
                    f"# TYPE nestick_{k} gauge\nnestick_{k} {v}\n"
                    for k, v in rows.items()
                    if isinstance(v, (int, float))
                )
                self._send(body, "text/plain; version=0.0.4")
            elif path == "/leads":
                self._send(json.dumps([l.to_dict() for l in leads.values()], default=str))
            else:
                self.send_error(404)

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="diagnostics").start()
    log.info("Diagnostics listening on http://0.0.0.0:%d (/healthz /stats /metrics)", port)


# --------------------------------------------------------------------------- #
async def _run(settings: Settings, diagnostics_port: int = 0) -> int:
    from . import __version__

    console = None
    if not settings.quiet:
        with contextlib.suppress(ImportError):
            from rich.console import Console

            console = Console(stderr=True)
            console.print(f"[bold cyan]{BANNER.format(version=__version__)}[/]")

    async with Pipeline(settings) as pipe:
        if diagnostics_port:
            start_diagnostics(diagnostics_port, pipe.stats, pipe.leads)

        if settings.progress and console is not None:
            leads = await _run_with_dashboard(pipe, console)
        else:
            leads = await pipe.run()

        api_errors = pipe.api_errors
        if not leads:
            log.warning("No leads found.")
            for e in api_errors:
                log.error("%s", e)
            return 3 if api_errors else 2

        paths = Exporter(settings).write(leads, pipe.stats)
        _print_summary(console, leads, pipe.stats, paths, api_errors)
    return 0


async def _run_with_dashboard(pipe: Pipeline, console: Any) -> list[Lead]:
    """Run the pipeline behind a live Rich table of the best leads so far."""
    from rich.live import Live
    from rich.table import Table

    recent: list[Lead] = []

    def render() -> Table:
        s = pipe.stats
        t = Table(
            title=(f"[bold]Nestick[/] · {s.requests} req · {s.rps:.1f}/s · "
                   f"{s.cache_hits} cached · {s.failures} failed · "
                   f"{len(pipe.leads)} leads · {s.elapsed:.0f}s"),
            expand=True, header_style="bold magenta",
        )
        t.add_column("Domain", style="cyan", no_wrap=True, max_width=28)
        t.add_column("Name", max_width=24)
        t.add_column("E-mail", style="green", max_width=38)
        t.add_column("Phone", style="yellow", max_width=18)
        t.add_column("Sc", justify="right", width=4)
        for lead in recent[-12:][::-1]:
            t.add_row(
                lead.domain,
                (lead.name or "")[:24],
                (lead.emails[0] if lead.emails else "—"),
                (lead.phones[0] if lead.phones else "—"),
                str(int(lead.score)),
            )
        return t

    pipe.on_lead = lambda lead, _s: recent.append(lead)
    with Live(render(), console=console, refresh_per_second=4, transient=False) as live:
        task = asyncio.create_task(pipe.run())
        while not task.done():
            live.update(render())
            await asyncio.sleep(0.25)
        live.update(render())
        return await task


def _print_summary(console: Any, leads: list[Lead], stats: Stats, paths: list,
                   api_errors: list[str] | None = None) -> None:
    summary = summarise(leads)
    api_errors = api_errors or []
    if console is None:
        print(json.dumps({**summary, "files": [str(p) for p in paths],
                          "api_errors": api_errors}, indent=2))
        return
    if api_errors:
        from rich.panel import Panel as _Panel

        console.print(_Panel(
            "\n".join(f"• {e}" for e in api_errors)
            + "\n\n[dim]The run continued without that API.[/]",
            title="[bold red]API problem[/]", border_style="red"))
    from rich.panel import Panel
    from rich.table import Table

    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold")
    t.add_column()
    for k, v in summary.items():
        t.add_row(k.replace("_", " ").title(), str(v))
    t.add_row("", "")
    for k, v in stats.as_row().items():
        t.add_row(k.replace("_", " ").title(), str(v))
    t.add_row("", "")
    t.add_row("Files", "\n".join(str(p) for p in paths))
    console.print(Panel(t, title="[bold green]Run complete[/]", border_style="green"))


def main(argv: list[str] | None = None) -> int:
    from . import __version__

    argv = list(sys.argv[1:] if argv is None else argv)
    # Bare subcommand form: `nestick ui`
    if argv and argv[0] in ("ui", "gui", "web"):
        argv = ["--ui", *argv[1:]]
    # Declarative resource management: `nestick job apply -f jobs.yaml`
    if argv and argv[0] in ("job", "jobs"):
        from .ctl import main as ctl_main

        return ctl_main(argv[1:])
    # Single-page inspection: `nestick fetch URL --dump text`
    if argv and argv[0] == "fetch":
        from .fetch import fetch_main

        return fetch_main(argv[1:])

    args = build_parser().parse_args(argv)
    if args.version:
        print(f"nestick {__version__}")
        return 0

    setup_logging(args.verbose, args.quiet, args.log_file)

    if args.ui:
        from .web import serve

        return serve(host=args.ui_host, port=args.ui_port,
                     open_browser=not args.no_browser)
    try:
        settings = settings_from_args(args)
    except ValueError as exc:
        log.error("%s", exc)
        print("\nProvide -q/--query, -u/--urls or -i/--input-file. See --help.", file=sys.stderr)
        return 1

    if args.dry_run:
        print(json.dumps(settings.to_dict(), indent=2, default=str))
        return 0

    with contextlib.suppress(ImportError):
        import uvloop  # type: ignore

        uvloop.install()

    started = time.monotonic()
    try:
        return asyncio.run(_run(settings, args.diagnostics_port))
    except KeyboardInterrupt:
        log.warning("Interrupted after %.1fs", time.monotonic() - started)
        return 130
    except Exception as exc:  # noqa: BLE001
        log.error("Fatal: %s", exc, exc_info=args.verbose)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
