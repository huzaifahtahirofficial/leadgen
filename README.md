# Nestick Tech Lead Generator

### Powered by the SkelerSecurity Intelligence Engine

**A premium, high-efficiency contact &amp; lead intelligence platform in Python.**

One async engine that fuses everything from the five source files you provided:

| Source | What was salvaged |
|---|---|
| `clauneck.txt` (Ruby) | SerpApi paged discovery + async job polling, proxy/UA rotation, retry loop, regex artefact set, CSV export |
| `script.js` (Puppeteer) | Stealth headers, SERP → site crawl, **contact-page discovery**, e-mail junk filters, XLSX export, keyword resume |
| `app.js` (Electron) | Google **Places** business leads (geo/rating/address) + **Hunter.io** domain enrichment, JSON/CSV export |
| `main.js` (Electron) | Settings/state persistence model, save-and-export flow |
| `autocomplete.js` | Prefix-ranking idea → reused for **contact-link scoring** |
| `main.go` (Harvester) | **Env-var-backed flags**, `--threadiness`, signal-driven shutdown, **diagnostics port** (`/healthz`, `/metrics`) |

Everything runs from one command. No Node, no Electron, no browser required.

---

## Quick start — the interface

No flags to memorise. One command opens a control panel in your browser:

```bash
python -m nestick ui
```

![control panel](ui-preview.html)

* **Guided tour** on first launch, plus an **“i” button beside every setting** explaining
  what it does in plain words — hover, focus or tap (with a native tooltip fallback so the
  text is reachable even if scripting is blocked). Replay any time with **? Guide**
* **Crimson glass UI** — glassmorphic panels with backdrop blur over claymorphic,
  soft-shadowed controls
* **Search** tab for a query, **URL list** tab to paste sites — everything else has a sane default
* Live results table (sortable, filterable) that fills in **while the run happens**
* Score badges: green ≥60, amber ≥30 — your best leads float to the top
* **Stop** any time; partial results are kept and still exportable
* One-click **CSV / Excel / JSON** download, and an API-keys dialog that saves to `~/.nestick/config.json`
* Activity log panel so you can see exactly what the engine is doing

**Troubleshooting `ERR_CONNECTION_REFUSED`:** keep the console window open —
it hosts the server. If the app window still cannot connect, run
`--mode browser` or `--mode server` and open the printed address yourself.

The panel binds to `127.0.0.1` only. Useful options: `--ui-port 9000`,
`--ui-host 0.0.0.0` (LAN access), `--no-browser`.

It's built entirely on the Python standard library — **no Flask, no Electron, no npm**.

---

## Quick start — the CLI

```bash
pip install httpx[http2] rich openpyxl orjson       # only httpx is strictly required

# 1. Scrape a search query (keyless — uses DuckDuckGo)
python -m nestick -q "dentists in Lahore" --pages 2 -f csv,xlsx

# 2. Scrape a list of sites
python -m nestick -u https://acme.com https://globex.com

# 3. Big run from a file, 64-way concurrency, every format
python -m nestick -i urls.txt --threadiness 64 -f all -o out/leads
```

### Free intelligence sources

The SkelerSecurity Intelligence Engine layers free, keyless data on top of every
crawl. Nothing here is required, and any provider that is down simply degrades to
"unknown" rather than failing the run.

| Source | Key? | Adds |
|---|---|---|
| **DNS-over-HTTPS** (Google + Cloudflare) | no | MX records prove a domain can actually receive mail; detects Google Workspace / Microsoft 365 / Proofpoint etc. |
| **OpenStreetMap** (Nominatim + Overpass) | no | Business listings with address, coordinates, phone and website |
| **Wikidata / Wikipedia** | no | Legal entity name, industry and description (`--firmographics`) |
| **NumVerify** | free tier | Carrier, line type (mobile/landline) and country for phone numbers |
| SerpApi · Hunter.io · Google Places | paid | Google SERPs, named staff e-mails, rich place data |

