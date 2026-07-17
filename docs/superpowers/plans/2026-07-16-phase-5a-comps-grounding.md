# Phase 5a — Comps Grounding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `domainscout/comps.py` — a cached, $0, zero-network-at-scoring-time source of real NameBio comparable-sale statistics for a `.com` label, plus two CLI subcommands, ready for Phase 5c to inject into the Tier-2 prompt.

**Architecture:** One new module. A single network path (`comps-refresh`) downloads two bulk CSVs **per-file and independently** (own gate, own `.prev`, own swap, own sidecar entry), validating each **before** swapping so a bad-but-complete download can never replace a good cache. Everything else is local and pure: an index maps `keyword → raw CSV line` (parsed on demand), and `lookup()` reuses Phase 3's `filters.dict_score()` segmentation to pick NameBio's placement stats by word position.

**Tech Stack:** Python 3.11+, stdlib `csv`/`json`/`hashlib`, existing `httpx` + `truststore` (via `ingest.make_client()`). **Zero new dependencies.** pytest via `python -m pytest`.

**Design doc:** [`docs/PHASE-5A-DESIGN.md`](../../PHASE-5A-DESIGN.md) (approved 2026-07-16, review round 2). Read its **NameBio gotchas** list before Task 4.

## Global Constraints

- **Zero new dependencies.** stdlib + existing `httpx`/`truststore` only.
- **Zero network in the test suite.** Every test uses fixtures + an injected fake client. The only live check is one `@pytest.mark.skip` smoke (Task 8), matching `tests/test_rdap.py::test_live_smoke_known_registered_and_available`.
- **comps is SYNC.** Use `ingest.make_client()` (`httpx.Client`, truststore/OS trust store). **Never `verify=False`.**
- **`ratelimit.py` is deliberately NOT reused.** It is async, its `RETRYABLE` is whodap-specific, and a comps 429 is a *status code* not an exception. comps.py carries its own ~12-line sync `_get_with_retry()` retrying **`httpx.TransportError` only**. This is a documented decision (design doc gotcha #4), **not** duplication to refactor away.
- **429 is NEVER retried in-run.** Recovery takes **hours** (measured >30 min). Refuse that file, keep its cache, log, **exit 0**. The next daily cron run is the retry. This deliberately **inverts** Phase 4's policy.
- **Per-file independence.** A failure on one file must **never** discard the other's validated download. **retailstats is fetched FIRST.**
- **`--force` bypasses the freshness no-op and the shrink check, but NEVER the parse/header/`min_rows` checks.** No flag may install an error page.
- **Exact values** (do not paraphrase): `shrink_tolerance = 0.8`, `refresh_days = 7`, `stale_warn_factor = 3`, `min_rows_retailstats = 1000`, `min_rows_tldstats = 100`, base URL `https://api.namebio.com`, paths `/retailstats-download` and `/tldstats-download`.
- **Attribution string** (verbatim, a licence condition — NameBio's free-data permission is *conditioned* on it): `Comparable sales data from NameBio (https://namebio.com)`
- **`value_range` JSON must always carry `"modeled": null`** — the reserved `ValuationProvider` slot, so HumbleWorth later is a data change, never a schema migration. Task 6 has a guard test; do not "clean up" the null.
- Commit after each task (`git add <files> && git commit`). **Do not push** — push happens at phase end.
- Style: match the existing modules — `from __future__ import annotations`, dataclasses in `models.py`, module docstring stating the phase + the pure/IO split.

## File Structure

| File | Responsibility |
|---|---|
| **Create** `domainscout/comps.py` | Everything 5a: sidecar IO, sync download+retry, validation gate, per-file swap, index load, placement lookup |
| **Modify** `domainscout/models.py` | `+KeywordComps`, `+CompsContext`, `+FileRefreshResult`, `+RefreshResult` |
| **Modify** `domainscout/config.py` | `+[comps]` parsing → `Criteria` fields |
| **Modify** `criteria.toml` | `+[comps]` section + the measured rate-limit comment block |
| **Modify** `domainscout/commands.py` | `+cmd_comps_refresh`, `+cmd_comps` |
| **Modify** `domainscout/__main__.py` | wire `comps-refresh` + `comps` subparsers |
| **Create** `tests/test_comps.py` | all 5a tests |
| **Create** `tests/fixtures/namebio_retailstats_small.csv` | 5-keyword fixture with the real 21-column header |
| **Create** `tests/fixtures/namebio_tldstats_small.csv` | 2-TLD fixture |
| **Modify** `.gitignore` | ignore `data/namebio_*` |
| **Modify** `CLAUDE.md` | Phase 5 checklist → 5a/5b/5c |

Task order: **1** config+toml → **2** models+fixtures → **3** index/parse → **4** lookup (the crux) → **5** sidecar+validation → **6** download+swap+refresh → **7** CLI → **8** docs+live smoke.

---

### Task 1: `[comps]` config

**Files:**
- Modify: `domainscout/config.py` (add fields to `Criteria`; parse in `load_criteria`)
- Modify: `criteria.toml` (new `[comps]` section)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: existing `Criteria` frozen dataclass, `_as_int`/`_as_float`, `ConfigError`.
- Produces: `criteria.comps_base_url: str`, `.comps_retailstats_path: str`, `.comps_tldstats_path: str`, `.comps_refresh_days: int`, `.comps_shrink_tolerance: float`, `.comps_min_rows_retailstats: int`, `.comps_min_rows_tldstats: int`, `.comps_stale_warn_factor: int`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_criteria_has_comps_defaults():
    crit = load_criteria(REPO_ROOT / "criteria.toml")
    assert crit.comps_base_url == "https://api.namebio.com"
    assert crit.comps_retailstats_path == "/retailstats-download"
    assert crit.comps_tldstats_path == "/tldstats-download"
    assert crit.comps_refresh_days == 7
    assert crit.comps_shrink_tolerance == 0.8
    assert crit.comps_min_rows_retailstats == 1000
    assert crit.comps_min_rows_tldstats == 100
    assert crit.comps_stale_warn_factor == 3


def test_comps_section_is_optional(tmp_path):
    """A criteria.toml with no [comps] still loads, using dataclass defaults."""
    src = (REPO_ROOT / "criteria.toml").read_text(encoding="utf-8")
    trimmed = src.split("[comps]")[0]
    p = tmp_path / "c.toml"
    p.write_text(trimmed, encoding="utf-8")
    crit = load_criteria(p)
    assert crit.comps_refresh_days == 7


def test_comps_refresh_days_must_be_int(tmp_path):
    src = (REPO_ROOT / "criteria.toml").read_text(encoding="utf-8")
    p = tmp_path / "c.toml"
    p.write_text(src.replace("refresh_days = 7", 'refresh_days = "weekly"'), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"\[comps\].refresh_days"):
        load_criteria(p)
```

Ensure `tests/test_config.py` imports `pytest` and `ConfigError` (add to the existing import line if absent).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -k comps -v`
Expected: FAIL — `AttributeError: 'Criteria' object has no attribute 'comps_base_url'`

- [ ] **Step 3: Write minimal implementation**

In `domainscout/config.py`, add to the `Criteria` dataclass (after the `rdap_recheck_days` field, keeping all-defaulted fields last):

```python
    comps_base_url: str = "https://api.namebio.com"
    comps_retailstats_path: str = "/retailstats-download"
    comps_tldstats_path: str = "/tldstats-download"
    comps_refresh_days: int = 7
    comps_shrink_tolerance: float = 0.8
    comps_min_rows_retailstats: int = 1000
    comps_min_rows_tldstats: int = 100
    comps_stale_warn_factor: int = 3
```

In `load_criteria`, after the `rdap_*` parsing block and before `return Criteria(`:

```python
    comps_tbl = data.get("comps", {})
    if not isinstance(comps_tbl, dict):
        raise ConfigError("criteria.toml: [comps] must be a table")
    comps_base_url = str(comps_tbl.get("base_url", "https://api.namebio.com"))
    comps_retailstats_path = str(comps_tbl.get("retailstats_path", "/retailstats-download"))
    comps_tldstats_path = str(comps_tbl.get("tldstats_path", "/tldstats-download"))
    comps_refresh_days = _as_int(comps_tbl.get("refresh_days", 7), "[comps].refresh_days")
    comps_shrink_tolerance = _as_float(
        comps_tbl.get("shrink_tolerance", 0.8), "[comps].shrink_tolerance")
    comps_min_rows_retailstats = _as_int(
        comps_tbl.get("min_rows_retailstats", 1000), "[comps].min_rows_retailstats")
    comps_min_rows_tldstats = _as_int(
        comps_tbl.get("min_rows_tldstats", 100), "[comps].min_rows_tldstats")
    comps_stale_warn_factor = _as_int(
        comps_tbl.get("stale_warn_factor", 3), "[comps].stale_warn_factor")
```

Then add these keyword arguments to the `return Criteria(...)` call:

```python
        comps_base_url=comps_base_url,
        comps_retailstats_path=comps_retailstats_path,
        comps_tldstats_path=comps_tldstats_path,
        comps_refresh_days=comps_refresh_days,
        comps_shrink_tolerance=comps_shrink_tolerance,
        comps_min_rows_retailstats=comps_min_rows_retailstats,
        comps_min_rows_tldstats=comps_min_rows_tldstats,
        comps_stale_warn_factor=comps_stale_warn_factor,
```

- [ ] **Step 4: Add the `[comps]` section to `criteria.toml`**

Append to `criteria.toml` (after `[rdap.recheck_days]`, before `[retention]`). The comment block is **required** — it is the empirical basis the owner asked for, in the whodap-gotchas tradition. Copy verbatim:

```toml
[comps]                             # Phase 5a — NameBio comps grounding (free tier, $0)
base_url = "https://api.namebio.com"
retailstats_path = "/retailstats-download"
tldstats_path = "/tldstats-download"
refresh_days = 7                    # aggregated stats move glacially; `comps-refresh` is safe to call
                                    # from the daily cron - each file NO-OPS unless older than this.
                                    # Costs nothing to reverse (set 1) if it ever proves wrong.
shrink_tolerance = 0.8              # refuse a refresh whose row count < 80% of the sidecar's recorded rows
min_rows_retailstats = 1000         # first-run floor (no sidecar baseline to compare against)
min_rows_tldstats = 100
stale_warn_factor = 3               # warn loudly once a cache is older than this x refresh_days.
                                    # An exact-header match means a NameBio column ADD bricks refresh
                                    # until a code change - correct, but it fails silently-forever from
                                    # cron (exit 0), so surface age where we actually look.

# NameBio free-tier rate limits — MEASURED 2026-07-16/17, not taken from the docs:
#   /retailstats, /tldstats  : 4 req / 60 s ROLLING, PER-ENDPOINT (4th ok, 5th -> 429; recovered at
#                              ~64 s). During a /retailstats 429, /tldstats still returned 200.
#   429 headers              : NONE. Cloudflare returns no Retry-After and no X-RateLimit-* --
#                              backoff must be our own; there is nothing to honour.
#   *-download endpoints     : INDEPENDENT, MUCH LONGER window. One download of each succeeded cold,
#                              then both 429'd for >30 min with no other traffic. Exact window
#                              UNCHARACTERIZED (needs hours of polling); known > 30 min.
#                              => a download 429 is NOT retryable in-run. The next daily cron run IS
#                              the retry. This deliberately INVERTS Phase 4's policy, where RDAP 429s
#                              recover in seconds. Both downloads DO succeed back-to-back when cold,
#                              so the refresh's 2 GETs need no sleep between them.
#   WARNING: `comps-refresh --force` can burn the download window and lock you out for HOURS.
#            `comps --domain` is local-only (reads the cache) and never touches the network.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (all config tests, including the new ones)

- [ ] **Step 6: Commit**

```bash
git add domainscout/config.py criteria.toml tests/test_config.py
git commit -m "feat(comps): add [comps] config + measured NameBio rate-limit notes"
```

---

### Task 2: Dataclasses + test fixtures

**Files:**
- Modify: `domainscout/models.py`
- Create: `tests/fixtures/namebio_retailstats_small.csv`
- Create: `tests/fixtures/namebio_tldstats_small.csv`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `KeywordComps`, `CompsContext`, `FileRefreshResult`, `RefreshResult` — consumed by Tasks 3–7.

- [ ] **Step 1: Create the retailstats fixture**

`tests/fixtures/namebio_retailstats_small.csv` — the header is the **real** 21-column header from the live endpoint (2026-07-16); rows are real-shaped. Keep on one physical header line:

```csv
keyword,exact_sale_count,exact_price_sum,exact_price_avg,exact_price_max,exact_price_stddev,start_sale_count,start_price_sum,start_price_avg,start_price_max,start_price_stddev,end_sale_count,end_price_sum,end_price_avg,end_price_max,end_price_stddev,middle_sale_count,middle_price_sum,middle_price_avg,middle_price_max,middle_price_stddev
cloud,120,480000,4000.00,100000,7492.41,2762,8653841,3133.18,500000,10466.05,245,1122056,4579.82,92000,6120.00,735,2266513,3083.69,45000,4302.34
vault,18,90000,5000.00,42000,8100.00,60,210000,3500.00,40000,5900.00,41,146699,3578.02,39600,6531.93,12,30000,2500.00,9000,1800.00
austin,30,300000,10000.00,120000,21000.00,88,264000,3000.00,60000,7000.00,15,45000,3000.00,12000,2600.00,20,40000,2000.00,8000,1500.00
plumber,9,54000,6000.00,25000,7100.00,11,33000,3000.00,9000,2400.00,64,320000,5000.00,80000,9100.00,5,10000,2000.00,4000,900.00
shop,200,1000000,5000.00,150000,12000.00,900,2700000,3000.00,90000,6000.00,410,1640000,4000.00,70000,5200.00,150,300000,2000.00,20000,2100.00
```

- [ ] **Step 2: Create the tldstats fixture**

`tests/fixtures/namebio_tldstats_small.csv`. The real file has 50 columns (10 periods × 5 stats); the fixture uses a **reduced but structurally identical** header — the code reads columns **by name**, never by index. Keep the header on one physical line:

```csv
extension,all_sale_count,all_price_sum,all_price_avg,all_price_max,all_price_stddev,all_retail_sale_count,all_retail_price_sum,all_retail_price_avg,all_retail_price_max,all_retail_price_stddev
.com,1448826,2532519355,1747.98,70000000,77814.09,189826,1630039181,8587.02,70000000,214317.64
.net,120000,90000000,750.00,500000,4200.00,20000,40000000,2000.00,500000,9100.00
```

- [ ] **Step 3: Write the failing test**

Add to `tests/test_models.py`:

```python
def test_keyword_comps_and_context_shape():
    from domainscout.models import CompsContext, KeywordComps
    kw = KeywordComps(keyword="cloud", placement="start", sale_count=2762,
                      price_avg=3133.18, price_max=500000.0, price_stddev=10466.05)
    ctx = CompsContext(domain="cloudvault.com", segmentation="cloud+vault",
                       keywords=(kw,), exact=None, tld_baseline={"extension": ".com"},
                       retrieved="2026-07-16")
    assert ctx.modeled is None            # reserved ValuationProvider slot
    assert ctx.attribution.startswith("Comparable sales data from NameBio")


def test_refresh_result_reports_mixed_outcome():
    from domainscout.models import FileRefreshResult, RefreshResult
    res = RefreshResult(files=(
        FileRefreshResult(name="retailstats", action="swapped", reason="", rows=97568, bytes=6678360),
        FileRefreshResult(name="tldstats", action="refused", reason="429", rows=None, bytes=None),
    ))
    assert res.any_swapped is True
    assert res.any_refused is True
```

- [ ] **Step 4: Run test to verify it fails**

Run: `python -m pytest tests/test_models.py -k "comps or refresh_result" -v`
Expected: FAIL — `ImportError: cannot import name 'CompsContext'`

- [ ] **Step 5: Write minimal implementation**

Append to `domainscout/models.py`:

```python
# NameBio's free-data permission is CONDITIONED on attribution, so this is a licence
# obligation, not a courtesy. CompsContext carries it so the Phase 7 digest cannot forget.
NAMEBIO_ATTRIBUTION = "Comparable sales data from NameBio (https://namebio.com)"


@dataclass
class KeywordComps:
    """One NameBio keyword's stats at ONE placement. price_* are None-free: a keyword
    absent from the index yields no KeywordComps at all (see comps.lookup)."""

    keyword: str
    placement: str          # 'exact' | 'start' | 'end' | 'middle'
    sale_count: int
    price_avg: float
    price_max: float
    price_stddev: float


@dataclass
class CompsContext:
    """The Tier-2 comps payload for one domain; serialized into candidates.value_range by 5c."""

    domain: str
    segmentation: str                       # from filters.dict_score, e.g. 'cloud+vault'
    keywords: tuple[KeywordComps, ...]
    exact: KeywordComps | None              # whole-label exact lookup (often absent)
    tld_baseline: dict
    retrieved: str | None                   # namebio_meta.json retailstats date; None if no sidecar
    modeled: dict | None = None             # RESERVED ValuationProvider slot (HumbleWorth).
                                            # MUST serialize as "modeled": null - keeps a later
                                            # HumbleWorth a data change, never a schema migration.
    attribution: str = NAMEBIO_ATTRIBUTION


@dataclass
class FileRefreshResult:
    """One cache file's independent outcome. action: 'swapped'|'skipped_fresh'|'refused'."""

    name: str               # 'retailstats' | 'tldstats'
    action: str
    reason: str = ""
    rows: int | None = None
    bytes: int | None = None


@dataclass
class RefreshResult:
    """Per-file results. Mixed outcomes are normal, not an error (design doc: per-file independence)."""

    files: tuple[FileRefreshResult, ...] = ()

    @property
    def any_swapped(self) -> bool:
        return any(f.action == "swapped" for f in self.files)

    @property
    def any_refused(self) -> bool:
        return any(f.action == "refused" for f in self.files)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_models.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add domainscout/models.py tests/test_models.py tests/fixtures/namebio_retailstats_small.csv tests/fixtures/namebio_tldstats_small.csv
git commit -m "feat(comps): add comps dataclasses + NameBio CSV fixtures"
```

---

### Task 3: Index load + placement parsing

**Files:**
- Create: `domainscout/comps.py`
- Test: `tests/test_comps.py`

**Interfaces:**
- Consumes: `KeywordComps` (Task 2).
- Produces: `RETAILSTATS_HEADER: tuple[str, ...]`, `TLDSTATS_KEY_COL: str`, `load_index(path) -> dict[str, str]`, `load_tld_stats(path) -> dict[str, dict]`, `parse_placement(line, placement) -> KeywordComps | None`, `CompsCacheMissing`.

**Why `keyword -> raw line`:** materialising 97,568 × 21 cells as Python floats costs hundreds of MB for data we touch ~60 cells of. Storing the raw line is ~15 MB and parses on demand.

- [ ] **Step 1: Write the failing test**

Create `tests/test_comps.py`:

```python
from pathlib import Path

import pytest

from domainscout import comps

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RETAIL = FIXTURES / "namebio_retailstats_small.csv"
TLD = FIXTURES / "namebio_tldstats_small.csv"


def test_load_index_keys_by_keyword_and_keeps_raw_line():
    idx = comps.load_index(RETAIL)
    assert set(idx) == {"cloud", "vault", "austin", "plumber", "shop"}
    assert idx["cloud"].startswith("cloud,")   # raw line retained, parsed on demand


def test_parse_placement_reads_the_right_columns():
    idx = comps.load_index(RETAIL)
    kc = comps.parse_placement(idx["cloud"], "start")
    assert kc.keyword == "cloud" and kc.placement == "start"
    assert kc.sale_count == 2762
    assert kc.price_avg == 3133.18
    assert kc.price_max == 500000.0
    assert kc.price_stddev == 10466.05


def test_parse_placement_exact_differs_from_start():
    idx = comps.load_index(RETAIL)
    assert comps.parse_placement(idx["cloud"], "exact").sale_count == 120
    assert comps.parse_placement(idx["cloud"], "start").sale_count == 2762


def test_parse_placement_zero_sales_returns_none():
    """0 sales carries no information; treat as absent so lookup reports 'no comps'."""
    line = "zylo," + ",".join(["0"] * 20)
    assert comps.parse_placement(line, "exact") is None


def test_load_tld_stats_by_extension():
    tld = comps.load_tld_stats(TLD)
    assert tld[".com"]["all_retail"]["sale_count"] == 189826
    assert tld[".com"]["all_retail"]["price_avg"] == 8587.02


def test_load_index_missing_file_raises_cache_missing():
    with pytest.raises(comps.CompsCacheMissing):
        comps.load_index(FIXTURES / "does-not-exist.csv")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_comps.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'domainscout.comps'`

- [ ] **Step 3: Write minimal implementation**

Create `domainscout/comps.py`:

```python
"""Phase 5a: NameBio comps grounding.

A cache + lookup library (NOT a pipeline stage): comps are global context keyed by
freshness, not per-candidate state, so nothing here writes `candidates`. 5c calls
lookup() and writes value_range at scoring time.

Network lives ONLY in refresh_cache(); the httpx.Client is injected so tests never hit it.
Read docs/PHASE-5A-DESIGN.md "NameBio gotchas" before touching the refresh path.
"""

from __future__ import annotations

import csv
from pathlib import Path

from domainscout.models import KeywordComps

# The real 21-column header from GET /retailstats-download (verified live 2026-07-16).
# Matched EXACTLY before a swap: a NameBio column change must brick the refresh rather
# than silently shift our column reads. Task 6 gates on this; Task 7 surfaces the staleness.
RETAILSTATS_HEADER: tuple[str, ...] = (
    "keyword",
    "exact_sale_count", "exact_price_sum", "exact_price_avg", "exact_price_max", "exact_price_stddev",
    "start_sale_count", "start_price_sum", "start_price_avg", "start_price_max", "start_price_stddev",
    "end_sale_count", "end_price_sum", "end_price_avg", "end_price_max", "end_price_stddev",
    "middle_sale_count", "middle_price_sum", "middle_price_avg", "middle_price_max", "middle_price_stddev",
)
TLDSTATS_KEY_COL = "extension"
PLACEMENTS = ("exact", "start", "end", "middle")


class CompsCacheMissing(FileNotFoundError):
    """No comps cache (and no .prev) — run `domainscout comps-refresh`."""


def load_index(path: str | Path) -> dict[str, str]:
    """keyword -> raw CSV line. Raw lines (not parsed rows) keep this ~15 MB instead of
    hundreds of MB: we touch ~60 of the ~2M cells per run."""
    p = Path(path)
    if not p.is_file():
        raise CompsCacheMissing(f"no comps cache at {p}; run `domainscout comps-refresh`")
    index: dict[str, str] = {}
    with p.open("r", encoding="utf-8", newline="") as fh:
        fh.readline()  # header; validated at swap time, not on every load
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            kw = line.split(",", 1)[0].strip().lower()
            if kw:
                index[kw] = line
    return index


def parse_placement(line: str, placement: str) -> KeywordComps | None:
    """Pull one placement's 5 stats out of a raw retailstats line.
    Returns None when the keyword has 0 sales at that placement — absence of data, which
    lookup() reports as 'no comparable sales' rather than as a zero-valued comp."""
    if placement not in PLACEMENTS:
        raise ValueError(f"unknown placement {placement!r}; expected one of {PLACEMENTS}")
    cells = next(csv.reader([line]))
    row = dict(zip(RETAILSTATS_HEADER, cells))
    try:
        sale_count = int(float(row[f"{placement}_sale_count"] or 0))
    except (KeyError, ValueError):
        return None
    if sale_count <= 0:
        return None

    def num(col: str) -> float:
        try:
            return float(row.get(col) or 0.0)
        except ValueError:
            return 0.0

    return KeywordComps(
        keyword=row["keyword"].strip().lower(),
        placement=placement,
        sale_count=sale_count,
        price_avg=num(f"{placement}_price_avg"),
        price_max=num(f"{placement}_price_max"),
        price_stddev=num(f"{placement}_price_stddev"),
    )


def load_tld_stats(path: str | Path) -> dict[str, dict]:
    """extension -> {period: {stat: value}}. Columns are read BY NAME (`<period>_<stat>`),
    never by index, so NameBio adding a period does not shift our reads."""
    p = Path(path)
    if not p.is_file():
        raise CompsCacheMissing(f"no comps cache at {p}; run `domainscout comps-refresh`")
    out: dict[str, dict] = {}
    with p.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            ext = (row.get(TLDSTATS_KEY_COL) or "").strip().lower()
            if not ext:
                continue
            periods: dict[str, dict] = {}
            for col, raw in row.items():
                if not col or col == TLDSTATS_KEY_COL or raw is None:
                    continue
                for stat in ("_sale_count", "_price_sum", "_price_avg", "_price_max", "_price_stddev"):
                    if col.endswith(stat):
                        period = col[: -len(stat)]
                        try:
                            val = float(raw or 0.0)
                        except ValueError:
                            val = 0.0
                        key = stat.lstrip("_")
                        periods.setdefault(period, {})[key] = (
                            int(val) if key == "sale_count" else val
                        )
                        break
            out[ext] = periods
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_comps.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add domainscout/comps.py tests/test_comps.py
git commit -m "feat(comps): NameBio CSV index load + placement parsing"
```

---

### Task 4: `lookup()` — keyword → placement mapping (the crux)

**Files:**
- Modify: `domainscout/comps.py`
- Test: `tests/test_comps.py`

**Interfaces:**
- Consumes: `load_index`/`load_tld_stats`/`parse_placement` (Task 3); `filters.dict_score(label, criteria) -> (float, str)` (Phase 3); `CompsContext` (Task 2).
- Produces: `lookup(domain, index, tld_stats, criteria, *, retrieved=None) -> CompsContext`, `context_to_json(ctx) -> str`.

**The mapping.** NameBio keys stats by keyword **and placement**, which maps exactly onto word position. Segmentation is **reused from `filters.dict_score()`** — the project's single source of truth for splitting a label. A second splitter would drift.

| Domain | `dict_score` seg | Lookups |
|---|---|---|
| `vault.com` | `vault` | `vault` → **exact** |
| `cloudvault.com` | `cloud+vault` | `cloud` → **start**, `vault` → **end** |
| `austinplumber.com` | `austin+plumber` | `austin` → **start**, `plumber` → **end** |
| `zylo.com` | `zylo` | `zylo` → **exact** (absent ⇒ no comps — *absence of evidence*, not "worthless") |

- [ ] **Step 1: Write the failing test**

Add to `tests/test_comps.py`:

```python
import json

from domainscout.config import load_criteria

REPO_ROOT = Path(__file__).resolve().parents[1]
CRIT = load_criteria(REPO_ROOT / "criteria.toml")


def _ctx(domain):
    return comps.lookup(domain, comps.load_index(RETAIL), comps.load_tld_stats(TLD),
                        CRIT, retrieved="2026-07-16")


def test_lookup_two_words_uses_start_then_end():
    ctx = _ctx("cloudvault.com")
    assert ctx.segmentation == "cloud+vault"
    got = [(k.keyword, k.placement, k.sale_count) for k in ctx.keywords]
    assert got == [("cloud", "start", 2762), ("vault", "end", 41)]


def test_lookup_single_word_uses_exact():
    ctx = _ctx("vault.com")
    assert ctx.segmentation == "vault"
    assert [(k.keyword, k.placement) for k in ctx.keywords] == [("vault", "exact")]


def test_lookup_geo_service_secondary_track():
    ctx = _ctx("austinplumber.com")
    got = [(k.keyword, k.placement, k.sale_count) for k in ctx.keywords]
    assert got == [("austin", "start", 88), ("plumber", "end", 64)]


def test_lookup_unknown_keyword_is_absence_not_error():
    """Invented brandables are systematically underrepresented in keyword-keyed retail
    stats. 'No comps' must mean 'no evidence for this pattern', never 'worthless' --
    a naive 5c prompt would penalize exactly the secondary-track names we exist to catch."""
    ctx = _ctx("zylo.com")
    assert ctx.keywords == ()
    assert ctx.exact is None
    assert ctx.tld_baseline["extension"] == ".com"   # baseline still present to reason from


def test_lookup_attaches_tld_baseline_and_retrieved():
    ctx = _ctx("cloudvault.com")
    assert ctx.tld_baseline["all_retail"]["price_avg"] == 8587.02
    assert ctx.retrieved == "2026-07-16"


def test_context_to_json_always_carries_modeled_null():
    """The reserved ValuationProvider slot. If this disappears, adding HumbleWorth later
    becomes a schema migration instead of a data change. Do not 'clean up' the null."""
    payload = json.loads(comps.context_to_json(_ctx("cloudvault.com")))
    assert "modeled" in payload and payload["modeled"] is None
    assert payload["source"] == "namebio-free"
    assert payload["attribution"].startswith("Comparable sales data from NameBio")
    assert payload["keywords"][0]["placement"] == "start"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_comps.py -k lookup -v`
Expected: FAIL — `AttributeError: module 'domainscout.comps' has no attribute 'lookup'`

- [ ] **Step 3: Write minimal implementation**

Add to `domainscout/comps.py` (add `import json`, `from dataclasses import asdict`, and `from domainscout import filters` / `from domainscout.models import CompsContext, KeywordComps` to the imports):

```python
def lookup(domain, index, tld_stats, criteria, *, retrieved: str | None = None) -> CompsContext:
    """Comps for one .com domain. Placement is chosen by word POSITION, which is exactly
    what NameBio's exact/start/end placements mean:
      1 part  -> `exact` for the label
      2 parts -> `start` for the left word, `end` for the right
    Segmentation is REUSED from filters.dict_score (Phase 3) - the single source of truth
    for splitting a label; a second splitter would drift from the dictionary gate.
    A missing keyword yields no entry: absence of evidence, NOT a zero-valued comp."""
    label = domain[:-4] if domain.endswith(".com") else domain
    _score, seg = filters.dict_score(label, criteria)

    found: list[KeywordComps] = []
    if "+" in seg:
        left, right = seg.split("+", 1)
        for word, placement in ((left, "start"), (right, "end")):
            line = index.get(word)
            if line:
                kc = parse_placement(line, placement)
                if kc:
                    found.append(kc)
    else:
        line = index.get(seg)
        if line:
            kc = parse_placement(line, "exact")
            if kc:
                found.append(kc)

    # Always also try the WHOLE label as an exact keyword (catches e.g. a known compound).
    exact = None
    whole = index.get(label)
    if whole:
        exact = parse_placement(whole, "exact")
    if exact is not None and any(
        k.keyword == exact.keyword and k.placement == "exact" for k in found
    ):
        exact = None  # already reported in `keywords`; don't duplicate

    baseline = dict(tld_stats.get(".com") or {})
    baseline["extension"] = ".com"
    return CompsContext(
        domain=domain, segmentation=seg, keywords=tuple(found), exact=exact,
        tld_baseline=baseline, retrieved=retrieved,
    )


def context_to_json(ctx: CompsContext) -> str:
    """Serialize to the candidates.value_range payload (5c writes it).
    `modeled` is ALWAYS emitted as null - the reserved ValuationProvider slot."""
    return json.dumps({
        "source": "namebio-free",
        "retrieved": ctx.retrieved,
        "segmentation": ctx.segmentation,
        "keywords": [asdict(k) for k in ctx.keywords],
        "exact": asdict(ctx.exact) if ctx.exact else None,
        "tld_baseline": ctx.tld_baseline,
        "modeled": ctx.modeled,
        "attribution": ctx.attribution,
    })
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_comps.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add domainscout/comps.py tests/test_comps.py
git commit -m "feat(comps): keyword->placement lookup reusing Phase-3 segmentation"
```

---

### Task 5: Metadata sidecar + validation gate + `.prev` resolution

**Files:**
- Modify: `domainscout/comps.py`
- Test: `tests/test_comps.py`

**Interfaces:**
- Produces: `load_meta(data_dir) -> dict`, `write_meta(data_dir, meta) -> None`, `validate_download(tmp_path, *, expected_header, baseline_rows, min_rows, shrink_tolerance) -> tuple[bool, str]`, `resolve_cache_path(current, prev) -> tuple[Path, bool]`, `cache_age_days(meta, name, now) -> float | None`, `META_FILENAME`.

**Why a sidecar, not mtimes** (owner review round 2): per-file swaps mean the caches can legitimately differ in age, so one global date would be a lie; mtimes survive copies/restores badly; and it persists `rows`, which otherwise means re-reading 6.7 MB **just to count lines** on every refresh.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_comps.py`:

```python
from datetime import datetime, timedelta


def _write(p: Path, header, rows):
    p.write_text(",".join(header) + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def test_validate_download_accepts_good_file(tmp_path):
    ok, reason = comps.validate_download(
        RETAIL, expected_header=comps.RETAILSTATS_HEADER, baseline_rows=5,
        min_rows=1, shrink_tolerance=0.8)
    assert ok is True and reason == ""


def test_validate_download_rejects_error_page(tmp_path):
    """HTTP 200 + an HTML error page is the exact failure atomic rename does NOT cover."""
    p = tmp_path / "bad.csv"
    p.write_text("<html><body>rate limited</body></html>", encoding="utf-8")
    ok, reason = comps.validate_download(
        p, expected_header=comps.RETAILSTATS_HEADER, baseline_rows=5,
        min_rows=1, shrink_tolerance=0.8)
    assert ok is False and "header" in reason.lower()


def test_validate_download_rejects_shrink_below_tolerance(tmp_path):
    p = tmp_path / "short.csv"
    _write(p, comps.RETAILSTATS_HEADER, ["cloud," + ",".join(["1"] * 20)])
    ok, reason = comps.validate_download(
        p, expected_header=comps.RETAILSTATS_HEADER, baseline_rows=100,
        min_rows=1, shrink_tolerance=0.8)
    assert ok is False and "shrink" in reason.lower()


def test_validate_download_first_run_uses_min_rows_floor(tmp_path):
    """No sidecar baseline: an error page must not be able to SEED the cache either."""
    p = tmp_path / "tiny.csv"
    _write(p, comps.RETAILSTATS_HEADER, ["cloud," + ",".join(["1"] * 20)])
    ok, reason = comps.validate_download(
        p, expected_header=comps.RETAILSTATS_HEADER, baseline_rows=None,
        min_rows=1000, shrink_tolerance=0.8)
    assert ok is False and "min_rows" in reason.lower()


def test_meta_roundtrip_atomic(tmp_path):
    meta = {"retailstats": {"retrieved": "2026-07-16T10:00:00", "rows": 97568,
                            "sha256": "abc", "bytes": 10}}
    comps.write_meta(tmp_path, meta)
    assert (tmp_path / comps.META_FILENAME).is_file()
    assert comps.load_meta(tmp_path)["retailstats"]["rows"] == 97568


def test_load_meta_missing_or_corrupt_returns_empty(tmp_path):
    assert comps.load_meta(tmp_path) == {}
    (tmp_path / comps.META_FILENAME).write_text("{not json", encoding="utf-8")
    assert comps.load_meta(tmp_path) == {}   # degrade: refresh falls back to first-run rules


def test_resolve_cache_path_falls_back_to_prev(tmp_path, caplog):
    """Crash between `current->.prev` and `tmp->current` leaves NO current file."""
    cur = tmp_path / "x.csv"
    prev = tmp_path / "x.csv.prev"
    prev.write_text("data", encoding="utf-8")
    path, used_prev = comps.resolve_cache_path(cur, prev)
    assert path == prev and used_prev is True


def test_resolve_cache_path_prefers_current(tmp_path):
    cur = tmp_path / "x.csv"
    prev = tmp_path / "x.csv.prev"
    cur.write_text("new", encoding="utf-8")
    prev.write_text("old", encoding="utf-8")
    assert comps.resolve_cache_path(cur, prev) == (cur, False)


def test_resolve_cache_path_both_missing_raises(tmp_path):
    with pytest.raises(comps.CompsCacheMissing):
        comps.resolve_cache_path(tmp_path / "x.csv", tmp_path / "x.csv.prev")


def test_cache_age_days(tmp_path):
    now = datetime(2026, 7, 16, 12, 0, 0)
    meta = {"retailstats": {"retrieved": (now - timedelta(days=3)).isoformat()}}
    assert round(comps.cache_age_days(meta, "retailstats", now), 1) == 3.0
    assert comps.cache_age_days(meta, "tldstats", now) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_comps.py -k "validate or meta or resolve or age" -v`
Expected: FAIL — `AttributeError: module 'domainscout.comps' has no attribute 'validate_download'`

- [ ] **Step 3: Write minimal implementation**

Add to `domainscout/comps.py` (add `import hashlib`, `import logging`, `from datetime import datetime` to the imports):

```python
log = logging.getLogger(__name__)

META_FILENAME = "namebio_meta.json"


def load_meta(data_dir: str | Path) -> dict:
    """Per-file {retrieved, rows, sha256, bytes}. Missing/corrupt -> {} so refresh falls back
    to first-run rules and load still works (the sidecar is an optimisation + audit record,
    never a hard dependency for READING the cache)."""
    p = Path(data_dir) / META_FILENAME
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("comps: %s is unreadable/corrupt; treating caches as stale", p)
        return {}
    return data if isinstance(data, dict) else {}


def write_meta(data_dir: str | Path, meta: dict) -> None:
    """Atomic tmp+rename so a crash can't leave a half-written sidecar."""
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (META_FILENAME + ".tmp")
    tmp.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(d / META_FILENAME)


def _count_rows(path: Path) -> int:
    with path.open("rb") as fh:
        return max(0, sum(1 for _ in fh) - 1)  # minus header


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_download(tmp_path, *, expected_header, baseline_rows, min_rows,
                      shrink_tolerance) -> tuple[bool, str]:
    """Gate a downloaded file BEFORE it replaces a good cache.

    Atomic rename guarantees a COMPLETE file, not a GOOD one: HTTP 200 with an
    error-page-as-CSV, an empty body, or a truncated export would all atomically install
    garbage. Returns (ok, reason); reason is '' on success."""
    p = Path(tmp_path)
    if not p.is_file():
        return False, "download missing"
    try:
        with p.open("r", encoding="utf-8", newline="") as fh:
            first = fh.readline().rstrip("\n").rstrip("\r")
    except OSError as exc:
        return False, f"unreadable: {exc}"
    if not first:
        return False, "empty file"
    try:
        header = tuple(next(csv.reader([first])))
    except csv.Error as exc:
        return False, f"does not parse as CSV: {exc}"
    if header != tuple(expected_header):
        return False, (
            f"header mismatch (got {header[0]!r}, {len(header)} cols; "
            f"expected {len(expected_header)}) - NameBio may have changed the schema"
        )
    rows = _count_rows(p)
    if baseline_rows:
        floor = int(baseline_rows * shrink_tolerance)
        if rows < floor:
            return False, f"shrink: {rows} rows < {floor} ({shrink_tolerance:.0%} of {baseline_rows})"
    elif rows < min_rows:
        return False, f"below min_rows floor: {rows} < {min_rows}"
    return True, ""


def resolve_cache_path(current: Path, prev: Path) -> tuple[Path, bool]:
    """(path_to_load, used_prev). `current -> .prev` and `tmp -> current` are each atomic but
    NOT jointly atomic: a crash between them leaves no current file. Fall back loudly."""
    current, prev = Path(current), Path(prev)
    if current.is_file():
        return current, False
    if prev.is_file():
        log.warning(
            "comps cache %s missing but %s exists - loading .prev (crash between swap "
            "renames?); run `domainscout comps-refresh --force` to repair",
            current.name, prev.name,
        )
        return prev, True
    raise CompsCacheMissing(
        f"no comps cache at {current} or {prev}; run `domainscout comps-refresh`")


def cache_age_days(meta: dict, name: str, now: datetime) -> float | None:
    """Age in days from the sidecar's `retrieved`; None if unknown."""
    stamp = (meta.get(name) or {}).get("retrieved")
    if not stamp:
        return None
    try:
        return (now - datetime.fromisoformat(stamp)).total_seconds() / 86400.0
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_comps.py -v`
Expected: PASS (23 tests)

- [ ] **Step 5: Commit**

```bash
git add domainscout/comps.py tests/test_comps.py
git commit -m "feat(comps): metadata sidecar, validation gate, .prev crash fallback"
```

---

### Task 6: Download + per-file swap + `refresh_cache()`

**Files:**
- Modify: `domainscout/comps.py`
- Test: `tests/test_comps.py`

**Interfaces:**
- Consumes: everything from Tasks 3–5; `ingest.make_client()`.
- Produces: `FileSpec`, `FILE_SPECS`, `_get_with_retry(client, url, dest, *, retries, sleep)`, `refresh_one(...) -> FileRefreshResult`, `refresh_cache(client, criteria, data_dir, *, force=False, now=None) -> RefreshResult`, `summary_line(result) -> str`.

**Read before implementing** — design doc gotchas #3/#4/#5:
- **429 → refuse immediately, NEVER retry in-run** (recovery takes hours; the daily cron is the retry).
- **`ratelimit.py` is deliberately not reused** (async; whodap-specific `RETRYABLE`; a comps 429 is a status code, not an exception). Local sync `_get_with_retry` retries **`httpx.TransportError` only**.
- **Per-file independence** — one file's failure must never discard the other's validated download. **retailstats first.**
- No sleep needed between the two GETs (proven safe cold).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_comps.py`:

```python
import httpx

from domainscout.models import RefreshResult


def _fake_client(routes):
    """routes: url-substring -> (status, body_bytes) or an Exception to raise."""
    def handler(request: httpx.Request) -> httpx.Response:
        for frag, outcome in routes.items():
            if frag in str(request.url):
                if isinstance(outcome, Exception):
                    raise outcome
                status, body = outcome
                return httpx.Response(status, content=body)
        return httpx.Response(404, content=b"")
    return httpx.Client(transport=httpx.MockTransport(handler))


def _good(path: Path) -> bytes:
    return path.read_bytes()


NOW = datetime(2026, 7, 16, 12, 0, 0)


def _crit_small():
    """Fixtures are tiny, so drop the production min_rows floors to 1."""
    from dataclasses import replace
    return replace(CRIT, comps_min_rows_retailstats=1, comps_min_rows_tldstats=1)


def test_refresh_swaps_both_and_writes_sidecar(tmp_path):
    client = _fake_client({"retailstats-download": (200, _good(RETAIL)),
                           "tldstats-download": (200, _good(TLD))})
    res = comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW)
    assert [f.action for f in res.files] == ["swapped", "swapped"]
    assert (tmp_path / "namebio_retailstats.csv").is_file()
    meta = comps.load_meta(tmp_path)
    assert meta["retailstats"]["rows"] == 5
    assert meta["retailstats"]["retrieved"] == NOW.isoformat()
    assert len(meta["retailstats"]["sha256"]) == 64


def test_refresh_retailstats_is_fetched_first(tmp_path):
    """If only one file survives the rate-limit window it must be the one Tier-2 needs."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        body = _good(RETAIL) if "retailstats" in str(request.url) else _good(TLD)
        return httpx.Response(200, content=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW)
    assert "retailstats" in seen[0]


def test_refresh_per_file_independence_429_on_second(tmp_path):
    """THE bug per-file swap exists to prevent: a tldstats 429 must NOT discard a
    validated 6.7MB retailstats bought with the long, uncharacterized 429 window."""
    client = _fake_client({"retailstats-download": (200, _good(RETAIL)),
                           "tldstats-download": (429, b"rate limited")})
    res = comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW)
    by = {f.name: f for f in res.files}
    assert by["retailstats"].action == "swapped"
    assert by["tldstats"].action == "refused" and "429" in by["tldstats"].reason
    assert (tmp_path / "namebio_retailstats.csv").is_file()   # KEPT
    assert not (tmp_path / "namebio_tldstats.csv").exists()
    assert res.any_swapped and res.any_refused


def test_refresh_429_never_retries_in_run(tmp_path):
    """Recovery takes HOURS -> in-run retry is useless. Exactly one attempt."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, content=b"")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    res = comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW)
    assert len(calls) == 2          # one attempt per file, no retries
    assert all(f.action == "refused" for f in res.files)


def test_refresh_retries_transport_error(tmp_path):
    attempts = []

    def handler(request):
        attempts.append(str(request.url))
        if "retailstats" in str(request.url) and len(attempts) == 1:
            raise httpx.ConnectError("boom")
        body = _good(RETAIL) if "retailstats" in str(request.url) else _good(TLD)
        return httpx.Response(200, content=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    res = comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW, sleep=lambda s: None)
    assert [f.action for f in res.files] == ["swapped", "swapped"]


def test_refresh_bad_download_leaves_cache_byte_identical(tmp_path):
    good = _crit_small()
    client = _fake_client({"retailstats-download": (200, _good(RETAIL)),
                           "tldstats-download": (200, _good(TLD))})
    comps.refresh_cache(client, good, tmp_path, now=NOW)
    before = (tmp_path / "namebio_retailstats.csv").read_bytes()

    bad = _fake_client({"retailstats-download": (200, b"<html>error</html>"),
                        "tldstats-download": (200, _good(TLD))})
    later = NOW + timedelta(days=30)
    res = bad and comps.refresh_cache(bad, good, tmp_path, now=later, force=True)
    by = {f.name: f for f in res.files}
    assert by["retailstats"].action == "refused" and "header" in by["retailstats"].reason
    assert (tmp_path / "namebio_retailstats.csv").read_bytes() == before   # untouched


def test_refresh_keeps_one_prev_on_swap(tmp_path):
    client = _fake_client({"retailstats-download": (200, _good(RETAIL)),
                           "tldstats-download": (200, _good(TLD))})
    comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW)
    comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW + timedelta(days=30))
    assert (tmp_path / "namebio_retailstats.csv.prev").is_file()


