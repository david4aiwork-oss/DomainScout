# Phase 2 — Ingestion: design

**Status:** 📝 **DRAFT — pending owner approval (2026-07-14).** Brainstormed via superpowers; supersedes nothing.
Parent design: `docs/TECHNICAL-DESIGN.md` §4.1 (module boundaries), §4.2 (ingestion + hard gate), §5 (schema).

**Goal:** Turn the WhoisFreaks free feed into gated `candidates` rows on an idempotent daily run — download →
hard-invariant gate → open-cycle upsert → per-run `ingest_log` counts — reusing the Phase-1 DB helpers. Lock a shared
source-adapter interface so a second source drops in later. **No filtering, RDAP, or scoring** (those are Phases 3–5).

---

## Scope

**In scope:**
- Real, end-to-end **WhoisFreaks** ingestion (both `-expired-` and `-dropped-` daily files).
- A shared `FeedSource` interface + a **Dynadot stub** adapter (`NotImplementedError`) that locks the interface without wiring real data.
- The pure **hard-invariant gate** (`.com` → `^[a-z]+$` → length ≤ 12) applied at ingestion.
- `ingest` CLI subcommand (replaces its Phase-1 stub); `ingest_log` audit rows.
- Add `httpx` as the project's first runtime dependency.

**Out of scope (deliberately deferred):**
- **Dynadot real ingestion** → a later "Phase 2b" spec (auction CSV has a different schema: prices, auction-end dates).
- **Retention prune** of `data/feeds/` → Phase 8 (`prune`); Phase 2 only *writes* the retained copies.
- Dictionary / pronounceability / primary-secondary classification → Phase 3 (tunable gates).
- `lifecycle_status` population → Phase 4 (RDAP). Ingestion leaves it at its `'unknown'` default.

---

## Locked decisions (from TDD + this brainstorm)

- **Sources:** WhoisFreaks free GitHub feed (names-only firehose) now; Dynadot stub only.
- **HTTP client:** `httpx` (used synchronously here; reused for async RDAP in Phase 4).
- **Gate at ingestion:** only survivors land in the permanent DB; raw files retained (360 d, pruned in Phase 8) so a
  future criteria loosening can re-ingest.
- **`feed_category`** set from the filename (`expired` | `dropped`); **`lifecycle_status` untouched** (`'unknown'`) — writing
  `'dropped'` from the filename would re-open the born-closed duplicate bug (TDD §5).
- **Idempotency:** `upsert_candidate` (open-cycle `ON CONFLICT`) + `record_ingest` (keyed on run_date/source/file), both
  from Phase 1; `first_seen` insert-only.
- **Cron:** late-morning run (feed has ~1-day lag).

---

## Architecture

New modules (see TDD §4.1 layout):

```
domainscout/
  ingest.py            # orchestrator + pure gate()  + DEFAULT_FEEDS_DIR
  sources/
    base.py            # FeedSource protocol + FeedFile dataclass  (the shared interface)
    whoisfreaks.py     # real adapter
    dynadot.py         # stub adapter (protocol methods raise NotImplementedError)
```

### Shared interface — `sources/base.py`
Isolates *network* from *pure parsing*, and *source-specific format knowledge* from the generic orchestrator.

- `@dataclass(frozen=True) FeedFile` — describes one downloadable file:
  `source: str`, `feed_category: str` (`'expired'|'dropped'`), `remote_url: str`, `local_name: str`.
- `FeedSource` protocol:
  - `name: str`
  - `feed_files(run_date: date) -> list[FeedFile]` — which files to pull for that date (WhoisFreaks: the expired + dropped pair).
  - `iter_domains(path: Path) -> Iterator[str]` — parse a *local* feed file into raw domain strings. **All source-specific
    format knowledge lives here** (newline names for WhoisFreaks; CSV columns for a future Dynadot).

### Orchestrator + gate — `ingest.py`
- `gate(domain: str, criteria: Criteria) -> tuple[bool, str | None]` — **pure**, no I/O. Order (first failure wins, buckets
  mutually exclusive):
  1. normalize (strip whitespace, lowercase).
  2. ends with `.com`? else → `"rejected_tld"`.
  3. label (name without the `.com` suffix) matches `criteria.charset` (`^[a-z]+$`)? else → `"rejected_charset"`.
  4. `len(label) <= criteria.ingest_max_length` (=12, derived)? else → `"rejected_length"`.
  5. otherwise pass (`True, None`). **Length is measured on the label**, matching the ≤8 / 9–12 criteria.
- `download(feed_file: FeedFile, feeds_dir: Path, client: httpx.Client) -> Path` — GET → write `feeds_dir/local_name`;
  **skip if the file already exists** (idempotent, and this file *is* the retained copy). Network lives only here; the
  `httpx.Client` is **injected** so tests pass a fake and the suite never hits the network.
- `ingest_source(conn, source: FeedSource, run_date, criteria, feeds_dir, client, *, dry_run=False) -> list[IngestCounts]` —
  for each `FeedFile`: `download` → `iter_domains` → `gate` each line, tallying `seen`/`rejected_tld`/`rejected_charset`/
  `rejected_length`/`landed` into an `IngestCounts` → `upsert_candidate` each survivor (unless `dry_run`) → `record_ingest`
  (unless `dry_run`). Returns one `IngestCounts` per file.