MX verification is on by default and is genuinely useful: an address on a domain
with no mail server is dead on arrival, so its confidence is downgraded
automatically. Disable with `--no-verify-mx`.

```bash
export NUMVERIFY_KEY=...      # optional: phone carrier + line type
nestick -q "dentists in Lahore" --places --firmographics
```

### Intelligence report

Every run produces an aggregate analysis, shown in the UI and available at
`/api/status`:

* **Reachability** — contactable %, split by e-mail / phone / social
* **Deliverability** — MX-verified domains vs dead ones
* **E-mail quality** — role (`info@`) vs named people vs free mailboxes
* **Lead scoring** — average, median, hot/warm/cold bands
* **Firmographics** — mail platforms, social networks, TLD spread, sources

A live run on *"schools in Riyadh"*: **21 leads, 90.5% contactable, 95.2%
MX-verified, 52 unique e-mails** (20 role, 32 named), across Google Workspace
and Microsoft 365 tenants.

### API integrations

All optional and auto-detected from the environment — the scraper is fully
functional without any of them.

```bash
export SERPAPI_KEY=...      # Google results via SerpApi
export HUNTER_API_KEY=...   # e-mail enrichment for domains with no public address
export GOOGLE_MAPS_KEY=...  # Places businesses: address, geo, rating, phone

python -m nestick -q "law firms nyc" --pages 3 --places -f all
```

| API | What it adds | Handled details |
|---|---|---|
| **SerpApi** | Google organic + local results | async job polling (`status: Processing`), multi-page fetch, `cached_page_link` fallback |
| **Hunter.io** | Named contacts per domain | per-e-mail confidence, position/department, org metadata, rate-limited to 5 concurrent |
| **Google Places** | Business name, address, geo, rating, phone | Text Search + Details, `next_page_token` pagination, prefers the international phone format |
| **OpenStreetMap** *(keyless fallback)* | Same shape of business data, no key required | Nominatim geocodes the city to a bounding box, Overpass returns POIs with `website`/`phone`/`email` tags. Rate-limited to 1 req/s with an identifying User-Agent, per OSM policy |

**Failures are never silent.** Each vendor reports errors differently — SerpApi
uses `{"error": ...}` with HTTP 401, Hunter uses `{"errors": [{"details": ...}]}`,
and Google Places returns **HTTP 200** with `REQUEST_DENIED` in the body. All three
are decoded to a plain message, shown in the CLI and the UI, and the run
**falls back to the keyless engine** rather than returning zero leads:

```
╭─────────────────────── API problem ───────────────────────╮
│ • SerpApi: HTTP 401: Invalid API key. Your API key should  │
│   be here: https://serpapi.com/manage-api-key              │
│                                                            │
│ The run continued without that API.                        │
╰────────────────────────────────────────────────────────────╯
```

Endpoints are overridable (`NESTICK_SERPAPI_URL`, `NESTICK_HUNTER_URL`,
`NESTICK_PLACES_URL`, `NESTICK_PLACES_DETAILS_URL`) for self-hosted proxies or testing.

---

## Why it's fast

Measured in this workspace on real sites:

| Scenario | Result |
|---|---|
| 15 sites / 47 pages, cold | **3.9 s** (~11 req/s, 0 failures) |
| Same 4-site run, warm cache | **0 requests, 10 cache hits** — 3× faster |

* **Async HTTP/2** with connection pooling and keep-alive (`httpx`)
* **Two-tier concurrency** — a global cap plus a per-host cap, so one slow site never stalls the run
* **AIMD adaptive throttling** — doubles the delay on `429`/`5xx`, decays back on success
* **Early exit** — stops crawling a site once a high-confidence same-domain e-mail is found
* **Ranked crawling** — `/contact`, `/about`, `/impressum`… are fetched first, not the whole site
* **gzip-compressed SQLite cache** — re-runs and resumes cost nothing
* **Streaming-safe limits** — body-size caps and content-type gating reject binaries before parsing

