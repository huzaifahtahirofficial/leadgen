"""The reconcile loop — a Wrangler-style controller for ScrapeJob resources.

Harvester's core pattern: a controller watches resources and repeatedly drives
observed state (``status``) towards declared state (``spec``). Here that means
running scrapes when they are due, recording conditions, retrying with backoff,
and re-queueing scheduled jobs.

    controller = JobController(store)
    await controller.reconcile_once("lahore-dentists")   # one pass
    await controller.run()                               # continuous loop
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from typing import Any, Callable

from .export import Exporter, summarise
from .pipeline import Pipeline
from .resources import (
    ConditionType,
    JobStore,
    Phase,
    ScrapeJob,
)
from .utils import log
from .webhook import admit

#: Called after every reconcile with (job, event) — used by the CLI/UI.
EventHook = Callable[[ScrapeJob, str], None]

_EVERY_RE = re.compile(r"^every\s+(\d+)\s*(s|m|h|d|sec|min|hour|day)s?$", re.I)
_UNIT_SECONDS = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600,
                 "hour": 3600, "d": 86_400, "day": 86_400}
_NAMED = {"@hourly": 3600, "@daily": 86_400, "@weekly": 604_800,
          "@monthly": 2_592_000}


def schedule_seconds(schedule: str) -> int | None:
    """Turn ``@daily`` / ``every 30m`` into an interval in seconds."""
    if not schedule:
        return None
    s = schedule.strip().lower()
    if s in _NAMED:
        return _NAMED[s]
    m = _EVERY_RE.match(s)
    if m:
        return max(1, int(m.group(1))) * _UNIT_SECONDS[m.group(2).lower()]
    return None


class JobController:
    """Reconciles :class:`ScrapeJob` resources towards their spec."""

    def __init__(
        self,
        store: JobStore,
        on_event: EventHook | None = None,
        resync_period: float = 5.0,
    ) -> None:
        self.store = store
        self.on_event = on_event
        self.resync_period = resync_period
        self._running: dict[str, asyncio.Task[Any]] = {}
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ #
    def _emit(self, job: ScrapeJob, event: str) -> None:
        log.debug("[%s] %s", job.name, event)
        if self.on_event:
            with contextlib.suppress(Exception):
                self.on_event(job, event)

    @staticmethod
    def due(job: ScrapeJob, now: float | None = None) -> bool:
        """Should this job run right now?"""
        now = now or time.time()
        st, sp = job.status, job.spec
        if sp.suspend or st.phase is Phase.RUNNING:
            return False
        if st.observedGeneration < job.generation:
            return True                       # spec changed → re-run
        interval = schedule_seconds(sp.schedule)
        if interval is None:
            return st.runCount == 0           # one-shot, never run
        if st.nextRunTime and now < st.nextRunTime:
            return False
        return True

    # ------------------------------------------------------------------ #
    async def reconcile_once(self, name: str, force: bool = False) -> ScrapeJob | None:
        """Reconcile a single job; runs it if due."""
        job = self.store.get(name)
        if job is None:
            log.warning("Job %r not found", name)
            return None

        review = admit(job)
        for w in review.warnings:
            log.warning("[%s] %s", job.name, w)
        if not review.allowed:
            job.status.phase = Phase.FAILED
            job.status.message = "; ".join(review.errors)
            job.status.set_condition(ConditionType.READY, False, "AdmissionDenied",
                                     job.status.message)
            self.store.save()
            self._emit(job, "denied")
            return job

        if job.spec.suspend:
            job.status.phase = Phase.PAUSED
            job.status.set_condition(ConditionType.READY, False, "Suspended",
                                     "spec.suspend is true")
            self.store.save()
            self._emit(job, "suspended")
            return job

        if not force and not self.due(job):
            return job

        return await self._run_job(job)

    async def _run_job(self, job: ScrapeJob) -> ScrapeJob:
        st = job.spec
        job.status.phase = Phase.RUNNING
        job.status.startTime = time.time()
        job.status.completionTime = None
        job.status.message = ""
        job.status.set_condition(ConditionType.PROGRESSING, True, "Running",
                                 "scrape in progress")
        job.status.set_condition(ConditionType.READY, False, "Running", "")
        self.store.save()
        self._emit(job, "started")

        attempt = 0
        last_error: str | None = None
        while attempt <= st.backoffLimit:
            attempt += 1
            try:
                settings = job.to_settings()
                async with Pipeline(settings) as pipe:
                    leads = await pipe.run()
                    files: list[str] = []
                    if leads:
                        files = [str(p) for p in Exporter(settings).write(leads, pipe.stats)]
                    summary = summarise(leads)
                    s = job.status
                    s.leads = summary["leads"]
                    s.emails = summary["unique_emails"]
                    s.requests = pipe.stats.requests
                    s.files = files
                    s.runCount += 1
                    s.lastRunTime = time.time()
                    s.completionTime = time.time()
                    s.observedGeneration = job.generation
                    s.phase = Phase.SUCCEEDED
                    s.message = f"{summary['leads']} leads, {summary['unique_emails']} e-mails"
                    if not leads:
                        hint = ""
                        if job.spec.crawl.maxPagesPerSite <= 1:
                            hint = (" — maxPagesPerSite is 1; contact details are "
                                    "usually on a second page, try 4+")
                        elif job.spec.crawl.depth == 0:
                            hint = " — depth is 0, so no internal links were followed"
                        s.message = f"no leads found{hint}"
                        log.warning("[%s] %s", job.name, s.message)
                    s.set_condition(ConditionType.READY, True, "RunSucceeded", s.message)
                    s.set_condition(ConditionType.PROGRESSING, False, "Completed", "")
                    api_errors = pipe.api_errors
                    if api_errors:
                        s.set_condition(ConditionType.DEGRADED, True, "ApiError",
                                        "; ".join(api_errors))
                    else:
                        s.set_condition(ConditionType.DEGRADED, False, "Healthy", "")
                self._schedule_next(job)
                self.store.save()
                self._emit(job, "succeeded")
                return job
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("[%s] attempt %d/%d failed: %s",
                            job.name, attempt, st.backoffLimit + 1, last_error)
                if attempt <= st.backoffLimit:
                    await asyncio.sleep(min(2 ** attempt, 30))

        s = job.status
        s.phase = Phase.FAILED
        s.failureCount += 1
        s.runCount += 1
        s.lastRunTime = time.time()
        s.completionTime = time.time()
        s.observedGeneration = job.generation
        s.message = last_error or "run failed"
        s.set_condition(ConditionType.READY, False, "RunFailed", s.message)
        s.set_condition(ConditionType.PROGRESSING, False, "Failed", "")
        self._schedule_next(job)
        self.store.save()
        self._emit(job, "failed")
        return job

    @staticmethod
    def _schedule_next(job: ScrapeJob) -> None:
        interval = schedule_seconds(job.spec.schedule)
        if interval:
            job.status.nextRunTime = time.time() + interval
            job.status.set_condition(
                ConditionType.SCHEDULED, True, "Scheduled",
                f"next run in {interval}s")
        else:
            job.status.nextRunTime = None

    # ------------------------------------------------------------------ #
    async def reconcile_all(self, selector: dict[str, str] | None = None) -> list[ScrapeJob]:
        """One pass over every job that is due."""
        out: list[ScrapeJob] = []
        for job in self.store.list(selector):
            if self.due(job):
                result = await self.reconcile_once(job.name)
                if result:
                    out.append(result)
        return out

    async def run(self, selector: dict[str, str] | None = None) -> None:
        """Continuous control loop until :meth:`stop` is called."""
        log.info("Controller started (resync every %.0fs, %d job(s))",
                 self.resync_period, len(self.store))
        while not self._stop.is_set():
            self.store.load()
            with contextlib.suppress(Exception):
                await self.reconcile_all(selector)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.resync_period)
        log.info("Controller stopped")

    def stop(self) -> None:
        self._stop.set()
