"""Declarative resources — a Harvester/Kubernetes-style API for scrape jobs.

Instead of a wall of CLI flags, a job is a *resource* you declare in YAML and
apply. The controller then reconciles reality towards that spec.

    apiVersion: nestick.io/v1
    kind: ScrapeJob
    metadata:
      name: lahore-dentists
      labels: {team: sales}
    spec:
      queries: ["dentists in Lahore"]
      pages: 2
      schedule: "@daily"
      output: {formats: [csv, xlsx], path: out/dentists}
    status:
      phase: Succeeded
      conditions: [...]

The split mirrors Kubernetes exactly: ``spec`` is user intent and is never
written by the system; ``status`` is observed state and is never written by the
user.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator

API_VERSION = "nestick.io/v1"
KIND = "ScrapeJob"

NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


class Phase(str, Enum):
    """Lifecycle of a job, mirroring a Kubernetes pod phase."""

    PENDING = "Pending"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    PAUSED = "Paused"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class ConditionType(str, Enum):
    READY = "Ready"
    PROGRESSING = "Progressing"
    DEGRADED = "Degraded"
    VALIDATED = "Validated"
    SCHEDULED = "Scheduled"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(slots=True)
class Condition:
    """A status condition: ``type`` is ``status`` because ``reason``.

    Exactly the Kubernetes convention — the richest way to express *why* a
    resource is in the state it is.
    """

    type: str
    status: str = "Unknown"  # "True" | "False" | "Unknown"
    reason: str = ""
    message: str = ""
    last_transition: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.status == "True"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": str(self.type),
            "status": self.status,
            "reason": self.reason,
            "message": self.message,
            "lastTransitionTime": _iso(self.last_transition),
        }


@dataclass(slots=True)
class OutputSpec:
    formats: list[str] = field(default_factory=lambda: ["csv", "json"])
    path: str = "leads"

    def to_dict(self) -> dict[str, Any]:
        return {"formats": list(self.formats), "path": self.path}


@dataclass(slots=True)
class CrawlSpec:
    concurrency: int = 24
    perHost: int = 3
    maxPagesPerSite: int = 6
    depth: int = 1
    timeout: float = 15.0
    retries: int = 3
    delay: float = 0.0
    respectRobots: bool = True
    cache: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class JobSpec:
    """User intent. The system never mutates this outside of webhooks."""

    queries: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    engine: str = "auto"
    pages: int = 1
    location: str | None = None
    language: str = "en"
    country: str = "us"
    places: bool = False
    want: list[str] = field(default_factory=lambda: ["email", "phone", "social"])
    minConfidence: float = 0.0
    maxEmailsPerLead: int = 25
    crawl: CrawlSpec = field(default_factory=CrawlSpec)
    output: OutputSpec = field(default_factory=OutputSpec)
    #: ``@hourly`` ``@daily`` ``@weekly`` ``every 30m`` — empty means run once.
    schedule: str = ""
    suspend: bool = False
    #: Retry the whole job this many times if it fails outright.
    backoffLimit: int = 2

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["crawl"] = self.crawl.to_dict()
        d["output"] = self.output.to_dict()
        return {k: v for k, v in d.items() if v not in (None, "")or k in ("schedule",)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "JobSpec":
        d = dict(d or {})
        crawl = CrawlSpec(**_pick(d.pop("crawl", {}) or {}, CrawlSpec))
        out = OutputSpec(**_pick(d.pop("output", {}) or {}, OutputSpec))
        return cls(crawl=crawl, output=out, **_pick(d, cls))


@dataclass(slots=True)
class JobStatus:
    """Observed state. Written only by the controller."""

    phase: Phase = Phase.PENDING
    conditions: list[Condition] = field(default_factory=list)
    observedGeneration: int = 0
    startTime: float | None = None
    completionTime: float | None = None
    lastRunTime: float | None = None
    nextRunTime: float | None = None
    runCount: int = 0
    failureCount: int = 0
    leads: int = 0
    emails: int = 0
    requests: int = 0
    files: list[str] = field(default_factory=list)
    message: str = ""

    # -- condition helpers -------------------------------------------- #
    def condition(self, ctype: str | ConditionType) -> Condition | None:
        return next((c for c in self.conditions if c.type == str(ctype)), None)

    def set_condition(
        self, ctype: str | ConditionType, status: bool | str,
        reason: str = "", message: str = "",
    ) -> None:
        value = status if isinstance(status, str) else ("True" if status else "False")
        existing = self.condition(ctype)
        if existing is None:
            self.conditions.append(
                Condition(str(ctype), value, reason, message))
            return
        if existing.status != value:
            existing.last_transition = time.time()
        existing.status, existing.reason, existing.message = value, reason, message

    def is_ready(self) -> bool:
        c = self.condition(ConditionType.READY)
        return bool(c and c.ok)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": str(self.phase),
            "conditions": [c.to_dict() for c in self.conditions],
            "observedGeneration": self.observedGeneration,
            "startTime": _iso(self.startTime),
            "completionTime": _iso(self.completionTime),
            "lastRunTime": _iso(self.lastRunTime),
            "nextRunTime": _iso(self.nextRunTime),
            "runCount": self.runCount,
            "failureCount": self.failureCount,
            "leads": self.leads,
            "emails": self.emails,
            "requests": self.requests,
            "files": list(self.files),
            "message": self.message,
        }


@dataclass(slots=True)
class ScrapeJob:
    """A declarative scrape job: ``apiVersion``/``kind``/``metadata``/``spec``/``status``."""

    apiVersion: str = API_VERSION
    kind: str = KIND
    metadata: dict[str, Any] = field(default_factory=dict)
    spec: JobSpec = field(default_factory=JobSpec)
    status: JobStatus = field(default_factory=JobStatus)

    # -- metadata accessors -------------------------------------------- #
    @property
    def name(self) -> str:
        return str(self.metadata.get("name", ""))

    @property
    def uid(self) -> str:
        return str(self.metadata.get("uid", ""))

    @property
    def generation(self) -> int:
        return int(self.metadata.get("generation", 1))

    @property
    def labels(self) -> dict[str, str]:
        return dict(self.metadata.get("labels") or {})

    def bump_generation(self) -> None:
        """Increment on a *spec* change — that is what triggers reconcile."""
        self.metadata["generation"] = self.generation + 1

    def ensure_defaults(self) -> None:
        self.metadata.setdefault("name", f"job-{uuid.uuid4().hex[:8]}")
        self.metadata.setdefault("uid", str(uuid.uuid4()))
        self.metadata.setdefault("generation", 1)
        self.metadata.setdefault("creationTimestamp", _iso(time.time()))
        self.metadata.setdefault("labels", {})

    # -- serialisation --------------------------------------------------- #
    def to_dict(self, include_status: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "apiVersion": self.apiVersion,
            "kind": self.kind,
            "metadata": self.metadata,
            "spec": self.spec.to_dict(),
        }
        if include_status:
            d["status"] = self.status.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ScrapeJob":
        if not isinstance(d, dict):
            raise ValueError("resource must be a mapping")
        kind = d.get("kind", KIND)
        if kind != KIND:
            raise ValueError(f"unsupported kind {kind!r} (expected {KIND})")
        api = d.get("apiVersion", API_VERSION)
        if api != API_VERSION:
            raise ValueError(f"unsupported apiVersion {api!r} (expected {API_VERSION})")
        job = cls(
            apiVersion=api,
            kind=kind,
            metadata=dict(d.get("metadata") or {}),
            spec=JobSpec.from_dict(d.get("spec") or {}),
        )
        st = d.get("status") or {}
        if st:
            job.status = _status_from_dict(st)
        job.ensure_defaults()
        return job

    def to_yaml(self, include_status: bool = True) -> str:
        return dump_yaml(self.to_dict(include_status))

    # -- conversion to the runtime Settings object ------------------------ #
    def to_settings(self, **overrides: Any):
        """Project this resource onto an :class:`nestick.config.Settings`."""
        from .config import Settings

        s = self.spec
        kw: dict[str, Any] = dict(
            queries=list(s.queries),
            urls=list(s.urls),
            engine=s.engine,
            pages=s.pages,
            location=s.location,
            language=s.language,
            country=s.country,
            places=s.places,
            want=tuple(s.want),
            min_confidence=s.minConfidence,
            max_emails_per_lead=s.maxEmailsPerLead,
            concurrency=s.crawl.concurrency,
            per_host_concurrency=s.crawl.perHost,
            max_pages_per_site=s.crawl.maxPagesPerSite,
            depth=s.crawl.depth,
            timeout=s.crawl.timeout,
            max_retries=s.crawl.retries,
            delay=s.crawl.delay,
            respect_robots=s.crawl.respectRobots,
            cache=s.crawl.cache,
            output=s.output.path,
            formats=tuple(s.output.formats),
            progress=False,
            resume=False,
        )
        kw.update(overrides)
        return Settings(**kw)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _pick(d: dict[str, Any], cls: Any) -> dict[str, Any]:
    """Keep only keys that are real fields of ``cls`` (ignore unknown input)."""
    names = {f for f in getattr(cls, "__dataclass_fields__", {})}
    return {k: v for k, v in (d or {}).items() if k in names}


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def _parse_iso(v: Any) -> float | None:
    if not v:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return time.mktime(time.strptime(str(v), "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None


def _status_from_dict(d: dict[str, Any]) -> JobStatus:
    st = JobStatus()
    with_phase = d.get("phase")
    if with_phase:
        try:
            st.phase = Phase(with_phase)
        except ValueError:
            st.phase = Phase.PENDING
    for c in d.get("conditions") or []:
        st.conditions.append(Condition(
            type=c.get("type", ""), status=c.get("status", "Unknown"),
            reason=c.get("reason", ""), message=c.get("message", ""),
            last_transition=_parse_iso(c.get("lastTransitionTime")) or time.time(),
        ))
    st.observedGeneration = int(d.get("observedGeneration") or 0)
    st.startTime = _parse_iso(d.get("startTime"))
    st.completionTime = _parse_iso(d.get("completionTime"))
    st.lastRunTime = _parse_iso(d.get("lastRunTime"))
    st.nextRunTime = _parse_iso(d.get("nextRunTime"))
    st.runCount = int(d.get("runCount") or 0)
    st.failureCount = int(d.get("failureCount") or 0)
    st.leads = int(d.get("leads") or 0)
    st.emails = int(d.get("emails") or 0)
    st.requests = int(d.get("requests") or 0)
    st.files = list(d.get("files") or [])
    st.message = str(d.get("message") or "")
    return st


# --------------------------------------------------------------------------- #
# YAML / JSON I-O  (PyYAML when available, otherwise a small built-in writer)
# --------------------------------------------------------------------------- #
def dump_yaml(data: Any) -> str:
    try:
        import yaml

        return yaml.safe_dump(data, sort_keys=False, default_flow_style=False,
                              allow_unicode=True)
    except ImportError:
        return _mini_yaml(data)


def _mini_yaml(data: Any, indent: int = 0) -> str:
    pad = "  " * indent
    out: list[str] = []
    if isinstance(data, dict):
        if not data:
            return pad + "{}\n"
        for k, v in data.items():
            if isinstance(v, (dict, list)) and v:
                out.append(f"{pad}{k}:")
                out.append(_mini_yaml(v, indent + 1).rstrip("\n"))
            else:
                out.append(f"{pad}{k}: {_scalar(v)}")
        return "\n".join(out) + "\n"
    if isinstance(data, list):
        if not data:
            return pad + "[]\n"
        for item in data:
            if isinstance(item, (dict, list)) and item:
                block = _mini_yaml(item, indent + 1).rstrip("\n")
                out.append(f"{pad}-" + block[len(pad) + 2:].join(("\n", "")) if False else f"{pad}-")
                out.append(block)
            else:
                out.append(f"{pad}- {_scalar(item)}")
        return "\n".join(out) + "\n"
    return pad + _scalar(data) + "\n"


def _scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "" or re.search(r'[:#\[\]{}",\n]|^\s|\s$', s) or s.lower() in (
        "true", "false", "null", "yes", "no", "on", "off"
    ):
        return json.dumps(s)
    return s


def load_documents(text: str) -> list[dict[str, Any]]:
    """Parse one or more YAML/JSON documents (``---`` separated)."""
    text = text.strip()
    if not text:
        return []
    if text.startswith(("{", "[")):
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    try:
        import yaml

        return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    except ImportError as exc:  # pragma: no cover - PyYAML ships with the app
        raise RuntimeError(
            "Reading YAML needs PyYAML (pip install pyyaml), or use JSON."
        ) from exc


def load_jobs(path: str | Path) -> list[ScrapeJob]:
    p = Path(path).expanduser()
    docs = load_documents(p.read_text("utf-8"))
    return [ScrapeJob.from_dict(d) for d in docs]


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class JobStore:
    """A tiny file-backed registry of jobs (the 'etcd' of this system)."""

    def __init__(self, path: str | Path = "~/.nestick/jobs.json") -> None:
        self.path = Path(path).expanduser()
        self._jobs: dict[str, ScrapeJob] = {}
        self.load()

    def load(self) -> None:
        self._jobs.clear()
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except Exception:
            return
        for d in raw.get("items", []):
            try:
                job = ScrapeJob.from_dict(d)
            except Exception:
                continue
            self._jobs[job.name] = job

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"apiVersion": API_VERSION, "kind": "ScrapeJobList",
                   "items": [j.to_dict() for j in self._jobs.values()]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), "utf-8")
        tmp.replace(self.path)

    # -- CRUD ----------------------------------------------------------- #
    def apply(self, job: ScrapeJob) -> tuple[ScrapeJob, str]:
        """Create or update. Returns ``(job, "created"|"configured"|"unchanged")``."""
        job.ensure_defaults()
        existing = self._jobs.get(job.name)
        if existing is None:
            self._jobs[job.name] = job
            self.save()
            return job, "created"
        if existing.spec.to_dict() == job.spec.to_dict():
            return existing, "unchanged"
        # preserve identity + observed state across an update
        job.metadata["uid"] = existing.uid
        job.metadata["creationTimestamp"] = existing.metadata.get("creationTimestamp")
        job.metadata["generation"] = existing.generation + 1
        job.status = existing.status
        self._jobs[job.name] = job
        self.save()
        return job, "configured"

    def get(self, name: str) -> ScrapeJob | None:
        return self._jobs.get(name)

    def delete(self, name: str) -> bool:
        if name in self._jobs:
            del self._jobs[name]
            self.save()
            return True
        return False

    def list(self, selector: dict[str, str] | None = None) -> list[ScrapeJob]:
        jobs = sorted(self._jobs.values(), key=lambda j: j.name)
        if selector:
            jobs = [j for j in jobs
                    if all(j.labels.get(k) == v for k, v in selector.items())]
        return jobs

    def __iter__(self) -> Iterator[ScrapeJob]:
        return iter(self.list())

    def __len__(self) -> int:
        return len(self._jobs)


def parse_selector(text: str | None) -> dict[str, str]:
    """``-l team=sales,tier=free`` → ``{"team": "sales", "tier": "free"}``."""
    out: dict[str, str] = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        k, _, v = part.partition("=")
        out[k.strip()] = v.strip()
    return out