---

## What it extracts

E-mails, phone numbers, and 10 social networks (LinkedIn, Twitter/X, Facebook, Instagram,
TikTok, YouTube, GitHub, Medium, Telegram, WhatsApp), plus page metadata and
schema.org JSON-LD (name, address, geo coordinates, rating, review count).

**Extraction is the part most scrapers get wrong.** Nestick defends against:

**JavaScript-heavy sites work.** Modern SPAs ship their content as escaped JSON
inside `<script>` tags rather than markup, and they publish contact addresses on
legal pages rather than `/contact`. Nestick mines both:

| Site | Before | After |
|---|--:|---|
| linear.app | 0 | `hello@` `sales@` `billing@` `security@` |
| vercel.com | 0 | `privacy@` `security@` `legalnotices@` |
| notion.com | 0 | `privacy@makenotion.com` `team@makenotion.com` |
| stripe.com | 0 | `sales@stripe.com` |
| slack.com · gitlab.com · supabase.com · hubspot.com | 0 | `dpo@` `privacy@` `legal@` `abuse@` |

Two mechanisms make that work: **state-blob mining** (`__NEXT_DATA__`,
`self.__next_f`, `__NUXT__`, Remix, Apollo — including `\u0040`/`&#64;`-escaped
addresses) and **legal-page ranking**, because a privacy policy or imprint is
legally required to carry a contact address while `/contact` is often just a form.

A staff-directory dump is capped at `max_emails_per_lead` (default 25, role
mailboxes kept first) so one careers page cannot skew a run — the true count is
recorded in `extra.emails_truncated`.

| Trap | Handling |
|---|---|
| `logo@2x.png`, `hero@3x.jpg` | Rejected — image-density suffixes |
| Cloudflare `data-cfemail` | XOR-decoded to the real address |
| `john (at) acme (dot) com` | Deobfuscated (bracket **and** spaced-word forms) |
| `ycombinator.com` → `ycombin@or.com` | **Prevented** — textual `at`/`dot` must be delimited |
| `'name' + '@' + 'site.com'` in JS | Reassembled |
| Fibonacci `8 13 21 34 55 89` | Rejected — digit-sequence detector |
| nginx changelog `2026-42533` | Rejected — year-prefixed ID/date guard |
| Facebook page id in a URL | Rejected — URLs stripped before phone parsing |
| `user@example.com`, `name@yourdomain.com` | Rejected — placeholder blocklist |

Every artefact carries a **confidence score** (0–1). A `mailto:` link scores 0.90;
a same-domain address on a contact page is boosted; a free webmail address is
nudged down. Each lead then gets a **0–100 quality score** for sorting.

---

## Output formats

`csv` · `json` · `jsonl` · `xlsx` · `md` · `sqlite` — or `-f all`.

The **XLSX** export is styled: frozen header, auto-filter table, clickable website
hyperlinks, colour-coded scores (green ≥60, amber ≥30, red below) and a Summary sheet.
The **SQLite** export writes normalised `leads` + `contacts` tables for SQL querying.

---

## CLI reference

Every flag has an environment variable (Harvester-style), shown in `[BRACKETS]`.

```
discovery   -q/--query (repeatable)   -u/--urls   -i/--input-file
            --engine auto|serpapi|duckduckgo|bing|urls   -p/--pages
            --location --language --country --places

crawling    -t/--threadiness [THREADINESS]   --per-host   --max-pages
            --depth --timeout --retries --delay
            --proxy (repeatable) --proxy-file --ignore-robots --no-http2

extraction  -w/--want email,phone,social,all   --min-confidence
            --no-deobfuscate --region

output      -o/--output   -f/--format   --no-cache --cache-ttl --no-resume

misc        --diagnostics-port  -v/--verbose --quiet --no-progress
            --log-file --seed --dry-run --version
```

Run `python -m nestick --help` for the full list.

### Operational features

