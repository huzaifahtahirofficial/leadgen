"""``nestick job …`` — a kubectl-style CLI for declarative scrape jobs.

    nestick job apply -f jobs.yaml
    nestick job get [-l team=sales] [-o yaml|json|wide]
    nestick job describe lahore-dentists
    nestick job run lahore-dentists
    nestick job controller           # continuous reconcile loop
    nestick job delete lahore-dentists
    nestick job template > job.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any

from .controller import JobController, schedule_seconds
from .resources import (
    API_VERSION,
    KIND,
    ConditionType,
    JobStore,
    Phase,
    ScrapeJob,
    dump_yaml,
    load_jobs,
    parse_selector,
)
from .utils import log
from .webhook import admit

DEFAULT_STORE = "~/.nestick/jobs.json"

TEMPLATE = f"""# Nestick job — apply with:  nestick job apply -f this-file.yaml
apiVersion: {API_VERSION}
kind: {KIND}
metadata:
  name: lahore-dentists
  labels:
    team: sales
spec:
  queries:
    - "dentists in Lahore"
  # urls:                       # or scrape a fixed list instead
  #   - https://example.com
  engine: auto                  # auto | serpapi | duckduckgo | bing | urls
  pages: 2
  location: "Lahore, Pakistan"
  want: [email, phone, social]
  maxEmailsPerLead: 25
  schedule: ""                  # "" = run once | @daily | every 6h
  suspend: false
  backoffLimit: 2
  crawl:
    concurrency: 24
    perHost: 3
    maxPagesPerSite: 6
    depth: 1
    delay: 0.0
    respectRobots: true
    cache: true
  output:
    formats: [csv, xlsx]
    path: out/lahore-dentists
