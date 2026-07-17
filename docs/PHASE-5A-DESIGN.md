# Phase 5a — Comps grounding: design

**Status:** ✅ **BUILT 2026-07-17** (8 tasks via subagent-driven-development, per-task + final whole-branch
review, live real-data confirmation passed; 187 tests pass + 2 skipped). Approved 2026-07-16 (owner review
round 2: per-file swap + metadata sidecar + crash-window fallback). · **Date:** 2026-07-16
**Open owner item:** the HumbleWorth interim decision (option 3) still wants an explicit **re-ratification**
in `DECISIONS.md` — the owner recommended it and approved the phase around it; see *HumbleWorth* below.
**Companion docs:** [`CLAUDE.md`](../CLAUDE.md) · [`DECISIONS.md`](../DECISIONS.md) · [`docs/TECHNICAL-DESIGN.md`](TECHNICAL-DESIGN.md) §4.5

> **Phase 5 is built in three sub-phases** (scope call, owner-delegated 2026-07-16). Phase 5 as specified
> in TDD §4.5 is three loosely-coupled subsystems plus a rubric, each with its own external dependency:
>
> | Sub-phase | Component | External dep | Anthropic key? |
> |---|---|---|---|
> | **5a (this doc)** | `comps.py` — NameBio comps grounding | NameBio free API | **no** |
> | 5b | `toxicity.py` — Wayback CDX + Safe Browsing gate | free Google Cloud key | **no** |
> | 5c | `scoring/` — Haiku triage → Sonnet deep, Batch API | Anthropic API key | **yes** |
>
> Ordered by dependency + key availability: the two no-key subsystems ship first at $0, and the
> key-gated scoring core comes last, consuming finished pieces. Each sub-phase gets its own
> spec → plan → build → real-data test → push, as with Phases 1–4.

---

## Scope

**In:** a local, cached, $0 source of *real comparable-sale statistics* for a `.com` label, exposed as a
lookup library plus a debug CLI, ready for 5c to inject into the Tier-2 prompt.

**Out:** Tier-1/Tier-2 scoring, prompts, the Batch API, the toxicity gate (5b/5c); writing
`candidates.value_range` (5c does that at scoring time — see *Boundary* below); HumbleWorth /
modeled valuations (deferred — see *HumbleWorth* below).

**Boundary — a library, not a pipeline stage.** 5a ships `comps.py` (cache + lookup) and two CLI
subcommands. It does **not** write `candidates` rows. Comps are needed only for the ~20–30 domains that
reach Tier-2, and Tier-1 decides who those are, so a bulk pre-pass would compute ~3,500 rows to use ~30.
Comps data is also *global context* keyed by freshness, not per-candidate state: it owns no row, has no
lifecycle, and would invent a DB write for data whose natural home is a file cache. TDD §4.1's
"state passes through the DB" governs *between phases*; comps → Tier-2 prompt is within Phase 5.
*(Alternative considered and rejected: a comps stage that bulk-writes `value_range` for all filter
survivors. Local and cheap, but YAGNI — and 5c must write the column regardless.)*

---

## Empirical spike (2026-07-16) — the mandated Phase-5 pre-task

DECISIONS.md ratification #3.2 made this a **CONDITION** on the $0 comps path: *"a ~30-min empirical
spike against the NameBio free endpoint (~20 keywords, confirm limits + CSV path) precedes Phase-5
prompt design; if it underdelivers, promote the own-comps-table hedge to a Phase-5 component."*

**Verdict: the free path over-delivers. The hedge is NOT promoted on NameBio's account.**

| Check | Result |
|---|---|
| `POST /retailstats` (form-encoded, `keyword=`) | ✅ 200 — placements `exact`/`start`/`end`/`middle`, each with `sale_count`/`price_sum`/`price_avg`/`price_max`/`price_stddev` |
| `POST /tldstats` (form-encoded, `extension=`) | ✅ 200 — `.com`: all 1,448,826 sales avg $1,747.98; `all_retail` 189,826 avg $8,587.02; `all_wholesale` 1,259,000 avg $716.82 |
| **`GET /retailstats-download`** | ✅ **6,678,360 bytes (6.7 MB), 97,568 lines, 1.8 s, free, no auth** |
| `GET /tldstats-download` | ✅ 161,637 bytes, 741 lines (741 TLDs) |
| ToS for free data | ✅ *"You may incorporate this data into other products and services, but attribution is required."* The "no product/service" ban is scoped to the **Paid API** only. |
| HumbleWorth free endpoint | ❌ **dead** — see below |

`retailstats-download` header (verbatim), confirming the placement×stat column grid:

```
keyword,exact_sale_count,exact_price_sum,exact_price_avg,exact_price_max,exact_price_stddev,
start_sale_count,start_price_sum,start_price_avg,start_price_max,start_price_stddev,
end_sale_count,end_price_sum,end_price_avg,end_price_max,end_price_stddev,
middle_sale_count,middle_price_sum,middle_price_avg,middle_price_max,middle_price_stddev
```

Sample row: `the,22,396923,18041.95,300000,61724.25,2762,8653841,3133.18,500000,10466.05,...`

### NameBio gotchas (measured 2026-07-16/17 — captured so the plan doesn't rediscover them)

These are **measured**, not read off the docs. Recorded with the same discipline as Phase 4's whodap gotchas.