```bash
python -m nestick -q "saas berlin" --diagnostics-port 6060
curl localhost:6060/healthz   # {"status":"ok"}
curl localhost:6060/metrics   # Prometheus gauges
curl localhost:6060/stats     # live JSON counters
```

* **Resume** — interrupted runs skip completed sites (state is scoped per `--output`)
* **Graceful shutdown** — `Ctrl-C` finishes in-flight work and still exports
* **robots.txt** — respected by default (verified: blocks Google `/search`, allows Wikipedia)

---

## Declarative jobs (Harvester-style)

Beyond one-off commands, a scrape can be declared as a **resource** and applied —
the same `apiVersion` / `kind` / `metadata` / `spec` / `status` model Harvester and
Kubernetes use. Reproducible, reviewable, and version-controllable.

```yaml
# jobs.yaml
apiVersion: nestick.io/v1
kind: ScrapeJob
metadata:
  name: lahore-dentists
  labels: {team: sales}
spec:
  queries: ["dentists in Lahore"]
  pages: 2
  schedule: "@daily"          # "" = run once | @hourly | every 6h
  crawl: {concurrency: 24, maxPagesPerSite: 6, respectRobots: true}
  output: {formats: [csv, xlsx], path: out/dentists}
```

```bash
nestick job template > jobs.yaml     # starter manifest
nestick job apply -f jobs.yaml       # create or update  (idempotent)
nestick job get -l team=sales -o wide
nestick job describe lahore-dentists
nestick job run lahore-dentists      # reconcile now
nestick job controller               # continuous loop, honours schedules
nestick job suspend lahore-dentists  # pause without deleting
```

**Four Harvester patterns, adapted to scraping:**

| Pattern | What it does here |
|---|---|
| **Custom resource** | `spec` is your intent and is never written by the system; `status` is observed state and is never written by you |
| **Admission webhooks** | *Mutating*: normalises names, resolves engine aliases, clamps `pages: 9999 → 50`. *Validating*: rejects unknown engines/formats/schedules **before** a crawl starts |
| **Reconcile loop** | Drives status towards spec — runs what is due, retries with `backoffLimit`, re-queues scheduled jobs. Bumping `generation` (any spec edit) triggers a re-run |
| **Status conditions** | `Ready` / `Progressing` / `Degraded` / `Validated` / `Scheduled`, each with reason + message and a transition timestamp |

Invalid specs fail in milliseconds instead of after a 40-minute crawl:

```
$ nestick job apply -f bad.yaml
patched: bad-name: metadata.name normalised to 'bad-name'
patched: bad-name: pages clamped 9999 → 50
warning: bad-name: spec.crawl.respectRobots is false — you are ignoring robots.txt
error:   bad-name: spec.engine 'nonsense' invalid; choose from [auto, serpapi, ...]
error:   bad-name: spec.output.formats has unknown entries ['pdf']
$ echo $?
1
```

## Python API

```python
from nestick import Settings, run

leads = run(Settings(query="dentists in Lahore", pages=2, formats=("csv",)))

for lead in leads:                     # already sorted by score
    print(lead.domain, lead.score, lead.emails, lead.phones)
```

Async, with a live callback:

```python
import asyncio
from nestick import Settings, Pipeline

async def main():
    async with Pipeline(Settings(urls=["https://acme.com"])) as p:
        p.on_lead = lambda lead, stats: print(lead.domain, lead.emails)
        return await p.run()

asyncio.run(main())
```

---

## Architecture