def test_refresh_noops_when_fresh(tmp_path):
    client = _fake_client({"retailstats-download": (200, _good(RETAIL)),
                           "tldstats-download": (200, _good(TLD))})
    comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW)
    res = comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW + timedelta(days=2))
    assert [f.action for f in res.files] == ["skipped_fresh", "skipped_fresh"]


def test_refresh_force_overrides_freshness(tmp_path):
    client = _fake_client({"retailstats-download": (200, _good(RETAIL)),
                           "tldstats-download": (200, _good(TLD))})
    comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW)
    res = comps.refresh_cache(client, _crit_small(), tmp_path,
                              now=NOW + timedelta(days=2), force=True)
    assert [f.action for f in res.files] == ["swapped", "swapped"]


def test_force_never_bypasses_header_check(tmp_path):
    """No flag may install an error page."""
    client = _fake_client({"retailstats-download": (200, b"<html>nope</html>"),
                           "tldstats-download": (200, _good(TLD))})
    res = comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW, force=True)
    by = {f.name: f for f in res.files}
    assert by["retailstats"].action == "refused" and "header" in by["retailstats"].reason
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_comps.py -k refresh -v`
Expected: FAIL — `AttributeError: module 'domainscout.comps' has no attribute 'refresh_cache'`

- [ ] **Step 3: Write minimal implementation**

Add to `domainscout/comps.py` (add `import time`, `import httpx`, `from dataclasses import dataclass`, `from domainscout.models import FileRefreshResult, RefreshResult`):

```python
@dataclass(frozen=True)
class FileSpec:
    name: str            # 'retailstats' | 'tldstats'
    path_attr: str       # Criteria attribute holding the URL path
    filename: str
    header: tuple[str, ...]
    min_rows_attr: str


