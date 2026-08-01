"""Orchestration: discovery → concurrent site crawl → enrichment → export."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import Settings
from .discovery import Discovery
from .enrich import Enricher, analyse
from .export import Exporter
from .extract import Extractor
from .http import Fetcher
from .models import Contact, ContactKind, Lead, Stats
from .security import is_safe_url
from .utils import dedupe, log, normalise_url, registrable_domain, setup_logging

ProgressHook = Callable[[Lead, Stats], None]


class Pipeline:
    """End-to-end scraping run.

    ::

        async with Pipeline(settings) as p:
            leads = await p.run()
    """

    def __init__(self, settings: Settings, on_lead: ProgressHook | None = None) -> None:
        self.s = settings
        self.stats = Stats()
        self.extractor = Extractor(settings)
        self.leads: dict[str, Lead] = {}
        self.on_lead = on_lead
        self._fetcher: Fetcher | None = None
        self._seen_urls: set[str] = set()
        self._stop = asyncio.Event()
        self._discovery: Discovery | None = None
        #: Sites found inside directory pages, crawled in a second wave.
        self._directory_finds: list[str] = []
        self._enricher: Enricher | None = None

    @property
    def analytics(self) -> dict[str, Any]:
        """Aggregate intelligence over the finished result set."""
        data = analyse(self._finalise())
        if self._enricher:
            data["enrichment"] = self._enricher.stats.as_row()
        return data

    @property
    def api_errors(self) -> list[str]:
        """Third-party API failures (bad key, quota…) seen during the run."""
        return list(self._discovery.api_errors) if self._discovery else []

    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> "Pipeline":
        self._fetcher = await Fetcher(self.s, self.stats).__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._fetcher:
            await self._fetcher.aclose()

    @property
    def fetcher(self) -> Fetcher:
        if self._fetcher is None:
            raise RuntimeError("Pipeline must be used as an async context manager")
        return self._fetcher

    # ------------------------------------------------------------------ #
    # State (resume support)
    # ------------------------------------------------------------------ #
    def _load_state(self) -> set[str]:
        if not self.s.resume:
            return set()
        p = Path(self.s.state_path)
        if not p.is_file():
            return set()
        with contextlib.suppress(Exception):
            data = json.loads(p.read_text("utf-8"))
            done = set(data.get("done", []))
            if done:
                log.info("Resuming: %d sites already processed", len(done))
            return done
        return set()

    def _save_state(self, done: Iterable[str]) -> None:
        if not self.s.resume:
            return
        with contextlib.suppress(Exception):
            Path(self.s.state_path).write_text(
                json.dumps({"done": sorted(done), "ts": time.time()}), "utf-8"
            )

    # ------------------------------------------------------------------ #
    # Main run
    # ------------------------------------------------------------------ #
    async def run(self) -> list[Lead]:
        t0 = time.monotonic()
        self._install_signals()

        # 1. Discovery -------------------------------------------------- #
        disco = Discovery(self.s, self.fetcher)
        self._discovery = disco
        seeds, prebuilt = await disco.discover()
        for lead in prebuilt:
            if lead.domain:
                self.leads.setdefault(lead.domain, lead)
        if not seeds:
            log.warning("No URLs discovered — nothing to crawl.")
            return self._finalise()

        blocked = [u for u in seeds if not is_safe_url(
            u, allow_private=self.s.allow_private_networks)]
        if blocked:
            log.warning(
                "%d target(s) blocked by the SSRF guard (private/internal "
                "addresses). Use --allow-private for intranet scraping. First: %s",
                len(blocked), blocked[0])

        # 2. Group seeds by site so each domain is crawled once ---------- #
        by_domain: dict[str, list[str]] = defaultdict(list)
        for u in dedupe(seeds):
            if self.extractor.is_scrapeable(u):
                by_domain[registrable_domain(u)].append(u)

        done = self._load_state()
        targets = {d: us for d, us in by_domain.items() if d and d not in done}
        log.info(
            "Crawling %d sites (%d seed URLs, concurrency=%d)",
            len(targets), sum(len(v) for v in targets.values()), self.s.concurrency,
        )

        # 3. Crawl ------------------------------------------------------- #
        sem = asyncio.Semaphore(max(4, self.s.concurrency // max(self.s.per_host_concurrency, 1)))
        completed: set[str] = set(done)

        async def worker(domain: str, urls: list[str]) -> None:
            if self._stop.is_set():
                return
            async with sem:
                if self._stop.is_set():
                    return
                try:
                    lead = await self.crawl_site(domain, urls)
                except Exception as exc:  # noqa: BLE001
                    log.debug("Site %s failed: %s", domain, exc)
                    return
                completed.add(domain)
                if lead and (lead.contacts or lead.name):
                    self.stats.leads = len(self.leads)
                    if self.on_lead:
                        with contextlib.suppress(Exception):
                            self.on_lead(lead, self.stats)

        tasks = [asyncio.create_task(worker(d, u)) for d, u in targets.items()]
        try:
            await asyncio.gather(*tasks)

            # Second wave: sites discovered inside directory/listicle pages.
            wave = 0
            while (self._directory_finds and not self._stop.is_set()
                   and wave < self.s.directory_waves):
                wave += 1
                found = list(dedupe(self._directory_finds))
                self._directory_finds.clear()
                by_dom: dict[str, list[str]] = defaultdict(list)
                for u in found:
                    d = registrable_domain(u)
                    if d and d not in self.leads and d not in completed:
                        by_dom[d].append(u)
                if not by_dom:
                    break
                log.info("Directory wave %d: crawling %d discovered site(s)",
                         wave, len(by_dom))
                await asyncio.gather(*(
                    asyncio.create_task(worker(d, u)) for d, u in by_dom.items()
                ))
        except asyncio.CancelledError:  # pragma: no cover
            pass
        finally:
            self._save_state(completed)

        # 4. Enrichment -------------------------------------------------- #
        if self.s.hunter_key:
            await self._enrich_hunter(disco)

        leads_now = self._finalise()

        # Free-source validation and firmographics.
        if leads_now and (self.s.verify_mx or self.s.numverify_key or self.s.firmographics):
            try:
                self._enricher = Enricher(self.fetcher, self.s)
                await self._enricher.enrich(leads_now)
            except Exception as exc:  # noqa: BLE001
                log.warning("Enrichment skipped: %s", exc)

        log.info("Crawl finished in %.1fs", time.monotonic() - t0)
        return self._finalise()

    # ------------------------------------------------------------------ #
    async def crawl_site(self, domain: str, seed_urls: list[str]) -> Lead | None:
        """Fetch the seed page, then the highest-value contact pages."""
        lead = self.leads.get(domain) or Lead(domain=domain, url=seed_urls[0])
        self.leads[domain] = lead
        budget = self.s.max_pages_per_site
        queue: list[str] = [u for u in dedupe(seed_urls) if u not in self._seen_urls][:3]
        visited: set[str] = set()
        depth_of: dict[str, int] = {u: 0 for u in queue}

        while queue and budget > 0 and not self._stop.is_set():
            batch = queue[: min(len(queue), 4, budget)]
            del queue[: len(batch)]
            for u in batch:
                visited.add(u)
                self._seen_urls.add(u)
            responses = await self.fetcher.gather(batch)
            budget -= len(batch)

            for url, resp in zip(batch, responses):
                if not resp.ok:
                    if resp.error and resp.error != "blocked-by-robots":
                        lead.errors.append(f"{url} :: {resp.error}")
                    continue
                self.stats.pages_parsed += 1
                lead.pages_crawled += 1
                contacts, meta = self.extractor.extract_all(resp.text, resp.url)
                new = lead.add(c for c in contacts if c.confidence >= self.s.min_confidence)

                # A search for "schools in Riyadh" surfaces directory pages, not
                # schools. Harvest the organisations such a page links to so the
                # real leads are crawled instead of the aggregator.
                if self.s.follow_directories and depth_of.get(url, 0) == 0:
                    if self.extractor.is_directory_page(resp.text, resp.url, len(contacts)):
                        orgs = self.extractor.outbound_orgs(
                            resp.text, resp.url, limit=self.s.max_directory_links)
                        fresh = [o for o in orgs
                                 if registrable_domain(o) not in self.leads
                                 and o not in self._seen_urls]
                        if fresh:
                            lead.extra["directory"] = True
                            lead.extra["listed_orgs"] = len(orgs)
                            self._directory_finds.extend(fresh)
                            log.info("%s is a directory — queued %d linked site(s)",
                                     domain, len(fresh))
                self.stats.emails_found += sum(
                    1 for c in contacts if c.kind is ContactKind.EMAIL
                )
                self._apply_meta(lead, meta)

                # Expand: contact-ish pages first, then shallow internal links.
                if budget > 0 and depth_of.get(url, 0) < max(self.s.depth, 1):
                    nxt = self.extractor.contact_links(resp.text, resp.url, limit=budget)
                    if not nxt and not lead.emails and self.s.depth > 0:
                        nxt = self.extractor.links(resp.text, resp.url)[:budget]
                    for n in nxt:
                        if n not in visited and n not in queue and n not in self._seen_urls:
                            queue.append(n)
                            depth_of[n] = depth_of.get(url, 0) + 1
                # Early exit: strong same-domain e-mail already found.
                if new and any(
                    c.kind is ContactKind.EMAIL
                    and c.confidence >= 0.9
                    and c.value.endswith(f"@{domain}")
                    for c in lead.contacts
                ):
                    queue = queue[:1]

        if not lead.url and seed_urls:
            lead.url = seed_urls[0]
        return lead

    @staticmethod
    def _apply_meta(lead: Lead, meta: dict[str, Any]) -> None:
        lead.name = lead.name or meta.get("name")
        lead.title = lead.title or meta.get("title")
        lead.description = lead.description or meta.get("description")
        lead.address = lead.address or meta.get("address")
        if lead.latitude is None:
            lead.latitude = meta.get("latitude")
        if lead.longitude is None:
            lead.longitude = meta.get("longitude")
        if lead.rating is None:
            lead.rating = meta.get("rating")
        if lead.reviews is None:
            lead.reviews = meta.get("reviews")

    async def _enrich_hunter(self, disco: Discovery) -> None:
        domains = [d for d, l in self.leads.items() if d and not l.emails]
        if not domains:
            return
        log.info("Hunter.io enrichment for %d domains…", len(domains))
        results = await disco.hunter_bulk(domains)
        for domain, (contacts, meta) in results.items():
            lead = self.leads.get(domain)
            if not lead:
                continue
            lead.add(contacts)
            if meta.get("name"):
                lead.name = lead.name or meta["name"]
            lead.extra.update({k: v for k, v in meta.items() if k != "name"})

    # ------------------------------------------------------------------ #
    def _trim_bulk_emails(self, lead: Lead) -> None:
        """Cap staff-directory dumps so one careers page cannot skew a lead."""
        cap = getattr(self.s, "max_emails_per_lead", 0)
        if not cap:
            return
        emails = [c for c in lead.contacts if c.kind is ContactKind.EMAIL]
        if len(emails) <= cap:
            return
        # Prefer role mailboxes and high-confidence hits over a personnel list.
        emails.sort(key=lambda c: (-c.confidence, not c.meta.get("role", False), c.value))
        keep = {id(c) for c in emails[:cap]}
        lead.contacts = [
            c for c in lead.contacts if c.kind is not ContactKind.EMAIL or id(c) in keep
        ]
        lead.extra["emails_truncated"] = len(emails)
        log.debug("%s: kept %d of %d e-mails", lead.domain, cap, len(emails))

    def _finalise(self) -> list[Lead]:
        for lead in self.leads.values():
            self._trim_bulk_emails(lead)
        leads = [l for l in self.leads.values() if l.contacts or l.name]
        leads.sort(key=lambda l: (-l.score, l.domain))
        self.stats.leads = len(leads)
        return leads

    def _install_signals(self) -> None:
        """Best-effort Ctrl-C handling.

        Only possible on the main thread of the main interpreter; when the
        pipeline is driven from a worker thread (e.g. the web UI) the caller
        stops it via :meth:`_request_stop` instead.
        """
        if os.name != "posix" or threading.current_thread() is not threading.main_thread():
            return
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError, RuntimeError, AttributeError):
                loop.add_signal_handler(sig, self._request_stop)

    def _request_stop(self) -> None:  # pragma: no cover - interactive
        if not self._stop.is_set():
            log.warning("Stop requested — finishing in-flight work, then exporting…")
            self._stop.set()


# --------------------------------------------------------------------------- #
# Convenience entry points
# --------------------------------------------------------------------------- #
async def arun(
    settings: Settings,
    export: bool = True,
    on_lead: ProgressHook | None = None,
) -> list[Lead]:
    """Run a full scrape asynchronously and optionally write output files."""
    setup_logging(settings.verbose, settings.quiet, settings.log_file)
    async with Pipeline(settings, on_lead=on_lead) as p:
        leads = await p.run()
        if export and leads:
            Exporter(settings).write(leads, p.stats)
        return leads


def run(settings: Settings, export: bool = True) -> list[Lead]:
    """Synchronous wrapper around :func:`arun`."""
    with contextlib.suppress(ImportError):
        import uvloop  # type: ignore

        uvloop.install()
    return asyncio.run(arun(settings, export=export))