```
nestick/
├── config.py      Settings dataclass, UA pool, blocklists, crawl heuristics
├── models.py      Contact / Lead / Response / Stats  (+ scoring & dedup)
├── http.py        Fetcher, ResponseCache, HostGovernor, RobotsCache
├── extract.py     Extractor — regexes, deobfuscation, JSON-LD, link ranking
├── discovery.py   SerpApi · DuckDuckGo · Bing · Places · Hunter.io
├── pipeline.py    Orchestration, resume, enrichment, signals
├── export.py      CSV / JSON / JSONL / XLSX / Markdown / SQLite
├── cli.py         argparse CLI, Rich dashboard, diagnostics server
├── resources.py   ScrapeJob resource model, conditions, YAML I-O, JobStore
├── webhook.py     admission control — mutating + validating webhooks
├── controller.py  reconcile loop, scheduling, backoff
├── ctl.py         `nestick job …` kubectl-style CLI
├── security.py    SSRF guard, formula-injection escaping, safe paths
├── places.py      Google Places + keyless OpenStreetMap fallback
├── enrich.py      DNS/MX, NumVerify, Wikidata enrichment + analytics
└── web/           browser control panel (stdlib http.server + vanilla JS)
    ├── server.py      JobManager, JSON API, config store
    └── static/        index.html · app.css · app.js
```

### Web API

| Route | Purpose |
|---|---|
| `POST /api/start` | begin a run |
| `POST /api/stop` | graceful stop, keeps partial results |
| `GET /api/status?log=N` | progress, leads and incremental log |
| `GET`/`POST` `/api/settings` | API keys (never echoed back) |
| `GET /api/download/<fmt>` | csv · xlsx · json · jsonl · md · db |

**Testing:** 280 tests, all passing (`python -m pytest tests/ -q`). Coverage includes
the full offline pipeline, cache round-trips, AIMD backoff, export integrity,
CLI/env-var parsing, the web API end-to-end (start → poll → stop → download),
path-traversal and double-start guards, and a regression test for every false
positive found in live runs, a stability suite (hostile payloads, malicious
servers, 40-way concurrency, memory growth, determinism), plus the declarative layer (resource round-trips,
admission mutation/validation, store idempotency, schedule maths and the
reconcile loop).

The three third-party APIs are verified against `tests/mock_api.py`, which
replays the real vendor payloads — happy paths (results, pagination, async job
polling, confidence mapping) *and* failure paths (bad key, quota, `REQUEST_DENIED`
behind HTTP 200), so the integrations are provably correct without paid keys.

---

## Security

A scraper consumes attacker-controlled input by definition — the pages it fetches,
the links it follows, and the text it writes into your spreadsheet. Each of those
boundaries is defended, and every item below was a real finding from auditing the
running system:

| Risk | Defence |
|---|---|
| **SSRF** — a scraped link pointing at `169.254.169.254` (AWS metadata), `localhost`, or RFC1918 space would leak cloud credentials or hit internal services | Every request is checked, including links discovered mid-crawl. Blocks metadata endpoints, private/loopback/link-local/CGNAT ranges, IPv4-mapped IPv6, non-HTTP schemes and non-web ports. Optional DNS resolution defeats rebinding |
| **CSV/Excel formula injection** — a page containing `=cmd\|'/c calc'!A1` executes when the export is opened | Cells beginning `=` `+` `-` `@` `\t` `\r` are prefixed with `'`. Genuine negative numbers and ratings are left alone so sorting still works |
| **CSRF / DNS rebinding** on the desktop UI — any site you browse could POST to `localhost:8765` and read your saved API keys | `Origin` and `Host` are validated on every `/api/` route; cross-site requests get `403`. Static assets stay public |
| **Secret leakage** | API keys are masked in exports, logs and `/api/settings`, and are never echoed back |
| **Decompression bombs** | 50 MB of zeros ships as 50 KB; the body cap is applied to *decompressed* bytes |

Intranet scraping is still possible — `--allow-private` (or
`allow_private_networks=True`) opts in. Cloud metadata endpoints stay blocked even
then. The SSRF check costs 0.06 ms per URL thanks to a resolution cache.

## Legal note

Scraping is regulated. Respect robots.txt (on by default), honour each site's terms of
service, throttle politely with `--delay`, and remember that harvested personal data
falls under GDPR/CCPA and anti-spam laws such as CAN-SPAM. Use for legitimate B2B
research and only on data you have a lawful basis to collect.
"# lead_gen" 