"""


# --------------------------------------------------------------------------- #
def _age(ts: float | None) -> str:
    if not ts:
        return "-"
    d = max(0, time.time() - ts)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= size:
            return f"{int(d // size)}{unit}"
    return f"{int(d)}s"


def _until(ts: float | None) -> str:
    if not ts:
        return "-"
    d = ts - time.time()
    if d <= 0:
        return "due"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= size:
            return f"{int(d // size)}{unit}"
    return f"{int(d)}s"


def _print_table(jobs: list[ScrapeJob], wide: bool = False) -> None:
    if not jobs:
        print("No jobs found. Create one with:  nestick job template > job.yaml")
        return
    headers = ["NAME", "PHASE", "READY", "LEADS", "EMAILS", "RUNS", "LAST", "NEXT"]
    if wide:
        headers += ["SCHEDULE", "ENGINE", "TARGETS", "OUTPUT"]
    rows: list[list[str]] = []
    for j in jobs:
        s = j.status
        ready = j.status.condition(ConditionType.READY)
        row = [
            j.name, str(s.phase),
            "True" if (ready and ready.ok) else "False",
            str(s.leads), str(s.emails), str(s.runCount),
            _age(s.lastRunTime), _until(s.nextRunTime),
        ]
        if wide:
            targets = (j.spec.queries[0] if j.spec.queries
                       else (j.spec.urls[0] if j.spec.urls else "-"))
            if len(j.spec.queries) + len(j.spec.urls) > 1:
                targets += f" (+{len(j.spec.queries) + len(j.spec.urls) - 1})"
            row += [j.spec.schedule or "-", j.spec.engine, targets[:38],
                    j.spec.output.path]
        rows.append(row)
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    for r in rows:
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)))


def _describe(job: ScrapeJob) -> None:
    s, sp = job.status, job.spec
    print(f"Name:          {job.name}")
    print(f"UID:           {job.uid}")
    print(f"Generation:    {job.generation} (observed {s.observedGeneration})")
    if job.labels:
        print(f"Labels:        {', '.join(f'{k}={v}' for k, v in job.labels.items())}")
    print(f"Created:       {job.metadata.get('creationTimestamp', '-')}")
    print()
    print("Spec:")
    print(f"  Engine:      {sp.engine}")
    if sp.queries:
        print(f"  Queries:     {', '.join(sp.queries)}")
    if sp.urls:
        print(f"  URLs:        {len(sp.urls)} target(s)")
    print(f"  Pages:       {sp.pages}")
    print(f"  Want:        {', '.join(sp.want)}")
    print(f"  Schedule:    {sp.schedule or '(run once)'}"
          + (f"  → every {schedule_seconds(sp.schedule)}s" if sp.schedule else ""))
    print(f"  Concurrency: {sp.crawl.concurrency} global / {sp.crawl.perHost} per host")
    print(f"  Robots:      {'respected' if sp.crawl.respectRobots else 'IGNORED'}")
    print(f"  Output:      {sp.output.path} ({', '.join(sp.output.formats)})")
    print()
    print("Status:")
    print(f"  Phase:       {s.phase}")
    print(f"  Runs:        {s.runCount} ({s.failureCount} failed)")
    print(f"  Leads:       {s.leads}   E-mails: {s.emails}   Requests: {s.requests}")
    print(f"  Last run:    {_age(s.lastRunTime)} ago" if s.lastRunTime else "  Last run:    never")
    if s.nextRunTime:
        print(f"  Next run:    in {_until(s.nextRunTime)}")
    if s.message:
        print(f"  Message:     {s.message}")
    if s.files:
        print("  Files:")
        for f in s.files:
            print(f"    - {f}")
    print()
    print("Conditions:")
    if not s.conditions:
        print("  (none)")
    else:
        w = max(len(c.type) for c in s.conditions)
        print("  " + "TYPE".ljust(w) + "  STATUS   REASON")
        for c in s.conditions:
            print(f"  {c.type.ljust(w)}  {c.status.ljust(7)}  {c.reason}"
                  + (f" — {c.message[:60]}" if c.message else ""))


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nestick job",
        description="Declarative scrape jobs (apply / get / describe / run / controller).",
    )
    p.add_argument("--store", default=DEFAULT_STORE,
                   help=f"job store path (default: {DEFAULT_STORE})")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("apply", help="create or update jobs from a file")
    a.add_argument("-f", "--filename", required=True, help="YAML/JSON file ('-' for stdin)")
    a.add_argument("--dry-run", action="store_true", help="validate only, change nothing")
    a.add_argument("--run", action="store_true", help="reconcile immediately after applying")

    g = sub.add_parser("get", help="list jobs")
    g.add_argument("name", nargs="?")
    g.add_argument("-l", "--selector", help="label selector, e.g. team=sales")
    g.add_argument("-o", "--output", default="table",
                   choices=["table", "wide", "yaml", "json", "name"])

    d = sub.add_parser("describe", help="show full detail for one job")
    d.add_argument("name")

    r = sub.add_parser("run", help="reconcile now, ignoring the schedule")
    r.add_argument("name", nargs="?")
    r.add_argument("-l", "--selector")
    r.add_argument("--watch", action="store_true", help="stream progress")

    c = sub.add_parser("controller", help="run the continuous reconcile loop")
    c.add_argument("-l", "--selector")
    c.add_argument("--interval", type=float, default=5.0, help="resync seconds")
    c.add_argument("--once", action="store_true", help="single pass, then exit")

    x = sub.add_parser("delete", help="remove a job")
    x.add_argument("name")

    s = sub.add_parser("suspend", help="pause a scheduled job")
    s.add_argument("name")
    u = sub.add_parser("resume", help="un-pause a job")
    u.add_argument("name")

    sub.add_parser("template", help="print a starter job manifest")
    return p


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "template":
        print(TEMPLATE, end="")
        return 0

    store = JobStore(args.store)

    # ---- apply ------------------------------------------------------- #
    if args.cmd == "apply":
        try:
            if args.filename == "-":
                from .resources import load_documents
                jobs = [ScrapeJob.from_dict(d) for d in load_documents(sys.stdin.read())]
            else:
                jobs = load_jobs(args.filename)
        except FileNotFoundError:
            print(f"error: no such file: {args.filename}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"error: could not parse {args.filename}: {exc}", file=sys.stderr)
            return 1
        if not jobs:
            print("error: no ScrapeJob documents found", file=sys.stderr)
            return 1

        failed = False
        applied: list[ScrapeJob] = []
        for job in jobs:
            review = admit(job)
            for w in review.warnings:
                print(f"warning: {job.name}: {w}", file=sys.stderr)
            for pch in review.patches:
                print(f"patched: {job.name}: {pch}", file=sys.stderr)
            if not review.allowed:
                for e in review.errors:
                    print(f"error: {job.name}: {e}", file=sys.stderr)
                failed = True
                continue
            if args.dry_run:
                print(f'scrapejob/{job.name} validated (dry run)')
                continue
            saved, action = store.apply(job)
            print(f"scrapejob/{saved.name} {action}")
            applied.append(saved)
        if failed:
            return 1
        if args.run and applied:
            async def go():
                ctrl = JobController(store)
                for j in applied:
                    await ctrl.reconcile_once(j.name, force=True)
            asyncio.run(go())
            _print_table([store.get(j.name) for j in applied if store.get(j.name)])
        return 0

    # ---- get ---------------------------------------------------------- #
    if args.cmd == "get":
        sel = parse_selector(args.selector)
        jobs = [store.get(args.name)] if args.name else store.list(sel)
        jobs = [j for j in jobs if j]
        if args.name and not jobs:
            print(f'error: scrapejob "{args.name}" not found', file=sys.stderr)
            return 1
        if args.output == "json":
            print(json.dumps({"apiVersion": API_VERSION, "kind": "ScrapeJobList",
                              "items": [j.to_dict() for j in jobs]},
                             indent=2, default=str))
        elif args.output == "yaml":
            print("\n---\n".join(j.to_yaml() for j in jobs), end="")
        elif args.output == "name":
            for j in jobs:
                print(f"scrapejob/{j.name}")
        else:
            _print_table(jobs, wide=args.output == "wide")
        return 0

    # ---- describe ------------------------------------------------------ #
    if args.cmd == "describe":
        job = store.get(args.name)
        if not job:
            print(f'error: scrapejob "{args.name}" not found', file=sys.stderr)
            return 1
        _describe(job)
        return 0

    # ---- run ------------------------------------------------------------ #
    if args.cmd == "run":
        sel = parse_selector(getattr(args, "selector", None))
        names = [args.name] if args.name else [j.name for j in store.list(sel)]
        if not names:
            print("no jobs to run", file=sys.stderr)
            return 1

        def hook(job: ScrapeJob, event: str) -> None:
            if event == "started":
                print(f"▶ {job.name}: started")
            elif event == "succeeded":
                print(f"✔ {job.name}: {job.status.message}")
            elif event == "failed":
                print(f"✘ {job.name}: {job.status.message}")
            elif event == "denied":
                print(f"✘ {job.name}: rejected — {job.status.message}")

        async def go() -> int:
            ctrl = JobController(store, on_event=hook)
            bad = 0
            for n in names:
                job = await ctrl.reconcile_once(n, force=True)
                if job and job.status.phase is Phase.FAILED:
                    bad += 1
            return bad

        failures = asyncio.run(go())
        print()
        _print_table([store.get(n) for n in names if store.get(n)])
        return 1 if failures else 0

    # ---- controller ------------------------------------------------------ #
    if args.cmd == "controller":
        sel = parse_selector(args.selector)

        def hook(job: ScrapeJob, event: str) -> None:
            log.info("[%s] %s — %s", job.name, event, job.status.message or job.status.phase)

        async def go() -> None:
            ctrl = JobController(store, on_event=hook, resync_period=args.interval)
            if args.once:
                done = await ctrl.reconcile_all(sel)
                print(f"reconciled {len(done)} job(s)")
                return
            with contextlib.suppress(KeyboardInterrupt):
                await ctrl.run(sel)

        from .utils import setup_logging
        setup_logging()
        try:
            asyncio.run(go())
        except KeyboardInterrupt:
            pass
        return 0

    # ---- delete / suspend / resume ---------------------------------------- #
    if args.cmd == "delete":
        if store.delete(args.name):
            print(f'scrapejob "{args.name}" deleted')
            return 0
        print(f'error: scrapejob "{args.name}" not found', file=sys.stderr)
        return 1

    if args.cmd in ("suspend", "resume"):
        job = store.get(args.name)
        if not job:
            print(f'error: scrapejob "{args.name}" not found', file=sys.stderr)
            return 1
        job.spec.suspend = args.cmd == "suspend"
        job.bump_generation()
        store.apply(job)
        store.save()
        print(f'scrapejob/{job.name} {"suspended" if job.spec.suspend else "resumed"}')
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