1. **`/retailstats` and `/tldstats`: 4 requests / 60 s rolling, per-endpoint.** Measured: 4 consecutive
   calls returned 200, the 5th returned 429; recovery to 200 at **~64 s** after the 429. During that
   429, `/tldstats` still returned **200** — buckets are **per-endpoint**, not global. (The documented
   "4/min" is exactly right; an earlier "429 after 2–3 calls" reading was a burst artifact.)
2. **429 carries NO `Retry-After` and NO `X-RateLimit-*` headers.** Server is Cloudflare. Backoff must be
   entirely our own — nothing machine-readable to honour.
3. **The two `*-download` endpoints have an INDEPENDENT, MUCH LONGER window.** Measured: one download of
   each succeeded cold, then **both 429'd for the rest of the session (>30 min)** with *no* intervening
   `/retailstats` or `/tldstats` traffic — i.e. long after the 60 s stats bucket had demonstrably
   recovered. **Exact window: UNCHARACTERIZED** (would need hours of polling). Known: **> 30 min**, and
   definitively longer than the 60 s stats window.
4. **Consequence — a download 429 is NOT retryable in-run.** Seconds-scale retry is useless when recovery
   takes hours. Refuse, keep the old cache, log, exit 0; **the next daily cron run is the retry** (the
   7-day freshness window gives ~7 days of slack, and once a cache passes `refresh_days` *every* daily run
   re-attempts until one succeeds). ⚠️ **This inverts Phase 4's policy**, deliberately: RDAP 429s recover in
   seconds so `ratelimit.RETRYABLE` includes `RateLimitError`; NameBio download 429s recover in hours so
   retrying in-run is actively wrong. **Rule: back off on transport errors; refuse immediately on 429.**

   ⚠️ **`ratelimit.py` is therefore NOT reused, and this is deliberate — not an oversight to "fix".**
   Three independent reasons: (a) `TokenBucket`/`with_backoff` are **async**, while comps is **sync**
   (`ingest.make_client()` → `httpx.Client`); (b) `ratelimit.RETRYABLE` is whodap-specific
   (`RateLimitError`, `BadStatusCode`) and a comps 429 arrives as an httpx **status code**, not an
   exception, so `with_backoff` would never see it; (c) `TokenBucket` paces *between* calls — meaningless
   for two sequential GETs a week that are proven safe back-to-back (gotcha #5). comps.py therefore carries
   its own ~12-line sync `_get_with_retry()` that retries **`httpx.TransportError` only**. A reviewer
   seeing "duplicate backoff logic" should read this note first.
5. **The refresh's two GETs need no sleep between them** — both downloads succeeded back-to-back when cold.
6. **⚠️ `comps-refresh --force` is a footgun**: it can burn the download window and lock you out for hours.
   `comps --domain` is **local-only** (reads the cache, never touches the network) and cannot poison a refresh.
7. **Free RetailStats has no median** (paid `KeywordStats` only). We get count/sum/avg/max/**stddev**.
8. **Stats are viciously right-skewed** — `.com` `price_max` is $70,000,000 against a $1,747.98 avg
   (stddev $77,814). `avg ± stddev` is **not** a sane band and must not be presented as one.
9. **Attribution is mandatory** if the data is displayed. No exact string is specified by NameBio
   (**UNCONFIRMED**); a citation naming NameBio with a link satisfies the stated rule.

---

## HumbleWorth — ratified decision #3.2 is broken (NEEDS RE-RATIFICATION)

**DECISIONS.md #3.2 ratified:** *"HumbleWorth open-source model (hosted endpoint on Windows-local;
self-host via Docker on VPS later)."* **That premise is now false.** This is an owner decision, not a doc
fix, and it gets its own dated DECISIONS.md entry rather than being buried in a correction.

**Evidence the free hosted endpoint is dead:**
- `valuation.humbleworth.com` fails the TLS handshake from **our** network (SSLEOFError; it CNAMEs to
  `ghs.googlehosted.com`, which closes connections for unmapped SNI) — while `humbleworth.com` itself
  resolves and serves fine **through the same proxy**, so this is not our MITM.
- It also fails from **Firecrawl's cloud** (`ERR_CONNECTION_CLOSED`) — an independent vantage point.
- **HumbleWorth's own API docs no longer mention it**: `humbleworth.com/about/api` documents *only* the
  Replicate route (`freePublicEndpointMentioned: false`). No deprecation notice.

**Options:**
| # | Option | Cost | Friction |
|---|---|---|---|
| 1 | **Replicate** (`humbleworth/price-predict-v1`) | $0.10/1k predictions ≈ **$0.09/mo** at 30/day (batch ≤2,560 ⇒ 1 call/day) | needs `REPLICATE_API_TOKEN` signup + `.env`; breaks the $0 property; a 2nd credential alongside the pending Anthropic key |
| 2 | **Docker self-host now** (`r8.im/humbleworth/price-predict-v1`) | free, unlimited, offline | needs Docker Desktop/WSL2 on Windows now — the friction we deliberately deferred to the VPS; model SPDX license **UNCONFIRMED** |
| 3 | **Ship NameBio-only; add HumbleWorth at VPS migration** ✅ *(selected)* | $0 | none |

**Selected: option 3, with option 1 as an easy upgrade.** Rationale: TDD anti-pattern #11 already makes
NameBio real sales the **anchor** and warns against relying on a model estimate alone; HumbleWorth is a
secondary signal whose training data stops early-2024. NameBio stats plus the Tier-2 model's own reasoning
is a workable v1. **The `value_range` schema reserves `"modeled": null` from day one, so adding HumbleWorth
later is a data change, never a schema migration.** A `ValuationProvider` interface is *defined* but
default-OFF and unimplemented (YAGNI: we cannot real-data test a provider we have no token for).

**Also corrected (TDD §4.5 is wrong):** the `auction`/`marketplace`/`brokerage` triple is **not** a
"modeled low/mid/high range". They are **three distinct sale channels** at the **50th / 97.5th / 99.25th**
percentiles. → carried to 5c: a Tier-2 prompt that presents them as one distribution's range would invite
the model to "reconcile" three numbers that were never in tension, defeating the point of injecting comps.

---

## Architecture

New module `domainscout/comps.py`. **Zero new dependencies** — stdlib `csv` + the existing
`httpx`/`truststore` client. One network function; everything else is local and pure.

```
        [ weekly, cron-safe ]                        [ per-domain, local, no network ]

  comps-refresh                                   comps --domain X  /  5c Tier-2 prompt
        │                                                     │
        │  PER-FILE + INDEPENDENT (retailstats FIRST)         ▼
        │                                            resolve_cache_path()
        ├──► retailstats (6.7 MB) ─┐                  current? else .prev + LOUD warn
        │                          │                          │
        └──► tldstats   (161 KB) ─┤                          ▼
             (no sleep between;    │              load_index(path) -> dict[kw, rawline]
              proven cold)         │              load_tld_stats(path) -> dict[ext, dict]
                                   │              load_meta() -> per-file {retrieved,rows,sha}
              for EACH file, independently:                   │
                                   ▼                          ▼
                        fresh? ──yes──► skip        filters.dict_score(label) -> segmentation
                          │ no                                │
                          ▼                                   ▼
                     GET -> tmp                     lookup() -> CompsContext -> value_range
                          │                                   │
                          ▼                                   ▼
                   validate() ── fail ─► REFUSE        5c injects into Tier-2 prompt
                 parse? header? rows>=80%?  (this file only;
                          │ pass             sibling unaffected)
                          ▼
                 swap: current -> .prev, tmp -> current
                 then update namebio_meta.json entry
```

**Per-file independence (owner-required, 2026-07-16 review).** The refresh makes two GETs, and an
all-or-nothing policy has one genuinely wasteful branch: retailstats validates, tldstats 429s, and we
discard a good 6.7 MB download **that cost us the long, uncharacterized rate-limit window of gotcha #3** —
after which the next cron run may 429 on both. A validated download is a **scarce resource**. So each file
gets its own freshness check, its own gate, its own `.prev`, and its own swap; a tldstats failure leaves a
freshly-swapped retailstats in place, and the summary reports the mixed outcome.
**retailstats is fetched first**: if only one file survives the window, it must be the one Tier-2 needs.

### Public surface

```python
# --- cache refresh (the only network path) ---
def refresh_cache(client, criteria, data_dir, *, force: bool = False,
                  now: datetime | None = None) -> RefreshResult      # .files: per-file results
def refresh_one(client, spec: FileSpec, data_dir, meta, *, force, now) -> FileRefreshResult
def validate_download(tmp_path, *, expected_header: tuple[str, ...],
                      baseline_rows: int | None, min_rows: int,
                      shrink_tolerance: float) -> tuple[bool, str]   # (ok, reason)

# --- metadata sidecar (source of truth for freshness + shrink baseline) ---
def load_meta(data_dir) -> dict[str, dict]      # {'retailstats': {retrieved, rows, sha256, bytes}}
def write_meta(data_dir, meta) -> None          # atomic tmp+rename

# --- local lookup (no network) ---
def resolve_cache_path(current: Path, prev: Path) -> tuple[Path, bool]  # (path, used_prev)
def load_index(path) -> dict[str, str]          # keyword -> raw CSV line (parsed on demand)
def load_tld_stats(path) -> dict[str, dict]     # '.com' -> {period: {stat: value}}
def parse_placement(line: str, placement: str) -> KeywordComps | None
def lookup(domain, index, tld_stats, criteria, *, retrieved: str | None) -> CompsContext
def cache_age_days(meta, name, now) -> float | None
```

**Memory:** the index is `keyword -> raw CSV line` (~97.5k entries, **≈15 MB**), parsed only on lookup.
Materialising all 97,568 × 21 cells as Python floats would cost hundreds of MB for data we touch ~60 cells of.

### The crux — keyword → placement mapping

NameBio keys stats by keyword **and placement**, which maps exactly onto where a word sits in the label.
Segmentation is **reused from Phase 3's `filters.dict_score()`** (returns `(score, segmentation)`, pure, no
network) — the project's single source of truth for splitting a label into words. A second splitter would drift.

| Domain | `dict_score` seg | Lookups |
|---|---|---|
| `vault.com` | `vault` | `vault` → **exact** |
| `cloudvault.com` | `cloud+vault` | `cloud` → **start**, `vault` → **end** |
| `austinplumber.com` | `austin+plumber` | `austin` → **start**, `plumber` → **end** |
| `zylo.com` | `zylo` | `zylo` → **exact** (likely 0 sales — itself informative) |

Rules: 1 part ⇒ `exact` for the label. 2 parts ⇒ `start` for the left, `end` for the right. Always
additionally attempt `exact` on the **whole label**. A keyword absent from the index yields `None`
(reported as "no comparable sales", which is real signal, not an error). `middle` is unused (our labels
are ≤2 parts by the Phase-3 splitter).

### `value_range` JSON (written by 5c; shape defined here)

```json
{"source":"namebio-free","retrieved":"2026-07-16","segmentation":"cloud+vault",
 "keywords":[{"keyword":"cloud","placement":"start","sale_count":2762,"price_avg":3133.18,
              "price_max":500000,"price_stddev":10466.05},
             {"keyword":"vault","placement":"end","sale_count":41,"price_avg":3578.02,
              "price_max":39600,"price_stddev":6531.93}],
 "exact":{"keyword":"cloudvault","sale_count":0},
 "tld_baseline":{"extension":".com","all_retail":{"sale_count":189826,"price_avg":8587.02,
                                                  "price_max":70000000,"price_stddev":214317.64}},
 "modeled":null,
 "attribution":"Comparable sales data from NameBio (https://namebio.com)"}
```

- **No fabricated median or range.** Free tier has no median; we ship what we actually have and let Tier-2 reason.
- **`sale_count` is the confidence signal** — 3 sales is noise, 2,762 is real. 5c's prompt must say so.
- **`tld_baseline`** is the calibration anchor (is $3k good for a `.com`? vs. the $8,587 retail average).
- **`modeled: null`** is the reserved `ValuationProvider` slot (gotcha #8's skew warning belongs in 5c's prompt).
- **`retrieved` comes from `namebio_meta.json`'s `retailstats` entry** — *not* a file mtime and *not* a
  single global date. Per-file swaps mean the two caches can legitimately differ in age; retailstats is the
  one Tier-2 reasons from, so its date is the one recorded. `null` (+ warning) if the sidecar is missing.

### Cache integrity — sanity gate + `.prev` + metadata sidecar (owner-required, 2026-07-16 review)

Atomic tmp+rename guarantees a *complete* file, **not a good one**. HTTP 200 with an error-page-as-CSV, an
empty body, or a silently truncated export would atomically replace a good cache with garbage — and with
overwrite-in-place there'd be no previous copy. Therefore, **per file**:

**`validate_download()` runs BEFORE that file's swap** and must pass all of:
1. parses as CSV;
2. header matches the expected column tuple **exactly**;
3. row count ≥ `shrink_tolerance` (0.8) × the **sidecar's recorded** row count for that file.

**First run (no sidecar baseline):** rule 3 has no baseline, so require header + `min_rows` floor
(retailstats ≥1,000; tldstats ≥100) — an error page can never *seed* the cache either.

**On pass:** `current → .prev` (replacing any existing `.prev`), then `tmp → current` (both atomic renames),
then update that file's sidecar entry. Exactly **one** `.prev` per file is kept — ~13 MB total, not
2.4 GB/yr of dated snapshots.
**On fail:** delete tmp, leave **that file's** cache untouched, log the reason, exit 0 (cron-safe; Phase 2's
ingest-404 precedent). The sibling file is **unaffected** — see *Per-file independence* above.

**`--force`** bypasses the freshness no-op **and** the shrink check (a legitimate >20% shrink needs it), but
**never** the parse/header/floor checks. You can never install an error page, by any flag.

#### `data/namebio_meta.json` — the source of truth for freshness and the shrink baseline

Written atomically (tmp+rename) as each file swaps:

```json
{"retailstats": {"retrieved": "2026-07-16T11:03:22", "rows": 97568,
                 "sha256": "9f2c…", "bytes": 6678360},
 "tldstats":    {"retrieved": "2026-07-16T11:03:24", "rows": 741,
                 "sha256": "41ab…", "bytes": 161637}}
```

Why a sidecar rather than file mtimes:
1. **Per-file `retrieved`.** With per-file swaps (or a mid-week `--force` of one file) the two caches can
   legitimately differ in age. One global date would be a lie.
2. **mtimes survive copies/restores badly** — a backup restore or a `cp -r` silently resets "freshness".
3. **It persists `rows`, killing a re-parse.** The shrink baseline was specced as "the current cache's row
   count", which meant re-reading 6.7 MB *just to count lines* on every refresh. The sidecar already has it.
4. **`sha256`** records what we actually installed — for diagnosis and out-of-band-corruption checks.
   Recorded at swap; **not** verified on every load (that would turn a hand-inspected cache into a hard failure).

**Degradation:** a missing/unparseable sidecar ⇒ **refresh** treats both files as stale and falls back to
first-run rules (`min_rows` floor); **load** still works, reporting `retrieved: null` plus a warning.
The sidecar is an optimisation and an audit record — never a hard dependency for reading the cache.

#### Crash window between the two renames (owner nit, 2026-07-16)

`current → .prev` and `tmp → current` are each atomic but **not jointly atomic**: a crash between them leaves
**no current file**. Vanishingly unlikely, but the fix is one branch in the *load* path, which is pure and
cheap to test:

```python
def resolve_cache_path(current, prev):
    if current.exists():
        return current, False
    if prev.exists():
        log.warning("comps cache %s missing but %s exists — loading .prev "
                    "(crash between swap renames?); run comps-refresh --force to repair",
                    current.name, prev.name)
        return prev, True
    raise CompsCacheMissing(f"no comps cache at {current} or {prev}; run comps-refresh")
```

**Permanence** (TDD §4.5: *"Keep these CSVs permanently"*) is satisfied by always holding a full local copy
plus one predecessor: if NameBio changes terms or vanishes, our cache still works. Dated daily snapshots
would be 2.4 GB/yr for data that moves glacially.

#### Staleness must be *visible* (owner note, 2026-07-16)

`expected_header` matching **exactly** means a NameBio column addition bricks refresh until a code change.
That is the correct conservative failure mode — but from cron it fails **silently forever** (exit 0, a log
line nobody reads), and the pipeline would quietly score against an ageing cache. So staleness surfaces
where we actually look:

- `comps --domain X` prints cache age per file, e.g.
  `cache: retailstats 3d (97,568 rows, retrieved 2026-07-16) | tldstats 3d (741 rows)`.
- Age > `stale_warn_factor` (3) × `refresh_days` ⇒ a loud `⚠️ STALE` line on **both** `comps --domain` and
  `comps-refresh`.
- **→ carried to Phase 7:** the digest surfaces comps cache age for the same reason.

---

## Config & dependency changes

**Dependencies: none added.** (stdlib `csv`; existing `httpx` + `truststore`.)

```toml
[comps]                             # Phase 5a — NameBio comps grounding
base_url = "https://api.namebio.com"
retailstats_path = "/retailstats-download"
tldstats_path = "/tldstats-download"
refresh_days = 7                    # aggregated stats move glacially; `comps-refresh` is safe to
                                    # call from the daily cron - it NO-OPS unless the cache is older
                                    # than this. Costs nothing to reverse (set 1) if ever wrong.
shrink_tolerance = 0.8              # refuse a refresh whose row count < 80% of the sidecar's recorded rows
min_rows_retailstats = 1000         # first-run floor (no sidecar baseline to compare against)
min_rows_tldstats = 100
stale_warn_factor = 3               # warn loudly once a cache is older than this x refresh_days.
                                    # An exact-header match means a NameBio column ADD bricks refresh
                                    # until a code change - correct, but it fails silently-forever from
                                    # cron (exit 0). This makes that visible where we actually look.

# NameBio free-tier rate limits — MEASURED 2026-07-16/17, not taken from the docs:
#   /retailstats, /tldstats  : 4 req / 60 s ROLLING, PER-ENDPOINT (4th ok, 5th -> 429; recovered
#                              at ~64 s). During a /retailstats 429, /tldstats still returned 200.
#   429 headers              : NONE. Cloudflare returns no Retry-After and no X-RateLimit-* --
#                              backoff must be our own; there is nothing to honour.
#   *-download endpoints     : INDEPENDENT, MUCH LONGER window. One download of each succeeded
#                              cold, then both 429'd for >30 min with no other traffic. Exact
#                              window UNCHARACTERIZED (needs hours of polling); known > 30 min.
#                              => a download 429 is NOT retryable in-run. The next daily cron run
#                              IS the retry (7 days of freshness slack). This deliberately INVERTS
#                              Phase 4's policy, where RDAP 429s recover in seconds.
#                              Both downloads DO succeed back-to-back cold -> no sleep needed
#                              between the refresh's 2 GETs.
#   WARNING: `comps-refresh --force` can burn the download window and lock you out for HOURS.
#            `comps --domain` is local-only (reads the cache) and never touches the network.
```

Cache files (git-ignored, alongside `data/domainscout.db`):
`data/namebio_retailstats.csv` (+ `.prev`), `data/namebio_tldstats.csv` (+ `.prev`),
`data/namebio_meta.json` (the per-file `{retrieved, rows, sha256, bytes}` sidecar).

---

## CLI

```
domainscout comps-refresh [--criteria criteria.toml] [--force] [--dry-run]
    Download + validate + swap the NameBio caches. PER-FILE and independent: each file has
    its own freshness check, gate, .prev and swap, so one file's failure never discards the
    other's good download. retailstats is fetched FIRST (if only one survives the rate-limit
    window, it must be the one Tier-2 needs). Idempotent; a file no-ops if younger than
    [comps].refresh_days. Cron-safe: a 429 or a failed gate refuses THAT file's swap, leaves
    its cache intact, logs why, and exits 0.
    --force  re-download even if fresh, and bypass the shrink check (never the header check).
             WARNING: can burn the download rate-limit window for HOURS (gotcha #3).

domainscout comps --domain NAME [--criteria criteria.toml]
    Print the comps context for one domain, plus per-file cache age (debug).
    LOCAL ONLY - reads the cache, never touches the network.
```

Matches existing precedent: `build-ngrams` (build a local data asset) and `verify --domain` (single-domain debug).

Summary lines — note the **mixed outcome** is first-class, not an error path:
```
comps-refresh: retailstats swapped (97,568 rows, 6.7 MB) | tldstats swapped (741 rows)
comps-refresh: retailstats swapped (97,568 rows, 6.7 MB) | tldstats REFUSED (429; next daily run retries)
comps-refresh: retailstats skipped (fresh, 2d < 7d) | tldstats skipped (fresh, 2d < 7d)  [--force to override]
comps-refresh: retailstats REFUSED (header mismatch: got 'html') | tldstats swapped (741 rows)
comps-refresh: ⚠️ STALE - retailstats 23d old (> 3x refresh_days=7); refresh has been failing
```

---

## Testing strategy (TDD: red → green → commit per task)

**Zero network in the suite** (the Phase 1–4 rule), via a small fixture CSV + an injected fake client.

| Area | Tests |
|---|---|
| index | loads fixture → `dict[kw, line]`; missing keyword → `None`; parse placement columns correctly |
| placement mapping | 1-part → `exact`; 2-part → `start`+`end`; whole-label `exact` always attempted; segmentation reuses `filters.dict_score` |
| lookup | builds `CompsContext`; absent keyword → "no comparable sales" not an error; `tld_baseline` attached; `modeled` is `null` |
| JSON | `value_range` round-trips; `modeled` key present-and-null (guards the reserved slot against silent removal) |
| sanity gate | bad header → refuse; empty/truncated → refuse; rows < 80% of **sidecar** baseline → refuse; first-run floor enforced; `--force` bypasses shrink but **NOT** header |
| swap | on pass: `.prev` retained, current replaced, sidecar entry updated; on fail: cache byte-identical, tmp removed, sidecar untouched |
| **per-file independence** | **retailstats OK + tldstats 429 ⇒ retailstats IS swapped** (the wasteful branch this exists to prevent); each file's `.prev`/sidecar entry moves independently; retailstats is fetched first |
| **metadata sidecar** | written atomically at swap; `rows` used as the next shrink baseline (**no 6.7 MB re-parse**); missing/corrupt sidecar ⇒ refresh falls back to first-run rules **and** load still works with `retrieved: null` + warning |
| **crash window** | current absent + `.prev` present ⇒ load uses `.prev` and warns loudly; both absent ⇒ `CompsCacheMissing` |
| refresh | a file no-ops when fresh; `--force` overrides; **429 → refuse that file + exit 0 + its cache intact** (no in-run retry); transport error → retried by comps' own sync helper (see below) |
| staleness | age > `stale_warn_factor` × `refresh_days` ⇒ `⚠️ STALE` on both `comps --domain` and `comps-refresh` |
| CLI | `comps-refresh --dry-run` writes nothing (incl. no sidecar write); `comps --domain` makes **zero** network calls |

**Live smoke** (`@pytest.mark.skip`, run manually — the Phase-4 `test_live_smoke_*` pattern): real
`comps-refresh` against NameBio, assert ~97.5k rows land and the header matches.

## Build-time real-data confirmations (per "test each phase with real data")

1. `comps-refresh` against live NameBio → 6.7 MB / ~97,568 rows + 741 TLDs land; `namebio_meta.json` written
   with real `rows`/`sha256`; `.prev` created on the 2nd forced run.
2. `comps --domain cloudvault.com` → real `cloud`(start) + `vault`(end) stats + cache age line.
3. `comps --domain austinplumber.com` → the geo+service secondary-track case.
4. `comps --domain vault.com` → single-word `exact` path.
5. `comps --domain zylo.com` → invented name, expect **no comps** — confirm it reads as *absence of evidence*,
   not an error (and that 5c's forward-carried note is warranted).
6. Re-run `comps-refresh` → **no-op** on both files (idempotency).
7. Corrupt the cache header by hand → refresh **refuses that file**, cache intact, reason logged.
8. Delete `namebio_retailstats.csv` leaving `.prev` → `comps --domain` loads `.prev` with a loud warning
   (the crash-window path).

⚠️ The download window is currently exhausted by the spike (gotcha #3); the live confirmation must wait
for it to clear. The suite itself is unaffected (fixtures + fakes).

---

## Doc corrections this phase must land

1. **DECISIONS.md — new dated entry** superseding **#3.2's HumbleWorth leg**, flagged **NEEDS
   RE-RATIFICATION**, recording the dead endpoint + the 3 options + selected option 3. *Not* a footnote.
2. **TDD §4.5** — HumbleWorth "modeled low/mid/high range" → **three sale channels at P50/P97.5/P99.25**.
3. **TDD §4.5 / DECISIONS.md** — NameBio free tier **does** provide programmatic + bulk-CSV access
   (`/retailstats-download`, `/tldstats-download`), and its ToS **permits** free data in products/services
   with attribution; the ban is **Paid-API-only**.
4. **DECISIONS.md pricing snapshot** — "Basic $10/mo + export ← the budget play" is **stale**: tiers are now
   Domainer/Business/Enterprise (prices UNCONFIRMED), and "Free = 5 results/search, web only" conflates the
   **website** cap with the **API**, which has no such cap. The paid tier is **not** needed for comps.

## Forward-carried notes (not 5a work — recorded so they aren't rediscovered)

- **→ Phase 7 (digest):** NameBio **attribution is mandatory** when the data is displayed. The free tier's
  permission is *conditioned* on it, so it is a licence obligation, not a courtesy. It must appear in the
  digest template. `CompsContext.attribution` carries the string so the digest can't forget it.
- **→ Phase 5c (Tier-2 prompt):** (a) HumbleWorth's channels are **P50/P97.5/P99.25 of three different
  sale channels**, not a low/mid/high band — never present them as one range; (b) the stats are viciously
  right-skewed (`.com` max $70M vs. $1,748 avg) so `avg ± stddev` must not be offered as a band;
  (c) `sale_count` is the confidence signal and the prompt must say so; (d) **"no comps found" means
  *absence of evidence for this exact keyword pattern*, NOT "worthless"** *(owner note, 2026-07-16)* —
  invented brandables are **systematically underrepresented** in keyword-keyed retail stats, so a naive
  prompt would penalise precisely the **secondary-track invented names the pipeline exists to catch**.
  This is a live failure mode, not a hypothetical: `zylo` → `exact`, 0 sales. The prompt must distinguish
  *"no comparable sales exist for this pattern"* from *"comparable sales exist and are low."*
  (e) The digest should surface **comps cache age** for the staleness reason above.
- **→ Phase 4 (follow-up proposal — owner STRONGLY ENDORSES chasing this, 2026-07-16):** NameBio exposes a
  **free, no-auth `POST /verisign`** (+ `/verisign-download`) returning the **exact Verisign pending-delete
  drop order for `.com`** (5 requests × ≤100 domains / 24 h / IP — comfortably covers the Tier-2 pool and
  then some). Phase 4's build notes record that Verisign's RDAP **omits RGP phase-start dates**, forcing our
  `today`-anchored estimates — this endpoint **directly fixes the one acknowledged weakness in Phase 4's
  design** by pinning real drop dates, sharpening the backorder decision. ⚠️ It is **another undocumented-window
  NameBio endpoint**, so the same **measure-first** discipline as gotcha #3 applies: characterize the limit
  before designing the retry policy. **Not chased in 5a**; logged as the Phase-4 follow-up.

---

## Self-review

- **Spec coverage:** every TDD §4.5 comps clause is addressed — NameBio free stats ✅, cached CSV ✅,
  attribution ✅, HumbleWorth ⚠️ (deferred with an explicit, re-ratification-flagged decision + reserved
  schema slot), injection into Tier-2 ✅ (shape defined; 5c consumes), empirical spike ✅ (done, recorded).
- **Ratified-condition check:** #3.2's CONDITION is **discharged** — the spike ran; the free path
  over-delivers; the own-comps hedge is **not** promoted for NameBio. The hedge remains prudent for the
  *undocumented* HumbleWorth endpoint, which option 3 sidesteps entirely for now.
- **Placeholders:** none. Every constant is measured or config; the only UNCONFIRMED items are explicitly
  labelled (download-window length, NameBio's exact attribution string, HumbleWorth's SPDX license) and
  none block the build.
- **Type consistency:** `filters.dict_score(label, criteria) -> (float, str)` is consumed for its
  segmentation only; `CompsContext`/`KeywordComps`/`RefreshResult` land in `models.py` beside the existing
  dataclasses; `refresh_cache` takes an injected client (the Phase-2/4 testability pattern).
- **Scope:** one module, two CLI subcommands, no new deps, no schema migration (`value_range` already
  exists). Comfortably one plan.
- **Ambiguity:** the "library vs stage" boundary and the "429 refuse vs retry" inversion are the two places
  a reader could reasonably assume the opposite, so both are stated explicitly with their rationale.

### Review round 2 (owner, 2026-07-16) — folded in

| # | Finding | Resolution |
|---|---|---|
| **Gap 1** | Partial-success policy across the two downloads unstated; all-or-nothing would **discard a validated 6.7 MB download** whose sibling 429'd — wasting the scarce, uncharacterized window of gotcha #3 | **Per-file independent validate-and-swap**: own gate, own `.prev`, own swap, own sidecar entry; mixed outcome is a first-class summary line. **retailstats fetched first** so a one-file window yields the file Tier-2 needs |
| **Gap 2** | `retrieved` had no real per-file source of truth; mtimes survive copies/restores badly; the shrink baseline required re-parsing 6.7 MB **just to count lines** | **`data/namebio_meta.json` sidecar** — per-file `{retrieved, rows, sha256, bytes}`, written atomically at swap. `CompsContext.retrieved` = the **retailstats** entry (the one Tier-2 reasons from). Degrades gracefully: missing ⇒ refresh uses first-run rules, load still works with `retrieved: null` + warning |
| **Nit** | `current → .prev` then `tmp → current` are atomic but **not jointly** — a crash between leaves no current file | `resolve_cache_path()` in the **load** path (pure, cheap to test): current absent + `.prev` present ⇒ load `.prev` + loud warning; both absent ⇒ `CompsCacheMissing`. Test case added |
| **Note** | Exact-header match is the right conservative failure, but from cron it fails **silently forever** (exit 0) | `stale_warn_factor = 3`: `⚠️ STALE` on `comps --domain` **and** `comps-refresh`; cache age surfaced per file; carried to the Phase 7 digest |
| **Note** | A naive 5c prompt would read "no comps" as "worthless", penalising the **invented secondary-track names the pipeline exists to catch** | Added to forward-carried 5c notes as item (d), with `zylo` as the concrete live case; real-data confirmation #5 exercises it |
| **Note** | Verisign drop-order endpoint: owner **strongly endorses** as the Phase-4 follow-up | Recorded, with the ⚠️ that it is **another undocumented-window endpoint** ⇒ same measure-first discipline as gotcha #3 |

---

## Build notes (2026-07-17)

Built via **subagent-driven-development** (8 tasks, a fresh implementer + a spec/quality reviewer per
task, controller verification between each, plus review fixes). Implementers ran on the cheap tier
(complete code in each brief = transcription+testing); reviewers and fixes on the mid tier.

- **Tests:** `python -m pytest -q` → **187 passed, 2 skipped** (the RDAP live smoke + this phase's
  `test_live_smoke_refresh_and_lookup`). **Zero network in the suite** (fixtures + injected `httpx.MockTransport`).
  (+1 over the per-task total: the final-review cron-safe-encoding regression test.)
- **Dependencies:** **none added** — stdlib `csv`/`json`/`hashlib`/`logging`/`datetime` + the existing
  `httpx`/`truststore` client (`ingest.make_client`).
- **Commits:** `18b06e3` (T1 config) · `0344314` (T2 models+fixtures) · `addb055` (T3 index/parse) ·
  `5b58ff8`+`037195a`+`04ca31c` (T4 lookup + coverage fixes) · `cd39efa` (T5 sidecar/gate/.prev) ·
  `0fa210d`+`393eba8` (T6 per-file refresh + swap-OSError fix) · `a5e19a4`+`3607aac` (T7 CLI + fixes) ·
  this commit (T8 smoke + docs).

**Review fixes (all teeth-checked — each proven to fail before the fix):**
- **T4 (Important):** the whole-label `exact` compound path had no positive coverage — the `exact` field's
  entire reason to exist (a compound like `cloudvault` that is *itself* a NameBio keyword) was untested.
  Added a `cloudvault` fixture row + `test_lookup_surfaces_whole_label_compound_as_exact`; then the dedup
  half too (`assert ctx.exact is None` for a single-word domain). Production code unchanged.
- **T6 (Important):** the swap block (`_count_rows`/`_sha256`/the two renames) sat *outside* `refresh_one`'s
  try/except, so an `OSError` mid-swap — **realistic on this Windows dev box, where AV can lock a file during
  rename** — propagated out of `refresh_cache`, breaking per-file independence and losing the sibling's meta.
  Wrapped the swap in `try/except OSError` → refuses that file only; `.prev` recovers reads.
- **T7 (2 Important):** `comps --domain` on a missing cache dumped a raw traceback → now a clean stderr
  message + exit 1 (the `CompsCacheMissing` text already names the remedy); and the `comps-refresh --dry-run`
  "writes nothing" constraint had no automated guard → added one.
- **Final whole-branch review (Important, whole-branch-only):** the `⚠️ STALE` warning was the **only**
  non-ASCII runtime `print` in the package; on Windows with **redirected** stdout (Task Scheduler/cron →
  log file, cp1252) it raised `UnicodeEncodeError` → non-zero exit, breaking the cron-safe contract in
  exactly the stale-cache case it exists to surface (`capsys` is UTF-8, so the suite never caught it; this
  box runs Python 3.14, pre-UTF-8-default). Marker → ASCII `!! STALE`; added a portable cp1252-encodability
  regression test (`test_stale_warning_is_cron_log_safe_encoding`). The final review confirmed all 7 binding
  invariants hold end-to-end and triaged all 9 deferred Minors as safe-to-defer (highest: the `tld_baseline`
  shallow-copy → **fix in 5c's first commit** before any consumer mutates it).

**Deliberate deviations from the plan/design (each recorded at the time):**
- **`ratelimit.py` is NOT reused** — it is async, its `RETRYABLE` is whodap-specific, and a comps 429 is an
  httpx *status code*, not an exception. comps carries a ~12-line sync `_get_with_retry` that retries
  `httpx.TransportError` only and raises `RateLimited` immediately on 429 (never retried — the daily cron is
  the retry). The design's testing-table line claiming `with_backoff` reuse was corrected before the build.
- **`parse_placement` returns `None` on `sale_count == 0`** — a zero-sale placement is *absence of data*, not
  a $0 comparable; surfacing it as zero would let 5c read "$0 average" as a real comp.
- **`[comps]` placed after `[retention]`** in `criteria.toml` (a test trims on `split("[comps]")` and would
  otherwise drop the `_require`d `[retention]` section) — TOML section order is semantically irrelevant.

**Live real-data confirmation — ✅ PASSED (2026-07-17).** Once the download window cleared, `comps-refresh`
ran against live NameBio and every mandated confirmation passed:
- **Real download:** `retailstats swapped (97,576 rows, 6.7 MB) | tldstats swapped (740 rows, 0.2 MB)`
  (97,576 vs the spike's 97,568 — NameBio's hourly aggregates grew slightly, as expected; `.com` retail
  baseline `n=189,842`).
- **`comps --domain` on the real cache:** `cloudvault.com` → `cloud`@start (n=289, avg $4,111) + `vault`@end
  (n=76, avg $2,851); `austinplumber.com` → `austin`@start (n=31) + `plumber`@end (n=16) — the geo+service
  secondary case; `vault.com` → `vault`@exact (n=4); `zylo.com` → *"no comparable sales for this keyword
  pattern (absence of evidence — invented names are underrepresented, NOT worthless)"*.
- **Idempotency:** a plain re-run → `retailstats skipped (fresh, 0d < 7d) | tldstats skipped (fresh, 0d < 7d)`.
- **429 handling, live:** the two downloads re-burned the window (gotcha #3 in action), so the next `--force`
  run → `retailstats REFUSED (429; cache intact, next daily run retries) | tldstats REFUSED (429; ...)` —
  cache left intact, exit 0. Real confirmation of the refuse-don't-retry policy.
- **Corrupt-header rejection:** `validate_download` on a hand-corrupted real cache → refused
  (`header mismatch (got 'not', 4 cols; expected 21)`), cache untouched.

`.prev`-on-successful-2nd-swap is covered by `test_refresh_keeps_one_prev_on_swap` (the live 2nd run 429'd
instead, which gave us the live refuse-and-keep confirmation above). The live smoke stays `@pytest.mark.skip`
(it needs a cleared window and network); the suite remains fixtures+fakes.

**Deferred Minors (logged in the SDD ledger for the final whole-branch review to triage — none blocking):**
`tld_baseline` nested-dict shallow-copy aliasing across `CompsContext`s (latent until a caller mutates in
place); the tldstats header double-parse in `refresh_one`; `cmd_comps` reads `load_meta` twice; the T1 config
tests assert against values equal to the dataclass defaults (can't distinguish "parsed" from "leaked").