### Adapters
- `sources/whoisfreaks.py` — builds the two dated `remote_url`s for a run date and yields newline-delimited names
  (skipping blanks). Feed location (base URL + filename templates) is **configuration**, not a hard-coded literal (see below).
- `sources/dynadot.py` — a class implementing `FeedSource` whose `feed_files`/`iter_domains` raise
  `NotImplementedError("Dynadot ingestion is Phase 2b")`. Locks the interface; carries no real logic.

---

## CLI

`ingest` becomes real (replaces the Phase-1 stub in `commands.py` / `__main__.py`):

```
python -m domainscout ingest [--source whoisfreaks]
                             [--date YYYY-MM-DD]          # default: yesterday (feed ~1-day lag)
                             [--file PATH]                # ingest a LOCAL feed file instead of downloading
                             [--feed-category expired|dropped]   # for --file when name is ambiguous
                             [--dry-run]                  # gate + print counts, write nothing
```

- Default source set = enabled `sources` in `criteria.toml`; `dynadot` is **skipped with a clear "stub — Phase 2b" notice**
  (not an error) so a normal run isn't blocked.
- `--file` is the **offline/TDD path** and the **re-ingest-from-retained-feeds path**; `feed_category` is inferred from the
  filename (`-expired-`/`-dropped-`) with `--feed-category` as override.
- Prints a per-file summary line (`seen / rejected_tld / rejected_charset / rejected_length / landed`) mirroring `ingest_log`.

Uses the global `--db`. Exit non-zero only on a real error (network failure without `--file`, unparseable file); a feed
file that's simply not published yet (404 during the lag window) is a **warning + skip**, not a crash.

---

## Data flow & idempotency

```
run(date) ─► for each enabled source ─► for each FeedFile(date):
   download → data/feeds/<name> (skip if present)
   iter_domains → gate each → tally IngestCounts
   survivors → upsert_candidate (open-cycle ON CONFLICT; first_seen insert-only; lifecycle_status untouched)
   record_ingest(counts)      # keyed (run_date, source, feed_file) → re-run overwrites
```

Re-running the same date converges: file already downloaded (skipped), upserts hit the existing open rows (source/
feed_category refreshed only), `ingest_log` row overwritten. Safe to run repeatedly from cron.

---

## Config & dependency changes

- **`pyproject.toml`:** `dependencies = ["httpx"]` (was empty). `pip install -e .` installs it.
- **Feed location = config, confirmed with real data.** WhoisFreaks base URL + filename templates live in `criteria.toml`
  under `[sources.whoisfreaks]` (loaded by `config.py`); the exact values are **confirmed against the live repo at the first
  build step**, so a wrong URL is a config edit, not a code change. `data/feeds/` path is a module constant
  (`DEFAULT_FEEDS_DIR`), parallel to `DEFAULT_DB_PATH`.
- The gate reads `charset` and `ingest_max_length` from the existing `Criteria` — **no change to the gate's inputs.**

---

## Testing strategy (TDD: red → green → commit per task)

- **`gate()` unit tests (pure):** `.com` label passes; non-`.com` → `rejected_tld`; hyphen/digit/dot/upperc-in-label →
  `rejected_charset`; 13-char label → `rejected_length`; boundaries **8 and 12 pass**; first-failing-bucket wins.
- **`iter_domains` parse tests:** a committed fixture `tests/fixtures/whoisfreaks-sample.csv` with deliberate junk (other
  TLDs, hyphens, digits, blank lines) → yields the right raw names.
- **`ingest_source` integration (temp DB, local file):** survivors land; `ingest_log` counts exact; **idempotent re-run
  converges** (no dup rows, counts stable); `first_seen` preserved; `feed_category` correct; `lifecycle_status` stays
  `'unknown'`; `--dry-run` writes nothing.
- **Network isolation:** `download()` takes an **injected `httpx.Client`**; tests pass a fake/transport — no real HTTP.
- **Dynadot stub:** asserts protocol methods raise `NotImplementedError`.
- **CLI:** `ingest --file <fixture> --db <tmp>` creates rows + an `ingest_log` entry; `--dry-run` leaves the DB empty.

## Build-time real-data confirmations (per "test each phase with real data before proceeding")

The first build task performs **one real download** to confirm, then records the results into `[sources.whoisfreaks]`:
1. **Exact raw URL + filenames** (e.g. `raw.githubusercontent.com/WhoisFreaks/daily-expired-and-dropped-domains/main/<file>`).
2. **Plain-text vs gzipped** — adapter decompresses if needed.
3. Sanity: real `.com` ratio and gate-survival counts on a live file (a rough expectation for the `ingest_log` review).

---

## Self-review

- **Placeholders:** none — the one genuinely-unknown value (feed URL) is explicitly a build-time confirmation, by design.
- **Consistency:** gate order/buckets match TDD §4.2 and the Phase-1 `IngestCounts` fields exactly; reuses Phase-1
  `upsert_candidate`/`record_ingest` unchanged; `lifecycle_status` handling matches the §5 open-cycle amendment.
- **Scope:** single phase, single source (real) + one stub; no Phase 3+ logic bleeds in.
- **Isolation:** network (`download`) / pure parse (`iter_domains`, `gate`) / generic orchestration (`ingest_source`) are
  separable and independently testable.