# ORDER MATTERS: retailstats FIRST. If only one file survives the (long, uncharacterized)
# download rate-limit window, it must be the one Tier-2 actually reasons from.
FILE_SPECS: tuple[FileSpec, ...] = (
    FileSpec("retailstats", "comps_retailstats_path", "namebio_retailstats.csv",
             RETAILSTATS_HEADER, "comps_min_rows_retailstats"),
    FileSpec("tldstats", "comps_tldstats_path", "namebio_tldstats.csv",
             None, "comps_min_rows_tldstats"),
)


class RateLimited(Exception):
    """NameBio returned 429. NOT retryable in-run: recovery takes HOURS (design gotcha #3)."""


def _get_with_retry(client, url: str, dest: Path, *, retries: int = 2, sleep=time.sleep) -> int:
    """Stream url -> dest. Returns bytes written.

    Deliberately NOT ratelimit.with_backoff: that is async, its RETRYABLE is whodap-specific,
    and a comps 429 arrives as a STATUS CODE not an exception. Retries httpx.TransportError
    ONLY; a 429 raises RateLimited immediately and is never retried (gotcha #4)."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            n = 0
            with client.stream("GET", url) as resp:
                if resp.status_code == 429:
                    raise RateLimited("429 rate limited")
                resp.raise_for_status()
                with dest.open("wb") as fh:
                    for chunk in resp.iter_bytes():
                        n += len(chunk)
                        fh.write(chunk)
            return n
        except httpx.TransportError as exc:      # transient: worth one more try
            last = exc
            dest.unlink(missing_ok=True)
            if attempt >= retries:
                raise
            sleep(min(30.0, 2.0 * (2 ** attempt)))
    raise last if last else RuntimeError("unreachable")


def refresh_one(client, spec: FileSpec, criteria, data_dir: Path, meta: dict, *,
                force: bool, now: datetime, sleep=time.sleep) -> FileRefreshResult:
    """One file's INDEPENDENT freshness check -> download -> validate -> swap -> sidecar.
    A failure here must never affect the sibling file."""
    current = data_dir / spec.filename
    prev = data_dir / (spec.filename + ".prev")
    entry = meta.get(spec.name) or {}

    age = cache_age_days(meta, spec.name, now)
    if not force and current.is_file() and age is not None and age < criteria.comps_refresh_days:
        return FileRefreshResult(spec.name, "skipped_fresh",
                                 f"fresh, {age:.0f}d < {criteria.comps_refresh_days}d",
                                 rows=entry.get("rows"))

    url = criteria.comps_base_url.rstrip("/") + getattr(criteria, spec.path_attr)
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = data_dir / (spec.filename + ".tmp")
    try:
        nbytes = _get_with_retry(client, url, tmp, sleep=sleep)
    except RateLimited:
        tmp.unlink(missing_ok=True)
        return FileRefreshResult(spec.name, "refused",
                                 "429; cache intact, next daily run retries")
    except (httpx.HTTPError, OSError) as exc:
        tmp.unlink(missing_ok=True)
        return FileRefreshResult(spec.name, "refused", f"{type(exc).__name__}: {exc}")

    header = spec.header
    if header is None:   # tldstats: NameBio may add periods; key column is what matters
        with tmp.open("r", encoding="utf-8", newline="") as fh:
            first = fh.readline().rstrip("\n").rstrip("\r")
        try:
            cols = tuple(next(csv.reader([first])))
        except csv.Error:
            cols = ()
        if not cols or cols[0] != TLDSTATS_KEY_COL:
            tmp.unlink(missing_ok=True)
            return FileRefreshResult(
                spec.name, "refused",
                f"header mismatch (expected first column {TLDSTATS_KEY_COL!r})")
        header = cols

    # --force bypasses the shrink check (a legitimate >20% shrink needs it) but NEVER
    # the parse/header/min_rows checks: no flag may install an error page.
    baseline = None if force else entry.get("rows")
    ok, reason = validate_download(
        tmp, expected_header=header, baseline_rows=baseline,
        min_rows=getattr(criteria, spec.min_rows_attr),
        shrink_tolerance=criteria.comps_shrink_tolerance,
    )
    if not ok:
        tmp.unlink(missing_ok=True)
        log.warning("comps: %s refused - %s; cache left intact", spec.name, reason)
        return FileRefreshResult(spec.name, "refused", reason)

    rows, sha = _count_rows(tmp), _sha256(tmp)
    if current.is_file():
        current.replace(prev)      # atomic; keeps exactly ONE predecessor
    tmp.replace(current)           # atomic
    meta[spec.name] = {"retrieved": now.isoformat(), "rows": rows,
                       "sha256": sha, "bytes": nbytes}
    return FileRefreshResult(spec.name, "swapped", "", rows=rows, bytes=nbytes)


def refresh_cache(client, criteria, data_dir, *, force: bool = False,
                  now: datetime | None = None, sleep=time.sleep) -> RefreshResult:
    """Refresh both NameBio caches, PER-FILE and INDEPENDENTLY (design doc: per-file
    independence). One file's 429 must never discard the other's validated download."""
    now = now or datetime.now()
    d = Path(data_dir)
    meta = load_meta(d)
    results = []
    for spec in FILE_SPECS:
        results.append(refresh_one(client, spec, criteria, d, meta,
                                   force=force, now=now, sleep=sleep))
    if any(r.action == "swapped" for r in results):
        write_meta(d, meta)
    return RefreshResult(files=tuple(results))


def summary_line(result: RefreshResult) -> str:
    parts = []
    for f in result.files:
        if f.action == "swapped":
            size = f" , {f.bytes/1e6:.1f} MB" if f.bytes else ""
            parts.append(f"{f.name} swapped ({f.rows:,} rows{size})")
        elif f.action == "skipped_fresh":
            parts.append(f"{f.name} skipped ({f.reason})")
        else:
            parts.append(f"{f.name} REFUSED ({f.reason})")
    return "comps-refresh: " + " | ".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_comps.py -v`
Expected: PASS (33 tests)

- [ ] **Step 5: Commit**

```bash
git add domainscout/comps.py tests/test_comps.py
git commit -m "feat(comps): per-file independent download/validate/swap + refresh_cache"
```

---

### Task 7: CLI — `comps-refresh` + `comps --domain`

**Files:**
- Modify: `domainscout/commands.py`
- Modify: `domainscout/__main__.py`
- Modify: `.gitignore`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `comps.refresh_cache`, `comps.summary_line`, `comps.load_index`, `comps.load_tld_stats`, `comps.lookup`, `comps.context_to_json`, `comps.resolve_cache_path`, `comps.load_meta`, `comps.cache_age_days`; `ingest.make_client()`.
- Produces: `commands.cmd_comps_refresh(args) -> int`, `commands.cmd_comps(args) -> int`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli.py`:

```python
def test_comps_subcommands_are_no_longer_stubs():
    from domainscout.__main__ import build_parser
    parser = build_parser()
    args = parser.parse_args(["comps-refresh", "--force"])
    assert args.func.__name__ == "cmd_comps_refresh"
    assert args.force is True
    args2 = parser.parse_args(["comps", "--domain", "cloudvault.com"])
    assert args2.func.__name__ == "cmd_comps"
    assert args2.domain == "cloudvault.com"


def test_cmd_comps_makes_no_network_calls(tmp_path, capsys, monkeypatch):
    """`comps --domain` is LOCAL ONLY - it must never be able to poison a refresh."""
    import shutil
    from pathlib import Path as _P

    from domainscout import commands, comps

    fx = _P(__file__).resolve().parent / "fixtures"
    shutil.copy(fx / "namebio_retailstats_small.csv", tmp_path / "namebio_retailstats.csv")
    shutil.copy(fx / "namebio_tldstats_small.csv", tmp_path / "namebio_tldstats.csv")
    comps.write_meta(tmp_path, {"retailstats": {"retrieved": "2026-07-16T10:00:00", "rows": 5}})

    def boom(*a, **k):
        raise AssertionError("comps --domain must not touch the network")

    monkeypatch.setattr("domainscout.ingest.make_client", boom)

    class A:
        criteria = "criteria.toml"
        domain = "cloudvault.com"
        data_dir = str(tmp_path)

    assert commands.cmd_comps(A()) == 0
    out = capsys.readouterr().out
    assert "cloud" in out and "start" in out
    assert "cache:" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli.py -k comps -v`
Expected: FAIL — `argparse` error / `AttributeError: 'cmd_stub'`

- [ ] **Step 3: Write the command handlers**

In `domainscout/commands.py`: add `comps` to the imports (`from domainscout import comps, db, filters, ingest, pronounce, rdap`), add `from datetime import datetime`, and append:

```python
COMPS_DATA_DIR = "data"


def cmd_comps_refresh(args: argparse.Namespace) -> int:
    criteria = load_criteria(args.criteria)
    data_dir = Path(getattr(args, "data_dir", None) or COMPS_DATA_DIR)
    if args.dry_run:
        print("comps-refresh: [dry-run] would refresh "
              + ", ".join(s.name for s in comps.FILE_SPECS)
              + f" into {data_dir} (nothing written)")
        return 0
    client = ingest.make_client()
    try:
        result = comps.refresh_cache(client, criteria, data_dir, force=args.force)
    finally:
        client.close()
    print(comps.summary_line(result))
    _warn_if_stale(criteria, data_dir)
    return 0


def _warn_if_stale(criteria, data_dir) -> None:
    """An exact-header match means a NameBio column ADD bricks refresh until a code change.
    That is the right conservative failure, but from cron it fails silently-forever (exit 0),
    so surface age where we actually look."""
    meta = comps.load_meta(data_dir)
    limit = criteria.comps_refresh_days * criteria.comps_stale_warn_factor
    now = datetime.now()
    for spec in comps.FILE_SPECS:
        age = comps.cache_age_days(meta, spec.name, now)
        if age is not None and age > limit:
            print(f"  ⚠️  STALE - {spec.name} is {age:.0f}d old (> {criteria.comps_stale_warn_factor}x "
                  f"refresh_days={criteria.comps_refresh_days}); refresh has been failing")


def cmd_comps(args: argparse.Namespace) -> int:
    """LOCAL ONLY: reads the cache, never touches the network."""
    criteria = load_criteria(args.criteria)
    data_dir = Path(getattr(args, "data_dir", None) or COMPS_DATA_DIR)
    retail, used_prev_r = comps.resolve_cache_path(
        data_dir / "namebio_retailstats.csv", data_dir / "namebio_retailstats.csv.prev")
    tldp, _ = comps.resolve_cache_path(
        data_dir / "namebio_tldstats.csv", data_dir / "namebio_tldstats.csv.prev")
    meta = comps.load_meta(data_dir)
    now = datetime.now()

    ages = []
    for spec in comps.FILE_SPECS:
        age = comps.cache_age_days(meta, spec.name, now)
        rows = (meta.get(spec.name) or {}).get("rows")
        ages.append(f"{spec.name} " + ("age unknown" if age is None else f"{age:.0f}d")
                    + (f" ({rows:,} rows)" if rows else ""))
    print("cache: " + " | ".join(ages) + ("  [using .prev!]" if used_prev_r else ""))
    _warn_if_stale(criteria, data_dir)

    ctx = comps.lookup(args.domain, comps.load_index(retail), comps.load_tld_stats(tldp),
                       criteria, retrieved=(meta.get("retailstats") or {}).get("retrieved"))
    print(f"{ctx.domain}  segmentation={ctx.segmentation}")
    if not ctx.keywords and ctx.exact is None:
        print("  no comparable sales for this keyword pattern "
              "(absence of evidence - invented names are underrepresented, NOT worthless)")
    for k in ctx.keywords:
        print(f"  {k.keyword:12s} {k.placement:6s} n={k.sale_count:<7,} "
              f"avg=${k.price_avg:,.0f}  max=${k.price_max:,.0f}  sd=${k.price_stddev:,.0f}")
    base = (ctx.tld_baseline.get("all_retail") or {})
    if base:
        print(f"  .com retail baseline: n={base.get('sale_count', 0):,} "
              f"avg=${base.get('price_avg', 0):,.0f}")
    print(comps.context_to_json(ctx))
    return 0
```

Add `from pathlib import Path` to `commands.py` imports if not already present (it is).

- [ ] **Step 4: Wire the subparsers**

In `domainscout/__main__.py`, remove `"score-submit"`/`"score-collect"` from `_STUB_HELP`? **No** — leave them (they are Phase 5c). Add before the `for name, help_text in _STUB_HELP.items()` loop:

```python
    p_comps_refresh = sub.add_parser(
        "comps-refresh",
        help="[Phase 5a] refresh the NameBio comps caches (idempotent, cron-safe)",
        description=(
            "Download + validate + swap the NameBio comps caches. PER-FILE and independent: "
            "each file has its own gate, .prev and swap, so one file's failure never discards "
            "the other's good download. retailstats is fetched first. A file no-ops unless "
            "older than [comps].refresh_days. A 429 or failed gate refuses THAT file, leaves "
            "its cache intact, and exits 0 - the next daily run is the retry."
        ),
    )
    p_comps_refresh.add_argument("--criteria", default="criteria.toml",
                                 help="path to criteria.toml (default: criteria.toml)")
    p_comps_refresh.add_argument(
        "--force", action="store_true",
        help="re-download even if fresh, and bypass the shrink check (never the header "
             "check). WARNING: can burn the NameBio download rate-limit window for HOURS.")
    p_comps_refresh.add_argument("--data-dir", dest="data_dir",
                                 help="cache directory (default: data)")
    p_comps_refresh.add_argument("--dry-run", action="store_true",
                                 help="print what would happen, write nothing")
    p_comps_refresh.set_defaults(func=commands.cmd_comps_refresh)

    p_comps = sub.add_parser(
        "comps",
        help="[Phase 5a] print the comps context for one domain (local only)",
        description="LOCAL ONLY - reads the cache, never touches the network.",
    )
    p_comps.add_argument("--domain", required=True, help="domain, e.g. cloudvault.com")
    p_comps.add_argument("--criteria", default="criteria.toml",
                         help="path to criteria.toml (default: criteria.toml)")
    p_comps.add_argument("--data-dir", dest="data_dir", help="cache directory (default: data)")
    p_comps.set_defaults(func=commands.cmd_comps)
```

- [ ] **Step 5: Ignore the cache files**

Append to `.gitignore`:

```gitignore
# Phase 5a NameBio comps caches (regenerable via `domainscout comps-refresh`)
data/namebio_*
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest -q`
Expected: PASS — all tests, 0 failures

- [ ] **Step 7: Commit**

```bash
git add domainscout/commands.py domainscout/__main__.py .gitignore tests/test_cli.py
git commit -m "feat(comps): comps-refresh + comps --domain CLI"
```

---

### Task 8: Live smoke + docs

**Files:**
- Modify: `tests/test_comps.py`
- Modify: `CLAUDE.md`
- Modify: `docs/PHASE-5A-DESIGN.md`

**Interfaces:** consumes everything above.

⚠️ **The live smoke will 429 until the download window clears** (design gotcha #3 — the 2026-07-16 spike burned it). That is expected and is exactly why it is `@pytest.mark.skip`. Do **not** run `comps-refresh --force` repeatedly to "fix" it.

- [ ] **Step 1: Add the skipped live smoke**

Append to `tests/test_comps.py` (mirrors `tests/test_rdap.py::test_live_smoke_known_registered_and_available`):

```python
@pytest.mark.skip(reason="live network - run manually against NameBio's free endpoints")
def test_live_smoke_refresh_and_lookup(tmp_path):
    from domainscout.ingest import make_client
    client = make_client()
    try:
        res = comps.refresh_cache(client, CRIT, tmp_path, force=True)
    finally:
        client.close()
    by = {f.name: f for f in res.files}
    # NB: may legitimately be REFUSED(429) if the download window has not cleared.
    assert by["retailstats"].action in ("swapped", "refused")
    if by["retailstats"].action == "swapped":
        assert by["retailstats"].rows > 50_000       # real file had 97,568
        ctx = comps.lookup("cloudvault.com", comps.load_index(tmp_path / "namebio_retailstats.csv"),
                           comps.load_tld_stats(tmp_path / "namebio_tldstats.csv"),
                           CRIT, retrieved="live")
        assert any(k.keyword == "cloud" and k.placement == "start" for k in ctx.keywords)
```

- [ ] **Step 2: Run the suite (smoke stays skipped)**

Run: `python -m pytest -q`
Expected: PASS, with 2 skipped (the RDAP smoke + this one)

- [ ] **Step 3: Update the CLAUDE.md checklist**

In `CLAUDE.md`, replace the `- [ ] Phase 5: AI scoring` line with:

```markdown
- [ ] Phase 5: AI scoring — split into 5a/5b/5c (see docs/PHASE-5A-DESIGN.md)
  - [x] Phase 5a: comps grounding (NameBio free stats; cached CSV + local lookup; $0, no API key)
  - [ ] Phase 5b: toxicity gate (Wayback CDX + Safe Browsing; needs a free Google Cloud key)
  - [ ] Phase 5c: two-tier scoring core (Haiku triage → Sonnet deep, Batch API; NEEDS an Anthropic API key)
```

- [ ] **Step 4: Record the build notes**

Append a `## Build notes (2026-07-16)` section to `docs/PHASE-5A-DESIGN.md` recording: the final test count; whether the live smoke ran or was blocked by the 429 window; and any deviation from this plan. Follow the style of `docs/PHASE-4-DESIGN.md`'s Build notes.

- [ ] **Step 5: Commit**

```bash
git add tests/test_comps.py CLAUDE.md docs/PHASE-5A-DESIGN.md
git commit -m "test(comps): skipped live smoke; docs: mark Phase 5a built"
```

---

## Self-Review

**1. Spec coverage** — every design-doc section maps to a task:

| Spec requirement | Task |
|---|---|
| `[comps]` config + **measured** rate-limit comment block | 1 |
| `CompsContext`/`KeywordComps`/`FileRefreshResult`/`RefreshResult`; attribution constant | 2 |
| raw-line index (~15 MB, not hundreds); `load_tld_stats` by name | 3 |
| keyword→placement mapping; `dict_score` segmentation reuse; "no comps" = absence | 4 |
| `value_range` JSON + **`modeled: null` guard test** | 4 |
| sanity gate (parse/header/80% shrink/first-run floor) | 5 |
| metadata sidecar `{retrieved, rows, sha256, bytes}`; degrades gracefully | 5 |
| crash-window `.prev` fallback | 5 |
| per-file independent validate-and-swap; retailstats first; mixed summary | 6 |
| 429 never retried in-run; transport error retried; `ratelimit.py` NOT reused | 6 |
| `--force` bypasses shrink, never header | 6 |
| `comps-refresh` + `comps --domain` (local-only); staleness surfaced | 7 |
| live smoke; CLAUDE.md checklist; build notes | 8 |

**2. Placeholder scan** — none. Every code step carries real code; every constant is a measured or config value. The only deliberately unspecified item is the download-window length, which the design doc labels UNCHARACTERIZED (>30 min) rather than inventing a number.

**3. Type consistency** — verified across tasks:
- `filters.dict_score(label, criteria) -> (float, str)` — Task 4 consumes only `[1]` (the segmentation). ✅ matches `domainscout/filters.py:19`.
- `ingest.make_client(timeout=30.0) -> httpx.Client` — sync. ✅ matches `domainscout/ingest.py:46`.
- `validate_download(..., baseline_rows=...)` — named `baseline_rows` (not `current_path`) in Tasks 5 **and** 6; the sidecar supplies it, so no 6.7 MB re-parse.
- `resolve_cache_path -> (Path, bool)` — Tasks 5 and 7 agree.
- `refresh_cache(client, criteria, data_dir, *, force, now, sleep)` — Tasks 6 and 7 agree; `sleep` injected so tests never wait.
- `FileRefreshResult.action ∈ {'swapped','skipped_fresh','refused'}` — Tasks 2, 6, 7 agree.
- `FileSpec.header is None` for tldstats (its 50-column header is checked by key column only, since NameBio may add periods) — handled explicitly in Task 6; `RETAILSTATS_HEADER` is matched exactly.

**4. Known deviations from the design doc, deliberate:**
- The doc's testing table said "transport error → `with_backoff` retried". `ratelimit.with_backoff` is **async** and its `RETRYABLE` is whodap-specific, so it is **unusable** here; the doc was corrected and Task 6 implements a local sync `_get_with_retry`. Called out as a Global Constraint so a reviewer does not flag it as duplication.
- `parse_placement` returns `None` for `sale_count == 0` (the doc implies but does not state it). A zero-sale placement is absence of data; surfacing it as a zero-valued comp would let 5c read "$0 average" as a real comp — the exact misread the "no comps ≠ worthless" note guards against.
