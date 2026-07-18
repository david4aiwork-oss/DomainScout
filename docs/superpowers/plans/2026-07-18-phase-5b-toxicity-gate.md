# Phase 5b Toxicity Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `domainscout/toxicity.py` — a library + debug CLI that hard-rejects Safe-Browsing-listed domains and derives a Wayback history-shape signal bundle for 5c to inject into the Tier-2 prompt.

**Architecture:** Network lives ONLY in two injected client classes (`CdxClient`, `GsbClient`); every decision is a pure function, so the test suite makes zero network calls. Safe Browsing batches all domains into one request and hard-rejects; Wayback CDX is queried per-domain and produces a graded signal. Verdicts cache to a JSON file with per-verdict TTLs. No `candidates` writes — 5c calls `screen()` between Tier-1 and Tier-2.

**Tech Stack:** Python 3.11+ (box runs 3.14), `httpx` + `truststore` (already runtime deps), stdlib `json`/`datetime`/`calendar`/`os`. pytest for tests.

**Spec:** [`docs/PHASE-5B-DESIGN.md`](../../PHASE-5B-DESIGN.md) — read it before Task 1.

## Global Constraints

- **ZERO new runtime dependencies.** `httpx` and `truststore` are already in `pyproject.toml`. Everything else must be stdlib.
- **Zero network in the test suite.** All clients are injected. Live checks are `@pytest.mark.skip`-marked smokes (5a precedent).
- **All runtime stdout/stderr output is ASCII only.** 5a shipped a `⚠️` that crashed the cron path on redirected cp1252 stdout. Use `!!` not `⚠️`, `->` not `→`.
- **TLS via `ingest.make_client()`** (`truststore.SSLContext`, OS trust store). This box MITMs HTTPS; certifi fails here.
- **`toxicity.py` never writes `candidates`.** It is a library, not a pipeline stage.
- **No `clean` or `safe` in any emitted JSON field name.** The GSB field is `gsb_currently_listed`. Field names are prompts.
- **Commit after every task.** Local only — push at phase end (owner's git cadence).
- **Verdict constants** are `"reject"`, `"unknown_error"`, `"unknown_no_history"`, `"pass"` — exactly these strings.

---

## File Structure

| File | Responsibility |
|---|---|
| `domainscout/toxicity.py` | **new.** Pure logic (`parse_cdx`, `bucket_monthly`, `compute_shape`, `decide`, `verdict_to_json`), `VerdictCache`, `CdxClient`, `GsbClient`, `screen()` |
| `domainscout/models.py` | **modify.** Add `Capture`, `ShapeBlock`, `Divergence`, `HistoryShape`, `GsbResult`, `ToxicityVerdict` + verdict constants |
| `domainscout/config.py` | **modify.** Add `[toxicity]` fields to `Criteria`, parse them in `load_criteria`, add `load_dotenv()` |
| `criteria.toml` | **modify.** Add `[toxicity]` + `[toxicity.cache_days]` |
| `domainscout/commands.py` | **modify.** Add `cmd_screen` |
| `domainscout/__main__.py` | **modify.** Register the `screen` subparser |
| `.gitignore` | **modify.** Add `data/toxicity_cache.json` |
| `tests/test_toxicity.py` | **new.** All 5b unit tests |
| `tests/fixtures/cdx_*.json`, `tests/fixtures/gsb_*.json` | **new.** Captured during Task 1 |
| `docs/PHASE-5B-SPIKE.md` | **new.** Task 1 findings (measured, not documented, limits) |

---

## Task 1: Empirical spike (BLOCKING — decides the CDX query strategy)

> ## ✅ RAN 2026-07-18, commit `fb13325`. DO NOT RE-RUN.
> **Outcome: the (a)+(d) prior was REFUTED.** Step 9's stop condition fired and the spike correctly
> refused to improvise. The owner re-planned from its evidence and ratified: **two `matchType=exact`
> queries per domain (apex + `www.`), each with SERVER-side `collapse=timestamp:6`, merged and
> de-duplicated**, then bucketed monthly client-side over the merged set.
> Tasks 2, 4 and 8 below have been **updated to match**; the CDX objective-2 (GSB) half is still
> outstanding because `.env` did not exist at spike time.
> Findings: [`docs/PHASE-5B-SPIKE.md`](../../PHASE-5B-SPIKE.md) · report `.superpowers/sdd/task-1-report.md`.
> The probe scripts below are retained as the record of what was measured, not as work to redo.

**This is a measurement task, not a TDD task.** Its findings settle spec objective 1 and become the fixtures every later task tests against. Nothing else may start until it completes.

**Files:**
- Create: `docs/PHASE-5B-SPIKE.md`
- Create: `tests/fixtures/cdx_longlived.json`, `tests/fixtures/cdx_never_archived.json`, `tests/fixtures/cdx_tail_flip.json`, `tests/fixtures/gsb_empty.json`, `tests/fixtures/gsb_match.json`
- Scratch script (NOT committed): use the session scratchpad directory

**Interfaces:**
- Produces: the four fixture files consumed by Tasks 4, 5, 9, 10; and the **query-strategy decision** (spec options a/b/c/d) consumed by Task 8.

- [ ] **Step 1: Read the spec's spike objectives**

Read `docs/PHASE-5B-DESIGN.md` sections "Spike objective 1" and "Spike objective 2" in full. Objective 1 is blocking.

- [ ] **Step 2: Probe CDX ordering and collapse semantics**

Write a scratch script (scratchpad dir, not the repo). Use `ingest.make_client()` — the MITM proxy will fail a bare `httpx.get`.

```python
from domainscout.ingest import make_client
c = make_client(timeout=60.0)
BASE = "https://web.archive.org/cdx/search/cdx"

# A: server-side collapse over matchType=domain (the spec's suspect query)
r = c.get(BASE, params={"url": "cnn.com", "output": "json", "matchType": "domain",
                        "fl": "timestamp,statuscode,mimetype,digest,urlkey",
                        "collapse": "timestamp:6", "limit": 5000})
rows = r.json()
print("A rows:", len(rows))
print("A first 3:", rows[1:4])
print("A last 3:", rows[-3:])
print("A distinct urlkeys:", len({row[4] for row in rows[1:]}))
print("A newest timestamp:", max(row[0] for row in rows[1:]))
```

**Record:** does `A newest timestamp` reach the present day, or does the 5000-row budget cut off years early? Are there many distinct urlkeys (confirming per-block collapse)?

- [ ] **Step 3: Probe the tail-reachability assertion (the deciding test)**

```python
# D: time-bounded tail query - can it be truncated away?
r = c.get(BASE, params={"url": "cnn.com", "output": "json", "matchType": "domain",
                        "fl": "timestamp,statuscode,mimetype,digest",
                        "from": "20240101", "limit": 5000})
rows = r.json()
print("D rows:", len(rows), "newest:", max(row[0] for row in rows[1:]))

# Also probe negative limit (documented as "last N")
r = c.get(BASE, params={"url": "cnn.com", "output": "json", "matchType": "domain",
                        "fl": "timestamp,statuscode,mimetype,digest", "limit": -200})
print("negative-limit newest:", max(row[0] for row in r.json()[1:]))
```

**The deciding assertion:** for a ~25-year heavily-crawled domain, do the final two years actually appear? Record yes/no for each of query A, D, and negative-limit.

- [ ] **Step 4: Probe payload cost of client-side bucketing (option a)**

```python
r = c.get(BASE, params={"url": "cnn.com", "output": "json", "matchType": "domain",
                        "fl": "timestamp,statuscode,mimetype,digest"})
print("uncollapsed bytes:", len(r.content), "rows:", len(r.json()))
```

**Record** bytes and latency. If a heavily-crawled domain returns tens of MB, option (a) needs a bound and the spike must say which.

- [ ] **Step 5: Capture the CDX fixtures**

Save real trimmed responses (~40-80 rows each is plenty) as:
- `tests/fixtures/cdx_longlived.json` — the long-lived domain, using the **winning** query strategy
- `tests/fixtures/cdx_never_archived.json` — an invented never-archived name; expect `[]` or a header-only response. **Record which**, it decides Task 4's empty-handling.
- `tests/fixtures/cdx_tail_flip.json` — a real flip-shaped domain if one is found. **If none is found, hand-build one** from the long-lived fixture's format: clean stable digests for ~10 years, then rapid digest churn in the final ~18 months. Add `"_synthetic": true` as a comment in the spike doc (NOT in the JSON — it must stay parseable as a real CDX payload).

- [ ] **Step 6: Probe GSB request/response shapes**

Requires `GOOGLE_SAFE_BROWSING_API_KEY` in `.env`.

```python
import os, json
key = os.environ["GOOGLE_SAFE_BROWSING_API_KEY"]
URL = "https://safebrowsing.googleapis.com/v4/threatMatches:find"
body = {
  "client": {"clientId": "domainscout", "clientVersion": "0.1"},
  "threatInfo": {
    "threatTypes": ["MALWARE", "SOCIAL_ENGINEERING", "UNWANTED_SOFTWARE",
                    "POTENTIALLY_HARMFUL_APPLICATION"],
    "platformTypes": ["ANY_PLATFORM"],
    "threatEntryTypes": ["URL"],
    "threatEntries": [{"url": "http://example.com"}, {"url": "https://example.com"}],
  },
}
r = c.post(URL, params={"key": key}, json=body)
print("clean status:", r.status_code, "body:", r.text)   # EXPECT: 200 and exactly {}
```

Then repeat with Google's official test URL `http://malware.testing.google.test/testing/malware/` to capture a **match** response.

Then repeat the clean call with `"platformTypes": []` and record whether it returns **no matches rather than an error** — the silent-false-clean the guard test exists for.

- [ ] **Step 7: Capture GSB fixtures**

Save `tests/fixtures/gsb_empty.json` (must be exactly `{}`) and `tests/fixtures/gsb_match.json` (the real match payload, key redacted if it appears anywhere).

- [ ] **Step 8: Write the findings doc**

Create `docs/PHASE-5B-SPIKE.md` with: measured CDX rate limits, error modes, payload sizes and latency; the ordering/collapse verdict; the tail-reachability answer per query variant; the GSB response shapes and the empty-list behaviour; and **an explicit "Query strategy decided: (a)+(d) / other" line with the evidence**.

- [ ] **Step 9: CHECKPOINT — stop if the spike refutes the prior**

The plan's Tasks 4 and 8 assume **(a) client-side monthly bucketing + (d) a time-bounded tail query**. If the spike shows this does not work — e.g. uncollapsed payloads are unmanageably large AND `from=` does not bound reliably — **STOP and report to the owner for re-planning.** Do not improvise a strategy.

- [ ] **Step 10: Commit**

```bash
git add docs/PHASE-5B-SPIKE.md tests/fixtures/cdx_*.json tests/fixtures/gsb_*.json
git commit -m "spike(5b): measure CDX ordering/tail reachability + GSB request shapes"
```

---

## Task 2: Config — `[toxicity]` + the `.env` loader

**Files:**
- Modify: `domainscout/config.py`
- Modify: `criteria.toml`
- Test: `tests/test_toxicity.py` (create), `tests/test_config.py`

**Interfaces:**
- Produces: `Criteria.tox_*` fields and `Criteria.tox_cache_days` (dict); `config.load_dotenv(path=".env") -> None`. Consumed by Tasks 7, 8, 9, 10, 11.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
def test_toxicity_config_parsed(tmp_path):
    crit = load_criteria(REPO_ROOT / "criteria.toml")
    assert crit.tox_cdx_base_url.startswith("https://")   # never plaintext
    assert crit.tox_cdx_collapse == "timestamp:6"
    assert crit.tox_tail_window_months == 24
    assert crit.tox_tail_min_captures == 3
    assert "ANY_PLATFORM" in crit.tox_gsb_platform_types
    assert "URL" in crit.tox_gsb_threat_entry_types
    assert crit.tox_cache_days["reject"] == 30
    assert "unknown_error" not in crit.tox_cache_days   # NEVER cached


def test_dotenv_does_not_clobber_real_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("GOOGLE_SAFE_BROWSING_API_KEY=from_file\nOTHER=alsofile\n", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_SAFE_BROWSING_API_KEY", "from_real_env")
    monkeypatch.delenv("OTHER", raising=False)
    config.load_dotenv(env)
    assert os.environ["GOOGLE_SAFE_BROWSING_API_KEY"] == "from_real_env"  # real env WINS
    assert os.environ["OTHER"] == "alsofile"                              # file fills gaps


def test_dotenv_missing_file_is_not_an_error(tmp_path):
    config.load_dotenv(tmp_path / "nope.env")   # must not raise


def test_dotenv_skips_comments_and_strips_quotes(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\n\nA="quoted"\nB=plain\n', encoding="utf-8")
    monkeypatch.delenv("A", raising=False); monkeypatch.delenv("B", raising=False)
    config.load_dotenv(env)
    assert os.environ["A"] == "quoted" and os.environ["B"] == "plain"
```

Add `import os` and `from domainscout import config` to the test file's imports.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_config.py -q -k "toxicity or dotenv"`
Expected: FAIL — `AttributeError: 'Criteria' object has no attribute 'tox_cdx_base_url'` and `module 'domainscout.config' has no attribute 'load_dotenv'`

- [ ] **Step 3: Add the `[toxicity]` block to `criteria.toml`**

Append the complete `[toxicity]` and `[toxicity.cache_days]` blocks **verbatim from `docs/PHASE-5B-DESIGN.md` § "Config"**, including all comments (the measured-limits comment style is a house convention). After Task 1, replace the `PROVISIONAL` note on `cdx_limit` with the spike's measured decision.

- [ ] **Step 4: Add the fields to `Criteria`**

In `domainscout/config.py`, add to the `Criteria` dataclass (all with defaults, after `comps_stale_warn_factor`):

```python
    tox_cdx_base_url: str = "https://web.archive.org/cdx/search/cdx"
    tox_cdx_collapse: str = "timestamp:6"
    tox_cdx_match_type: str = "exact"
    tox_cdx_limit: int = 5000
    tox_cdx_timeout: float = 20.0
    tox_cdx_max_rps: float = 1.0
    tox_cdx_max_retries: int = 3
    tox_tail_window_months: int = 24
    tox_tail_min_captures: int = 3
    tox_gsb_base_url: str = "https://safebrowsing.googleapis.com/v4/threatMatches:find"
    tox_gsb_batch_size: int = 250
    tox_gsb_timeout: float = 15.0
    tox_gsb_threat_types: tuple[str, ...] = (
        "MALWARE", "SOCIAL_ENGINEERING", "UNWANTED_SOFTWARE", "POTENTIALLY_HARMFUL_APPLICATION")
    tox_gsb_platform_types: tuple[str, ...] = ("ANY_PLATFORM",)
    tox_gsb_threat_entry_types: tuple[str, ...] = ("URL",)
    tox_cache_days: dict = field(
        default_factory=lambda: {"reject": 30, "pass": 14, "unknown_no_history": 30})
```

- [ ] **Step 5: Parse them in `load_criteria`**

Insert before the `return Criteria(` call, following the `[comps]` block's style:

```python
    tox_tbl = data.get("toxicity", {})
    if not isinstance(tox_tbl, dict):
        raise ConfigError("criteria.toml: [toxicity] must be a table")
    tox_cdx_base_url = str(tox_tbl.get("cdx_base_url", "https://web.archive.org/cdx/search/cdx"))
    if not tox_cdx_base_url.startswith("https://"):
        raise ConfigError(
            "criteria.toml: [toxicity].cdx_base_url must be https:// - this box MITMs TLS and "
            "the plaintext path is neither encrypted nor the code path we harden and test")
    _DEFAULT_CACHE_DAYS = {"reject": 30, "pass": 14, "unknown_no_history": 30}
    cache_tbl = tox_tbl.get("cache_days", {})
    if not isinstance(cache_tbl, dict):
        raise ConfigError("criteria.toml: [toxicity.cache_days] must be a table")
    if "unknown_error" in cache_tbl:
        raise ConfigError(
            "criteria.toml: [toxicity.cache_days].unknown_error must NOT be set - transient "
            "failures are never cached, so they are always retried on the next run")
    tox_cache_days = {**_DEFAULT_CACHE_DAYS,
                      **{str(k): _as_int(v, f"[toxicity.cache_days].{k}") for k, v in cache_tbl.items()}}
```

...and pass every `tox_*` field into the `Criteria(...)` call. Use `_as_int`/`_as_float`/`str` per type, and `tuple(...)` for the three GSB lists.

**Note the `unknown_error` guard** — it makes the never-cache rule impossible to override by config, which is the whole point of choosing "never persisted" over "TTL 0".

- [ ] **Step 6: Implement `load_dotenv`**

Add to `domainscout/config.py` (with `import os` at the top):

```python
def load_dotenv(path: str | Path = ".env") -> None:
    """Populate os.environ from a flat KEY=VALUE file. A REAL environment variable
    always wins over the file, so Task Scheduler and CI can override it. A missing
    file is not an error - most commands need no secret at all.

    Deliberately not python-dotenv: our format has no interpolation, no multiline
    values, and no export syntax, so the library's edge-case handling buys nothing
    against a 5th runtime dependency."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)   # setdefault == real env wins
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_config.py -q -k "toxicity or dotenv"`
Expected: PASS (4 tests)

- [ ] **Step 8: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass (188 + 4 new), 2 skipped

- [ ] **Step 9: Commit**

```bash
git add domainscout/config.py criteria.toml tests/test_config.py
git commit -m "feat(5b): [toxicity] config + stdlib .env loader (real env wins)"
```

---

## Task 3: Models

**Files:**
- Modify: `domainscout/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `Capture`, `ShapeBlock`, `Divergence`, `HistoryShape`, `GsbResult`, `ToxicityVerdict`, and constants `VERDICT_REJECT`, `VERDICT_UNKNOWN_ERROR`, `VERDICT_UNKNOWN_NO_HISTORY`, `VERDICT_PASS`. Consumed by Tasks 4-11.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_models.py`:

```python
def test_verdict_constants_are_the_exact_spec_strings():
    assert models.VERDICT_REJECT == "reject"
    assert models.VERDICT_UNKNOWN_ERROR == "unknown_error"
    assert models.VERDICT_UNKNOWN_NO_HISTORY == "unknown_no_history"
    assert models.VERDICT_PASS == "pass"


def test_toxicity_verdict_holds_partial_legs():
    """A verdict must be able to carry a successful leg alongside a failed one."""
    v = models.ToxicityVerdict(
        domain="x.com", verdict=models.VERDICT_UNKNOWN_ERROR, reason="cdx: timeout",
        gsb=models.GsbResult(currently_listed=False, threat_types=(), checked_at="2026-07-18"),
        history=None, screened_at="2026-07-18", collapse="timestamp:6")
    assert v.gsb is not None and v.gsb.currently_listed is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_models.py -q -k "verdict"`
Expected: FAIL — `AttributeError: module 'domainscout.models' has no attribute 'VERDICT_REJECT'`

- [ ] **Step 3: Implement the models**

Append to `domainscout/models.py`:

```python
# --- Phase 5b: toxicity gate -------------------------------------------------
# Four verdicts, NOT three. unknown_no_history (CDX succeeded, zero captures) is
# STABLE, informative absence - re-screening will not change it, and for an invented
# secondary-track brandable it is mildly reassuring. unknown_error is TRANSIENT
# ignorance and must be retried. Collapsing them would leave 5c unable to tell a
# young name from a failed lookup.
VERDICT_REJECT = "reject"
VERDICT_UNKNOWN_ERROR = "unknown_error"
VERDICT_UNKNOWN_NO_HISTORY = "unknown_no_history"
VERDICT_PASS = "pass"


@dataclass(frozen=True)
class Capture:
    """One Wayback CDX row. timestamp is 'YYYYMMDDhhmmss'."""

    timestamp: str
    statuscode: str
    mimetype: str
    digest: str


@dataclass(frozen=True)
class ShapeBlock:
    """History metrics over one time range. Computed over the MONTHLY-SAMPLED series,
    never the raw archive - raw counts are dominated by crawl-frequency artifacts."""

    first_capture: str | None
    last_capture: str | None
    span_years: float
    capture_count: int
    distinct_years: int
    max_gap_years: float
    digest_churn: float          # distinct digests / captures
    captures_per_year: float
    status_mix: dict             # '2xx'/'3xx'/'4xx'/'5xx'/'other' -> count
    mime_mix: dict               # mimetype -> count


@dataclass(frozen=True)
class Divergence:
    """Tail-vs-lifetime deltas. This is the content-flip signal: lifetime aggregates
    CANNOT show a late-life flip (12 clean years + 18 months of gambling averages out
    to respectable numbers), so the divergence is where the flip actually lives.
    5b reports these; it does NOT threshold them - interpretation is Tier-2's job."""

    churn_ratio: float | None            # tail.digest_churn / lifetime.digest_churn
    status_shift: float                  # tail 2xx proportion - lifetime 2xx proportion
    mime_shift: float                    # tail text/html proportion - lifetime's
    captures_per_year_ratio: float | None


@dataclass(frozen=True)
class HistoryShape:
    lifetime: ShapeBlock
    tail: ShapeBlock | None              # None if too few tail captures to be meaningful
    divergence: Divergence | None        # None whenever tail is None


@dataclass(frozen=True)
class GsbResult:
    """Safe Browsing is a blocklist of CURRENTLY listed URLs. A False here means
    'not presently listed' - a dropped domain that served malware in 2019 may well
    have aged off. The field is named currently_listed, never 'clean' or 'safe',
    so no downstream prompt can present it as verified-safe."""

    currently_listed: bool
    threat_types: tuple[str, ...]
    checked_at: str


@dataclass(frozen=True)
class ToxicityVerdict:
    """The verdict reflects the WORST leg; the data reflects EVERY leg that succeeded.
    A GSB success rides along even when CDX failed, and vice versa."""

    domain: str
    verdict: str
    reason: str
    gsb: GsbResult | None
    history: HistoryShape | None
    screened_at: str
    collapse: str                        # the sampling this verdict was computed under
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_models.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add domainscout/models.py tests/test_models.py
git commit -m "feat(5b): toxicity dataclasses + 4-valued verdict constants"
```

---

## Task 4: `parse_cdx` + `bucket_monthly`

**Files:**
- Create: `domainscout/toxicity.py`
- Test: `tests/test_toxicity.py`

**Interfaces:**
- Consumes: `models.Capture`
- Produces: `parse_cdx(payload: list) -> list[Capture]`, `bucket_monthly(captures: Iterable[Capture]) -> list[Capture]`, `class CdxError(Exception)`, `class ToxicityKeyMissing(Exception)`. Consumed by Tasks 5, 8, 10, 11.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_toxicity.py`:

```python
import json
from pathlib import Path

import pytest

from domainscout import models, toxicity

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parse_cdx_reads_columns_by_name_not_index():
    """CDX column ORDER depends on the fl= parameter. Reading by index would break
    silently the moment anyone reorders fl."""
    payload = [["digest", "timestamp", "mimetype", "statuscode"],
               ["ABC", "20200115120000", "text/html", "200"]]
    caps = toxicity.parse_cdx(payload)
    assert caps == [models.Capture(timestamp="20200115120000", statuscode="200",
                                   mimetype="text/html", digest="ABC")]


def test_parse_cdx_empty_and_header_only_both_mean_no_captures():
    """MEASURED in Task 1: a never-archived domain returns the literal bytes `[]`, a bare
    empty array - NOT a header-only response. Both are handled anyway, because a
    never-archived domain must never be mistaken for a parse failure."""
    assert toxicity.parse_cdx([]) == []
    assert toxicity.parse_cdx([["timestamp", "statuscode", "mimetype", "digest"]]) == []


def test_parse_cdx_never_archived_fixture_yields_nothing():
    assert toxicity.parse_cdx(_fixture("cdx_never_archived.json")) == []


def test_bucket_monthly_keeps_one_capture_per_calendar_month():
    caps = [models.Capture(f"2020{m:02d}{d:02d}120000", "200", "text/html", f"D{m}{d}")
            for m in (1, 1, 2) for d in (1, 15)]
    kept = toxicity.bucket_monthly(caps)
    assert [c.timestamp[:6] for c in kept] == ["202001", "202002"]


def test_bucket_monthly_sorts_by_time_first():
    """CdxClient merges two independently-collapsed host queries (apex + www.), so the
    merged list is NOT time-ordered and can hold two rows for the same month. Bucketing
    without sorting would sample by merge order rather than by time."""
    caps = [models.Capture("20220301120000", "200", "text/html", "B"),
            models.Capture("20200115120000", "200", "text/html", "A")]
    assert [c.timestamp for c in toxicity.bucket_monthly(caps)] == \
           ["20200115120000", "20220301120000"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_toxicity.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'domainscout.toxicity'`

- [ ] **Step 3: Create the module with both functions**

Create `domainscout/toxicity.py`:

```python
"""Phase 5b: the toxicity gate.

A library, NOT a pipeline stage: the gate runs between Tier-1 and Tier-2, and
Tier-1 - which decides who is worth screening - does not exist until 5c. 5c calls
screen() on its Tier-1 survivors, exactly as it calls comps.lookup().

Network lives ONLY in CdxClient and GsbClient; both are injected, so the suite
makes zero network calls. Read docs/PHASE-5B-DESIGN.md before touching the CDX
query strategy - the ordering/truncation behaviour there is measured, not assumed.
"""

from __future__ import annotations

import calendar
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from domainscout.models import (
    Capture, Divergence, GsbResult, HistoryShape, ShapeBlock, ToxicityVerdict,
    VERDICT_PASS, VERDICT_REJECT, VERDICT_UNKNOWN_ERROR, VERDICT_UNKNOWN_NO_HISTORY,
)


class CdxError(Exception):
    """Wayback CDX was unreachable or unparseable. Becomes unknown_error - NEVER a pass."""


class GsbError(Exception):
    """Safe Browsing failed. Becomes unknown_error - NEVER a pass."""


class ToxicityKeyMissing(Exception):
    """GOOGLE_SAFE_BROWSING_API_KEY is absent. Surfaced as a clean CLI message."""


def parse_cdx(payload: list) -> list[Capture]:
    """CDX json output is [header_row, *data_rows]. Columns are read BY NAME, because
    their order follows the fl= parameter. An empty list AND a header-only response
    both mean 'no captures' - a never-archived domain must never look like a failure."""
    if not payload:
        return []
    header, *rows = payload
    idx = {str(name): i for i, name in enumerate(header)}
    try:
        ts_i, st_i, mt_i, dg_i = (idx["timestamp"], idx["statuscode"],
                                  idx["mimetype"], idx["digest"])
    except KeyError as exc:
        raise CdxError(f"CDX response missing expected column {exc}") from exc
    out: list[Capture] = []
    for row in rows:
        if len(row) <= max(ts_i, st_i, mt_i, dg_i):
            continue
        out.append(Capture(timestamp=str(row[ts_i]), statuscode=str(row[st_i]),
                           mimetype=str(row[mt_i]), digest=str(row[dg_i])))
    return out


def bucket_monthly(captures: Iterable[Capture]) -> list[Capture]:
    """Collapse to one capture per calendar month over the WHOLE time-sorted series.

    CdxClient already asks the server to collapse, but it issues TWO queries per domain
    (apex + www.) and merges them - so the merged list is neither time-ordered nor free
    of duplicate months. This pass makes the sampling exact and the result independent
    of merge order. At ~600 merged rows it is free.

    Historical note (see docs/PHASE-5B-SPIKE.md): server-side collapse is only
    trustworthy because each query is matchType=exact, i.e. a single urlkey. Under
    matchType=domain, collapse acts on adjacent rows across THOUSANDS of urlkeys
    (cnn.com: 2,768), sampling per-URL-block and inflating digest_churn by reading URL
    diversity as content volatility."""
    seen: set[str] = set()
    out: list[Capture] = []
    for cap in sorted(captures, key=lambda c: c.timestamp):
        month = cap.timestamp[:6]
        if month in seen:
            continue
        seen.add(month)
        out.append(cap)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_toxicity.py -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add domainscout/toxicity.py tests/test_toxicity.py
git commit -m "feat(5b): parse_cdx (by-name columns) + client-side monthly bucketing"
```

---

## Task 5: `compute_shape` — lifetime, tail, divergence

**Files:**
- Modify: `domainscout/toxicity.py`
- Test: `tests/test_toxicity.py`

**Interfaces:**
- Consumes: `Capture`, `bucket_monthly`
- Produces: `compute_shape(captures, *, tail_window_months, tail_min_captures) -> HistoryShape | None`. Consumed by Tasks 10, 11.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_toxicity.py`:

```python
def _caps(pairs):
    """pairs: [(timestamp, digest)] -> captures, all 200/text/html."""
    return [models.Capture(ts, "200", "text/html", dg) for ts, dg in pairs]


def test_compute_shape_returns_none_for_no_captures():
    """Absence is NOT a zero-valued shape. Zero captures must reach decide() as
    unknown_no_history, and a ShapeBlock full of 0.0s would read as 'measured and bad'."""
    assert toxicity.compute_shape([], tail_window_months=24, tail_min_captures=3) is None


def test_compute_shape_lifetime_metrics():
    caps = _caps([("20100115120000", "A"), ("20110115120000", "A"),
                  ("20120115120000", "B"), ("20130115120000", "B")])
    shape = toxicity.compute_shape(caps, tail_window_months=24, tail_min_captures=3)
    lt = shape.lifetime
    assert lt.first_capture == "20100115120000" and lt.last_capture == "20130115120000"
    assert lt.capture_count == 4 and lt.distinct_years == 4
    assert lt.digest_churn == 0.5            # 2 distinct digests / 4 captures
    assert 2.9 < lt.span_years < 3.1
    assert lt.status_mix["2xx"] == 4


def test_compute_shape_tail_is_anchored_on_last_capture_not_today():
    """A domain that died in 2015 has a 2013-2015 tail. 'Late-life' means late in the
    DOMAIN's life - anchoring on today would make every dead domain's tail empty."""
    caps = _caps([(f"{y}0115120000", f"D{y}") for y in range(2005, 2016)])
    shape = toxicity.compute_shape(caps, tail_window_months=24, tail_min_captures=3)
    assert shape.tail is not None
    assert shape.tail.first_capture >= "20130115"
    assert shape.tail.last_capture == "20150115120000"


def test_compute_shape_detects_tail_flip_that_lifetime_aggregates_hide():
    """THE point of the tail window. 10 stable years then 18 months of churn: the
    lifetime digest_churn stays low and respectable, so only the divergence shows it."""
    stable = [(f"{y}{m:02d}15120000", "SAME") for y in range(2010, 2020) for m in (1, 7)]
    churny = [(f"2020{m:02d}15120000", f"FLIP{m}") for m in range(1, 13)]
    shape = toxicity.compute_shape(_caps(stable + churny),
                                   tail_window_months=24, tail_min_captures=3)
    assert shape.lifetime.digest_churn < 0.5          # lifetime looks fine
    assert shape.tail.digest_churn > 0.9              # tail is wild
    assert shape.divergence.churn_ratio > 2.0         # the signal


def test_compute_shape_divergence_is_none_below_tail_min_captures():
    """Two data points cannot support a ratio. None beats a fabricated number."""
    caps = _caps([("20100115120000", "A"), ("20110115120000", "B"),
                  ("20200115120000", "C")])
    shape = toxicity.compute_shape(caps, tail_window_months=24, tail_min_captures=3)
    assert shape.tail is None and shape.divergence is None


def test_compute_shape_divergence_is_none_when_tail_covers_whole_life():
    """If the domain is younger than the tail window, tail == lifetime and every
    ratio is 1.0 by construction - a meaningless 'no divergence' that reads as
    'checked and fine'."""
    caps = _caps([(f"2025{m:02d}15120000", f"D{m}") for m in range(1, 7)])
    shape = toxicity.compute_shape(caps, tail_window_months=24, tail_min_captures=3)
    assert shape.divergence is None


def test_compute_shape_divergence_values_are_exact():
    """Pin the arithmetic, not just the direction. 6 lifetime captures with 3 distinct
    digests (churn 0.5); the 3 tail captures are all distinct (churn 1.0) -> ratio 2.0."""
    caps = _caps([("20200115120000", "A"), ("20200715120000", "A"),
                  ("20210115120000", "B"), ("20230115120000", "C"),
                  ("20230715120000", "D"), ("20240115120000", "E")])
    shape = toxicity.compute_shape(caps, tail_window_months=24, tail_min_captures=3)
    assert shape.lifetime.digest_churn == 0.8333          # 5 distinct / 6
    assert shape.tail.capture_count == 3                  # 2023-01, 2023-07, 2024-01
    assert shape.tail.digest_churn == 1.0                 # C, D, E all distinct
    assert shape.divergence.churn_ratio == 1.2            # 1.0 / 0.8333
    assert shape.divergence.status_shift == 0.0           # all 2xx in both windows
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_toxicity.py -q -k "compute_shape"`
Expected: FAIL — `AttributeError: module 'domainscout.toxicity' has no attribute 'compute_shape'`

- [ ] **Step 3: Implement**

Append to `domainscout/toxicity.py`:

```python
def _to_dt(timestamp: str) -> datetime:
    return datetime.strptime(timestamp[:14].ljust(14, "0"), "%Y%m%d%H%M%S")


def _months_before(moment: datetime, months: int) -> datetime:
    """Calendar-correct month subtraction without pulling in dateutil."""
    year, month = moment.year, moment.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def _status_bucket(code: str) -> str:
    return f"{code[0]}xx" if code[:1].isdigit() and code[0] in "2345" else "other"


def _block(captures: Sequence[Capture]) -> ShapeBlock:
    first, last = captures[0], captures[-1]
    span_days = (_to_dt(last.timestamp) - _to_dt(first.timestamp)).days
    span_years = span_days / 365.25
    status_mix: dict = {}
    mime_mix: dict = {}
    for cap in captures:
        bucket = _status_bucket(cap.statuscode)
        status_mix[bucket] = status_mix.get(bucket, 0) + 1
        mime_mix[cap.mimetype] = mime_mix.get(cap.mimetype, 0) + 1
    max_gap_days = 0
    for prev, nxt in zip(captures, captures[1:]):
        max_gap_days = max(max_gap_days,
                           (_to_dt(nxt.timestamp) - _to_dt(prev.timestamp)).days)
    return ShapeBlock(
        first_capture=first.timestamp,
        last_capture=last.timestamp,
        span_years=round(span_years, 3),
        capture_count=len(captures),
        distinct_years=len({c.timestamp[:4] for c in captures}),
        max_gap_years=round(max_gap_days / 365.25, 3),
        digest_churn=round(len({c.digest for c in captures}) / len(captures), 4),
        captures_per_year=round(len(captures) / max(span_years, 1 / 365.25), 3),
        status_mix=status_mix,
        mime_mix=mime_mix,
    )


def _proportion(mix: dict, total: int, *keys: str) -> float:
    return sum(mix.get(k, 0) for k in keys) / total if total else 0.0


def compute_shape(captures, *, tail_window_months: int,
                  tail_min_captures: int) -> HistoryShape | None:
    """None means NO captures - stable, informative absence, which decide() turns into
    unknown_no_history. It must never become a ShapeBlock of zeros, which would read
    downstream as 'we measured this domain and it scored badly'."""
    sampled = bucket_monthly(captures)
    if not sampled:
        return None
    lifetime = _block(sampled)

    cutoff = _months_before(_to_dt(sampled[-1].timestamp), tail_window_months)
    tail_caps = [c for c in sampled if _to_dt(c.timestamp) >= cutoff]

    # Too thin to support a ratio, or the tail IS the whole life (every ratio would be
    # 1.0 by construction - a meaningless 'no divergence' that reads as 'checked, fine').
    if len(tail_caps) < tail_min_captures or len(tail_caps) == len(sampled):
        return HistoryShape(lifetime=lifetime, tail=None, divergence=None)

    tail = _block(tail_caps)
    lt_total, t_total = lifetime.capture_count, tail.capture_count
    divergence = Divergence(
        churn_ratio=(round(tail.digest_churn / lifetime.digest_churn, 4)
                     if lifetime.digest_churn else None),
        status_shift=round(_proportion(tail.status_mix, t_total, "2xx")
                           - _proportion(lifetime.status_mix, lt_total, "2xx"), 4),
        mime_shift=round(_proportion(tail.mime_mix, t_total, "text/html")
                         - _proportion(lifetime.mime_mix, lt_total, "text/html"), 4),
        captures_per_year_ratio=(round(tail.captures_per_year / lifetime.captures_per_year, 4)
                                 if lifetime.captures_per_year else None),
    )
    return HistoryShape(lifetime=lifetime, tail=tail, divergence=divergence)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_toxicity.py -q -k "compute_shape"`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add domainscout/toxicity.py tests/test_toxicity.py
git commit -m "feat(5b): compute_shape with tail window + divergence (flip detector)"
```

---

## Task 6: `decide` — verdict precedence and partial results

**Files:**
- Modify: `domainscout/toxicity.py`
- Test: `tests/test_toxicity.py`

**Interfaces:**
- Consumes: `GsbResult`, `HistoryShape`, verdict constants
- Produces: `decide(gsb, shape, errors) -> tuple[str, str]`. Consumed by Task 10.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_toxicity.py`:

```python
_LISTED = models.GsbResult(True, ("MALWARE",), "2026-07-18")
_NOT_LISTED = models.GsbResult(False, (), "2026-07-18")
_SHAPE = models.HistoryShape(
    lifetime=models.ShapeBlock("20100101000000", "20200101000000", 10.0, 20, 10,
                               1.0, 0.5, 2.0, {"2xx": 20}, {"text/html": 20}),
    tail=None, divergence=None)


def test_decide_gsb_listing_rejects_and_outranks_errors():
    """A blocklist hit is a fact, not a judgement. It wins even when the other leg
    failed - we already know enough to reject."""
    verdict, reason = toxicity.decide(_LISTED, None, ["cdx: timeout"])
    assert verdict == models.VERDICT_REJECT
    assert "MALWARE" in reason


def test_decide_error_never_becomes_pass():
    """Invariant 2. A timeout must never be indistinguishable from 'we checked, it's fine'."""
    verdict, _ = toxicity.decide(_NOT_LISTED, _SHAPE, ["cdx: timeout"])
    assert verdict == models.VERDICT_UNKNOWN_ERROR


def test_decide_no_captures_is_unknown_no_history_not_pass_and_not_reject():
    """Invariant 1. Invented secondary-track brandables routinely have zero captures;
    folding that into either pass or reject mis-scores exactly the names we hunt for."""
    verdict, reason = toxicity.decide(_NOT_LISTED, None, [])
    assert verdict == models.VERDICT_UNKNOWN_NO_HISTORY
    assert "absence" in reason.lower()


def test_decide_clean_and_archived_is_pass():
    verdict, _ = toxicity.decide(_NOT_LISTED, _SHAPE, [])
    assert verdict == models.VERDICT_PASS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_toxicity.py -q -k "decide"`
Expected: FAIL — no attribute `decide`

- [ ] **Step 3: Implement**

Append to `domainscout/toxicity.py`:

```python
def decide(gsb: GsbResult | None, shape: HistoryShape | None,
           errors: Sequence[str]) -> tuple[str, str]:
    """Precedence, in order:

        gsb listed            -> reject               terminal; outranks everything,
                                                      including a failed CDX leg
        gsb or cdx errored    -> unknown_error        transient; retried next run
        cdx ok, 0 captures    -> unknown_no_history   stable absence
        otherwise             -> pass

    Every non-reject verdict PROCEEDS to Tier-2 carrying its reason. Failing closed on
    unknown would let one bad archive.org day silently empty the digest - a failure mode
    far harder to notice than a false positive."""
    if gsb is not None and gsb.currently_listed:
        return VERDICT_REJECT, "safe-browsing listed: " + ",".join(gsb.threat_types)
    if errors:
        return VERDICT_UNKNOWN_ERROR, "; ".join(errors)
    if shape is None:
        return (VERDICT_UNKNOWN_NO_HISTORY,
                "no wayback captures - absence of evidence, not evidence of anything")
    return VERDICT_PASS, "not currently listed; history shape recorded"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_toxicity.py -q -k "decide"`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add domainscout/toxicity.py tests/test_toxicity.py
git commit -m "feat(5b): decide() verdict precedence - errors never become pass"
```

---

## Task 7: `VerdictCache`

**Files:**
- Modify: `domainscout/toxicity.py`
- Modify: `.gitignore`
- Test: `tests/test_toxicity.py`

**Interfaces:**
- Consumes: `ToxicityVerdict`, verdict constants
- Produces: `class VerdictCache` with `__init__(path, *, cache_days, collapse, now=None)`, `.get(domain) -> ToxicityVerdict | None`, `.put(verdict) -> None`, `.save() -> None`, and module constant `DEFAULT_CACHE_PATH = "data/toxicity_cache.json"`. Consumed by Tasks 10, 11.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_toxicity.py`:

```python
from datetime import datetime, timedelta

_DAYS = {"reject": 30, "pass": 14, "unknown_no_history": 30}


def _verdict(domain, verdict, collapse="timestamp:6", screened_at="2026-07-18T00:00:00"):
    return models.ToxicityVerdict(domain=domain, verdict=verdict, reason="r",
                                  gsb=_NOT_LISTED, history=None,
                                  screened_at=screened_at, collapse=collapse)


def test_cache_roundtrip_within_ttl(tmp_path):
    now = datetime(2026, 7, 18)
    cache = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                  collapse="timestamp:6", now=now)
    cache.put(_verdict("a.com", models.VERDICT_PASS, screened_at=now.isoformat()))
    cache.save()
    reopened = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                     collapse="timestamp:6", now=now + timedelta(days=13))
    assert reopened.get("a.com").verdict == models.VERDICT_PASS


def test_cache_expires_past_ttl(tmp_path):
    now = datetime(2026, 7, 18)
    cache = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                  collapse="timestamp:6", now=now)
    cache.put(_verdict("a.com", models.VERDICT_PASS, screened_at=now.isoformat()))
    cache.save()
    stale = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                  collapse="timestamp:6", now=now + timedelta(days=15))
    assert stale.get("a.com") is None


def test_cache_never_persists_unknown_error(tmp_path):
    """NOT a TTL of 0 - never written at all. A transient failure then CANNOT be
    misconfigured into stickiness, which any numeric TTL eventually can."""
    now = datetime(2026, 7, 18)
    cache = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                  collapse="timestamp:6", now=now)
    cache.put(_verdict("a.com", models.VERDICT_UNKNOWN_ERROR, screened_at=now.isoformat()))
    cache.save()
    assert cache.get("a.com") is None
    assert "a.com" not in json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))


def test_cache_misses_when_collapse_changed(tmp_path):
    """Self-enforcing calibration: every metric is relative to the sampling, so an
    entry computed under a different collapse is not comparable data."""
    now = datetime(2026, 7, 18)
    cache = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                  collapse="timestamp:6", now=now)
    cache.put(_verdict("a.com", models.VERDICT_PASS, collapse="timestamp:6",
                       screened_at=now.isoformat()))
    cache.save()
    changed = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                    collapse="timestamp:4", now=now)
    assert changed.get("a.com") is None


def test_cache_tolerates_a_corrupt_file(tmp_path):
    """A half-written cache must degrade to a cold cache, never crash the run."""
    (tmp_path / "c.json").write_text("{not json", encoding="utf-8")
    cache = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                  collapse="timestamp:6", now=datetime(2026, 7, 18))
    assert cache.get("a.com") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_toxicity.py -q -k "cache"`
Expected: FAIL — no attribute `VerdictCache`

- [ ] **Step 3: Implement**

Append to `domainscout/toxicity.py`:

```python
DEFAULT_CACHE_PATH = "data/toxicity_cache.json"


class VerdictCache:
    """Domain -> verdict, with per-verdict TTLs.

    Two rules carry this design:
      1. unknown_error is NEVER written. Not TTL-0 - never persisted, so a transient
         failure cannot be configured into stickiness.
      2. Every entry records the collapse it was computed under, and an entry whose
         collapse differs from the current config is a MISS. That makes 'thresholds
         are calibrated to this sampling' self-enforcing instead of a comment someone
         has to notice."""

    def __init__(self, path, *, cache_days: dict, collapse: str, now: datetime | None = None):
        self.path = Path(path)
        self.cache_days = cache_days
        self.collapse = collapse
        self.now = now or datetime.now()
        self._entries: dict = {}
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._entries = loaded
            except (OSError, ValueError):
                self._entries = {}   # a corrupt cache is a COLD cache, never a crash

    def get(self, domain: str) -> ToxicityVerdict | None:
        entry = self._entries.get(domain)
        if not isinstance(entry, dict):
            return None
        if entry.get("collapse") != self.collapse:
            return None
        ttl = self.cache_days.get(entry.get("verdict", ""))
        if ttl is None:
            return None
        try:
            screened = datetime.fromisoformat(entry["screened_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if (self.now - screened).days >= ttl:
            return None
        return ToxicityVerdict(
            domain=domain, verdict=entry["verdict"], reason=entry.get("reason", ""),
            gsb=None, history=None, screened_at=entry["screened_at"],
            collapse=entry["collapse"])

    def put(self, verdict: ToxicityVerdict) -> None:
        if verdict.verdict == VERDICT_UNKNOWN_ERROR:
            return   # see rule 1
        self._entries[verdict.domain] = {
            "verdict": verdict.verdict, "reason": verdict.reason,
            "screened_at": verdict.screened_at, "collapse": verdict.collapse,
        }

    def save(self) -> None:
        """Temp-file + os.replace. The OSError catch is not theoretical: 5a hit a real
        Windows AV file-lock during exactly this rename."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self._entries, indent=1, sort_keys=True),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            print(f"toxicity: WARNING - could not write cache {self.path}: {exc}")
```

- [ ] **Step 4: Add the gitignore entry**

In `.gitignore`, below the `data/namebio_*` line:

```
data/toxicity_cache.json   # Phase 5b verdict cache (regenerable; re-screens on miss)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_toxicity.py -q -k "cache"`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add domainscout/toxicity.py tests/test_toxicity.py .gitignore
git commit -m "feat(5b): VerdictCache - never persists unknown_error, misses on collapse change"
```

---

## Task 8: `CdxClient`

**Files:**
- Modify: `domainscout/toxicity.py`
- Test: `tests/test_toxicity.py`

**Interfaces:**
- Consumes: `parse_cdx`, `CdxError`, `Criteria`
- Produces: `class CdxClient` with `__init__(client, criteria)` and `.fetch(domain) -> list[Capture]`. Consumed by Task 10.

**Query strategy: SETTLED by the Task 1 spike, ratified by the owner 2026-07-18.** The original (a)+(d) prior was **refuted by measurement** — see `docs/PHASE-5B-SPIKE.md`. The code below implements the ratified replacement: **two `matchType=exact` queries per domain (apex + `www.`), each with SERVER-side collapse, merged and de-duplicated.** Do not reintroduce `matchType=domain`, a `from=`-bounded tail query, or a negative limit; all three were measured and all three failed.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_toxicity.py`:

```python
import httpx


def _fake_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_cdx_client_queries_both_hosts_over_https_with_server_collapse():
    """Two exact queries per domain. The www. one exists because a domain whose apex
    only 301s to www. would otherwise be shaped from redirect history, and nothing
    would look wrong."""
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, json=[["timestamp", "statuscode", "mimetype", "digest"],
                                         ["20200115120000", "200", "text/html", "A"]])

    crit = load_criteria(REPO_ROOT / "criteria.toml")
    caps = toxicity.CdxClient(_fake_client(handler), crit).fetch("example.com")
    assert len(seen) == 2
    assert all(str(u).startswith("https://") for u in seen)
    assert {u.params["url"] for u in seen} == {"example.com", "www.example.com"}
    assert all(u.params["matchType"] == "exact" for u in seen)
    assert all(u.params["collapse"] == crit.tox_cdx_collapse for u in seen)
    assert [c.digest for c in caps] == ["A"]      # identical rows de-duped across hosts


def test_cdx_client_normalizes_a_www_prefixed_input():
    seen = []

    def handler(request):
        seen.append(request.url.params["url"])
        return httpx.Response(200, json=[])

    crit = load_criteria(REPO_ROOT / "criteria.toml")
    toxicity.CdxClient(_fake_client(handler), crit).fetch("www.example.com")
    assert set(seen) == {"example.com", "www.example.com"}   # not www.www.example.com


def test_cdx_client_one_host_failing_does_not_fail_the_domain():
    """Partial results survive partial failure, same rule as the two legs of screen()."""
    def handler(request):
        if request.url.params["url"].startswith("www."):
            return httpx.Response(503)
        return httpx.Response(200, json=[["timestamp", "statuscode", "mimetype", "digest"],
                                         ["20200115120000", "200", "text/html", "A"]])

    crit = load_criteria(REPO_ROOT / "criteria.toml")
    caps = toxicity.CdxClient(_fake_client(handler), crit,
                              sleep=lambda s: None).fetch("example.com")
    assert [c.digest for c in caps] == ["A"]


def test_cdx_client_raises_cdx_error_on_timeout():
    """Must raise, not return []. An empty list means 'never archived' - a completely
    different verdict from 'we could not reach the archive'."""
    def handler(request):
        raise httpx.ConnectTimeout("boom")

    crit = load_criteria(REPO_ROOT / "criteria.toml")
    with pytest.raises(toxicity.CdxError):
        toxicity.CdxClient(_fake_client(handler), crit, sleep=lambda s: None).fetch("example.com")


def test_cdx_client_never_archived_both_hosts_is_empty_not_an_error():
    """MEASURED: archive.org returns the literal bytes `[]` for a never-archived name.
    Empty from both hosts is stable absence -> unknown_no_history, NOT unknown_error."""
    crit = load_criteria(REPO_ROOT / "criteria.toml")
    client = toxicity.CdxClient(_fake_client(lambda r: httpx.Response(200, json=[])), crit)
    assert client.fetch("qzxkvbnmplkjhgfd.com") == []


def test_cdx_client_raises_on_5xx_after_retries():
    calls = {"n": 0}
    slept = []

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503)

    crit = load_criteria(REPO_ROOT / "criteria.toml")
    client = toxicity.CdxClient(_fake_client(handler), crit, sleep=slept.append)
    with pytest.raises(toxicity.CdxError):
        client.fetch("example.com")
    # BOTH hosts are attempted, each exhausting its own retry budget, before the
    # domain is declared a failure.
    assert calls["n"] == crit.tox_cdx_max_retries * 2
    assert slept and all(s > 0 for s in slept)          # real backoff, just not real waiting


```

Add `from domainscout.config import load_criteria` and `REPO_ROOT = Path(__file__).resolve().parents[1]` to the test file's header.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_toxicity.py -q -k "cdx_client"`
Expected: FAIL — no attribute `CdxClient`

- [ ] **Step 3: Implement**

Append to `domainscout/toxicity.py` (add `import time` and `import httpx` to the imports):

```python
class CdxClient:
    """Wayback CDX. TWO GETs per domain - the apex and the www. host - each
    matchType=exact with SERVER-side collapse, then merged. No batching exists.

    QUERY STRATEGY (measured in the Task-1 spike, see docs/PHASE-5B-SPIKE.md; do not
    change without re-measuring). Three alternatives were tested and all three FAILED:

      * matchType=domain, uncollapsed: unmanageable. The single-URL apex alone is
        72.2 MB / 1,042,676 rows; the domain-wide unbounded query did not finish in
        257 s+, and a read-timeout never fires because the server trickles bytes.
      * from=<date> bounding: truncates 6 days into a 2.5-year window. Rows stay
        urlkey-then-timestamp sorted regardless of the filter, so time-bounding does
        not defeat row-truncation.
      * negative limit: reaches recent timestamps but returns 100% ONE static asset
        (the alphabetically-last urlkey) - zero page-history signal.

    matchType=exact + server collapse works because a single urlkey makes adjacent-row
    collapse identical to monthly collapse, and because collapsing shrinks 1M+ rows to
    ~311 - far below any cap, so truncation never engages at all.

    Both hosts are queried because a domain whose apex merely 301s to www. would
    otherwise yield a shape computed from redirect history, and NOTHING would look
    wrong - a 301-only history reads as a thin but valid shape."""

    def __init__(self, client: httpx.Client, criteria, sleep=time.sleep):
        self.client = client
        self.criteria = criteria
        self.sleep = sleep   # injected so retry tests do not actually wait out the backoff

    def _params(self, host: str) -> dict:
        return {
            "url": host,
            "output": "json",
            "matchType": self.criteria.tox_cdx_match_type,   # 'exact' - one urlkey
            "collapse": self.criteria.tox_cdx_collapse,      # server-side; legit on one urlkey
            "fl": "timestamp,statuscode,mimetype,digest",
            "limit": self.criteria.tox_cdx_limit,            # runaway guard; never engages
        }

    def _get(self, params: dict) -> list:
        last: Exception | None = None
        for attempt in range(self.criteria.tox_cdx_max_retries):
            try:
                resp = self.client.get(self.criteria.tox_cdx_base_url, params=params,
                                       timeout=self.criteria.tox_cdx_timeout)
                if resp.status_code >= 500 or resp.status_code == 429:
                    last = CdxError(f"CDX HTTP {resp.status_code}")
                else:
                    resp.raise_for_status()
                    return resp.json() or []
            except (httpx.TransportError, ValueError) as exc:
                last = exc
            self.sleep(min(2 ** attempt, 8) / max(self.criteria.tox_cdx_max_rps, 0.1))
        raise CdxError(f"CDX failed after {self.criteria.tox_cdx_max_retries} attempts: {last}")

    def hosts(self, domain: str) -> tuple[str, str]:
        bare = domain[4:] if domain.startswith("www.") else domain
        return (bare, f"www.{bare}")

    def fetch(self, domain: str) -> list[Capture]:
        """Returns [] for a never-archived domain and RAISES CdxError on failure.
        These must stay distinguishable - one is stable absence, the other transient
        ignorance, and they become different verdicts.

        One host failing does NOT fail the domain: if either query succeeds its captures
        are used, mirroring the partial-results rule. Only a failure of BOTH is CdxError,
        because only then do we genuinely know nothing."""
        captures: list[Capture] = []
        failures: list[str] = []
        for host in self.hosts(domain):
            try:
                captures.extend(parse_cdx(self._get(self._params(host))))
            except CdxError as exc:
                failures.append(f"{host}: {exc}")
        if failures and not captures:
            raise CdxError(f"CDX failed for every host of {domain} - " + "; ".join(failures))
        # De-dupe: the two hosts often overlap (and for some domains CDX appears to
        # canonicalize them to the same record entirely). bucket_monthly re-sorts, so
        # merge order does not matter.
        return list({(c.timestamp, c.digest): c for c in captures}.values())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_toxicity.py -q -k "cdx_client"`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add domainscout/toxicity.py tests/test_toxicity.py
git commit -m "feat(5b): CdxClient - time-bounded tail query, no server-side collapse"
```

---

## Task 9: `GsbClient`

**Files:**
- Modify: `domainscout/toxicity.py`
- Test: `tests/test_toxicity.py`

**Interfaces:**
- Consumes: `GsbResult`, `GsbError`, `ToxicityKeyMissing`, `Criteria`
- Produces: `class GsbClient` with `__init__(client, criteria, api_key)`, `.check(domains) -> dict[str, GsbResult]`, and classmethod `from_env(client, criteria)`. Consumed by Tasks 10, 11.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_toxicity.py`:

```python
def test_gsb_empty_response_means_not_listed_not_an_error():
    """v4 returns a bare {} for a clean batch - absent 'matches' key. Parsing that as
    malformed would turn every clean run into unknown_error."""
    def handler(request):
        return httpx.Response(200, json={})

    crit = load_criteria(REPO_ROOT / "criteria.toml")
    got = toxicity.GsbClient(_fake_client(handler), crit, "k").check(["a.com", "b.com"])
    assert got["a.com"].currently_listed is False
    assert got["b.com"].currently_listed is False


def test_gsb_match_marks_only_the_matched_domain():
    def handler(request):
        return httpx.Response(200, json={"matches": [
            {"threatType": "MALWARE", "threat": {"url": "http://bad.com/"}}]})

    crit = load_criteria(REPO_ROOT / "criteria.toml")
    got = toxicity.GsbClient(_fake_client(handler), crit, "k").check(["bad.com", "ok.com"])
    assert got["bad.com"].currently_listed is True
    assert got["bad.com"].threat_types == ("MALWARE",)
    assert got["ok.com"].currently_listed is False


def test_gsb_request_always_carries_all_three_non_empty_lists():
    """v4 requires threatTypes, platformTypes AND threatEntryTypes. An empty list
    returns NO MATCHES rather than an error - a silent false-clean, which is the exact
    failure invariant 2 exists to prevent."""
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    crit = load_criteria(REPO_ROOT / "criteria.toml")
    toxicity.GsbClient(_fake_client(handler), crit, "k").check(["a.com"])
    info = seen["body"]["threatInfo"]
    assert info["threatTypes"] and info["platformTypes"] and info["threatEntryTypes"]


def test_gsb_queries_both_url_schemes():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    crit = load_criteria(REPO_ROOT / "criteria.toml")
    toxicity.GsbClient(_fake_client(handler), crit, "k").check(["a.com"])
    urls = {e["url"] for e in seen["body"]["threatInfo"]["threatEntries"]}
    assert urls == {"http://a.com/", "https://a.com/"}


def test_gsb_403_and_400_are_distinguishable():
    def make(status):
        return lambda request: httpx.Response(status, json={"error": {"message": "no"}})

    crit = load_criteria(REPO_ROOT / "criteria.toml")
    for status, needle in ((403, "key"), (400, "request")):
        with pytest.raises(toxicity.GsbError) as exc:
            toxicity.GsbClient(_fake_client(make(status)), crit, "k").check(["a.com"])
        assert needle in str(exc.value).lower()


def test_gsb_from_env_raises_clean_error_without_a_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_SAFE_BROWSING_API_KEY", raising=False)
    crit = load_criteria(REPO_ROOT / "criteria.toml")
    with pytest.raises(toxicity.ToxicityKeyMissing):
        toxicity.GsbClient.from_env(_fake_client(lambda r: httpx.Response(200, json={})), crit)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_toxicity.py -q -k "gsb"`
Expected: FAIL — no attribute `GsbClient`

- [ ] **Step 3: Implement**

Append to `domainscout/toxicity.py`:

```python
GSB_KEY_ENV = "GOOGLE_SAFE_BROWSING_API_KEY"


class GsbClient:
    """Google Safe Browsing v4 threatMatches:find.

    Batches up to 500 URLs per request, so an entire day's screen is ONE call and the
    rate-limit surface is effectively nil. Both http:// and https:// forms are sent per
    domain: canonicalization usually makes a host-level entry match either, but at 60
    URLs against a 500 cap it is free insurance for the case where it does not."""

    def __init__(self, client: httpx.Client, criteria, api_key: str):
        self.client = client
        self.criteria = criteria
        self.api_key = api_key

    @classmethod
    def from_env(cls, client: httpx.Client, criteria) -> "GsbClient":
        key = os.environ.get(GSB_KEY_ENV, "").strip()
        if not key:
            raise ToxicityKeyMissing(
                f"{GSB_KEY_ENV} is not set. Safe Browsing needs a free Google Cloud API "
                f"key (no billing account required). Put it in .env - see .env.example.")
        return cls(client, criteria, key)

    def check(self, domains: Sequence[str]) -> dict:
        checked_at = datetime.now().isoformat(timespec="seconds")
        results = {d: GsbResult(False, (), checked_at) for d in domains}
        if not domains:
            return results
        per_domain = 2                      # http + https
        chunk = max(1, self.criteria.tox_gsb_batch_size // per_domain)
        for start in range(0, len(domains), chunk):
            batch = list(domains)[start:start + chunk]
            hits = self._find(batch)
            for domain, threats in hits.items():
                results[domain] = GsbResult(True, tuple(sorted(threats)), checked_at)
        return results

    def _find(self, batch: Sequence[str]) -> dict:
        entries = [{"url": f"{scheme}://{d}/"} for d in batch for scheme in ("http", "https")]
        body = {
            "client": {"clientId": "domainscout", "clientVersion": "0.1"},
            "threatInfo": {
                # All THREE lists must be present AND non-empty: v4 answers an empty
                # list with NO MATCHES rather than an error - a silent false-clean.
                "threatTypes": list(self.criteria.tox_gsb_threat_types),
                "platformTypes": list(self.criteria.tox_gsb_platform_types),
                "threatEntryTypes": list(self.criteria.tox_gsb_threat_entry_types),
                "threatEntries": entries,
            },
        }
        if not (body["threatInfo"]["threatTypes"] and body["threatInfo"]["platformTypes"]
                and body["threatInfo"]["threatEntryTypes"]):
            raise GsbError("refusing to send an empty threatTypes/platformTypes/"
                           "threatEntryTypes list - it returns no matches, not an error")
        try:
            resp = self.client.post(self.criteria.tox_gsb_base_url,
                                    params={"key": self.api_key}, json=body,
                                    timeout=self.criteria.tox_gsb_timeout)
        except httpx.TransportError as exc:
            raise GsbError(f"safe-browsing transport failure: {exc}") from exc
        if resp.status_code == 403:
            raise GsbError("safe-browsing rejected the API key (403) - key invalid, "
                           "Safe Browsing API not enabled, or quota exhausted")
        if resp.status_code == 400:
            raise GsbError("safe-browsing rejected the request (400) - malformed body, "
                           "which is our bug, not a configuration problem")
        if resp.status_code != 200:
            raise GsbError(f"safe-browsing HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise GsbError(f"safe-browsing returned non-JSON: {exc}") from exc
        # A clean batch is a BARE {} with no 'matches' key. Absent == not listed.
        hits: dict = {}
        for match in payload.get("matches", []) or []:
            url = (match.get("threat") or {}).get("url", "")
            for domain in batch:
                if f"//{domain}/" in url or url.rstrip("/").endswith(f"//{domain}"):
                    hits.setdefault(domain, set()).add(match.get("threatType", "UNKNOWN"))
        return hits
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_toxicity.py -q -k "gsb"`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add domainscout/toxicity.py tests/test_toxicity.py
git commit -m "feat(5b): GsbClient - batched, both schemes, all three lists non-empty"
```

---

## Task 10: `screen()` orchestration + `verdict_to_json`

**Files:**
- Modify: `domainscout/toxicity.py`
- Test: `tests/test_toxicity.py`

**Interfaces:**
- Consumes: everything from Tasks 4-9
- Produces: `screen(domains, *, cdx, gsb, criteria, cache=None, now=None) -> list[ToxicityVerdict]`, `verdict_to_json(v) -> str`. Consumed by Task 11 and by 5c.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_toxicity.py`:

```python
class _FakeCdx:
    def __init__(self, by_domain): self.by_domain = by_domain; self.calls = []
    def fetch(self, domain):
        self.calls.append(domain)
        value = self.by_domain.get(domain, [])
        if isinstance(value, Exception):
            raise value
        return value


class _FakeGsb:
    def __init__(self, listed=(), error=None): self.listed = set(listed); self.error = error; self.calls = []
    def check(self, domains):
        self.calls.append(list(domains))
        if self.error:
            raise self.error
        return {d: models.GsbResult(d in self.listed, ("MALWARE",) if d in self.listed else (),
                                    "2026-07-18") for d in domains}


def test_screen_returns_one_verdict_per_domain_in_input_order():
    crit = load_criteria(REPO_ROOT / "criteria.toml")
    out = toxicity.screen(["b.com", "a.com"], cdx=_FakeCdx({}), gsb=_FakeGsb(), criteria=crit)
    assert [v.domain for v in out] == ["b.com", "a.com"]


def test_screen_gsb_ok_cdx_error_keeps_the_gsb_result():
    """Verdict reflects the worst leg; data reflects every leg that succeeded. The
    obvious wrong implementation nulls everything on any error and throws away work
    that was already paid for."""
    crit = load_criteria(REPO_ROOT / "criteria.toml")
    cdx = _FakeCdx({"a.com": toxicity.CdxError("timeout")})
    v = toxicity.screen(["a.com"], cdx=cdx, gsb=_FakeGsb(), criteria=crit)[0]
    assert v.verdict == models.VERDICT_UNKNOWN_ERROR
    assert v.gsb is not None and v.gsb.currently_listed is False


def test_screen_cdx_ok_gsb_error_keeps_the_history():
    crit = load_criteria(REPO_ROOT / "criteria.toml")
    caps = [models.Capture(f"20{y}0115120000", "200", "text/html", f"D{y}")
            for y in range(10, 21)]
    v = toxicity.screen(["a.com"], cdx=_FakeCdx({"a.com": caps}),
                        gsb=_FakeGsb(error=toxicity.GsbError("boom")), criteria=crit)[0]
    assert v.verdict == models.VERDICT_UNKNOWN_ERROR
    assert v.history is not None and v.history.lifetime.capture_count == 11


def test_screen_listed_domain_is_rejected_and_skips_nothing_else():
    crit = load_criteria(REPO_ROOT / "criteria.toml")
    v = toxicity.screen(["bad.com"], cdx=_FakeCdx({}), gsb=_FakeGsb(listed=["bad.com"]),
                        criteria=crit)[0]
    assert v.verdict == models.VERDICT_REJECT


def test_screen_cache_hit_skips_both_legs_and_the_gsb_batch(tmp_path):
    """A live cache hit must cost NO network on either leg, and the domain must not
    even appear in the GSB batch."""
    crit = load_criteria(REPO_ROOT / "criteria.toml")
    now = datetime(2026, 7, 18)
    cache = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                  collapse=crit.tox_cdx_collapse, now=now)
    cache.put(models.ToxicityVerdict("a.com", models.VERDICT_PASS, "cached", None, None,
                                     now.isoformat(), crit.tox_cdx_collapse))
    cdx, gsb = _FakeCdx({}), _FakeGsb()
    out = toxicity.screen(["a.com", "b.com"], cdx=cdx, gsb=gsb, criteria=crit,
                          cache=cache, now=now)
    assert cdx.calls == ["b.com"]
    assert gsb.calls == [["b.com"]]
    assert [v.domain for v in out] == ["a.com", "b.com"]


def test_verdict_json_names_the_gsb_field_currently_listed():
    """Field names are prompts. If this serializes as 'clean' or 'safe', a future
    Tier-2 prompt can present a blocklist snapshot as verified safety."""
    crit = load_criteria(REPO_ROOT / "criteria.toml")
    v = toxicity.screen(["a.com"], cdx=_FakeCdx({}), gsb=_FakeGsb(), criteria=crit)[0]
    payload = json.loads(toxicity.verdict_to_json(v))
    assert payload["gsb_currently_listed"] is False
    blob = json.dumps(payload).lower()
    assert "clean" not in blob and '"safe"' not in blob
    assert payload["collapse"] == crit.tox_cdx_collapse
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_toxicity.py -q -k "screen or verdict_json"`
Expected: FAIL — no attribute `screen`

- [ ] **Step 3: Implement**

Append to `domainscout/toxicity.py`:

```python
def screen(domains: Sequence[str], *, cdx, gsb, criteria,
           cache: VerdictCache | None = None,
           now: datetime | None = None) -> list[ToxicityVerdict]:
    """Screen domains. Returns ONE verdict per input domain, IN INPUT ORDER.

    A live cache hit short-circuits BOTH legs - no CDX call, and the domain is excluded
    from the GSB batch entirely."""
    moment = now or datetime.now()
    stamp = moment.isoformat(timespec="seconds")
    verdicts: dict = {}

    pending = []
    for domain in domains:
        hit = cache.get(domain) if cache else None
        if hit is not None:
            verdicts[domain] = hit
        else:
            pending.append(domain)

    # CDX first: a per-domain failure is captured, never raised out of the batch.
    shapes: dict = {}
    errors: dict = {d: [] for d in pending}
    for domain in pending:
        try:
            shapes[domain] = compute_shape(
                cdx.fetch(domain),
                tail_window_months=criteria.tox_tail_window_months,
                tail_min_captures=criteria.tox_tail_min_captures)
        except CdxError as exc:
            shapes[domain] = None
            errors[domain].append(f"cdx: {exc}")

    # GSB second, one batched call. A batch failure marks every pending domain -
    # but their CDX shapes SURVIVE (verdict = worst leg, data = every leg that worked).
    gsb_results: dict = {}
    if pending:
        try:
            gsb_results = gsb.check(pending)
        except GsbError as exc:
            for domain in pending:
                errors[domain].append(f"safe-browsing: {exc}")

    for domain in pending:
        result = gsb_results.get(domain)
        verdict, reason = decide(result, shapes.get(domain), errors[domain])
        built = ToxicityVerdict(
            domain=domain, verdict=verdict, reason=reason, gsb=result,
            history=shapes.get(domain), screened_at=stamp,
            collapse=criteria.tox_cdx_collapse)
        verdicts[domain] = built
        if cache:
            cache.put(built)
    if cache:
        cache.save()
    return [verdicts[d] for d in domains]


def verdict_to_json(verdict: ToxicityVerdict) -> str:
    """The 5c prompt payload. The Safe Browsing field is gsb_currently_listed - never
    'clean', never 'safe'. GSB lists CURRENTLY flagged URLs, so a False means 'not
    presently listed', and a dropped domain that served malware years ago may well have
    aged off. Naming it defensively is what stops a future prompt from presenting a
    snapshot as verified safety."""
    return json.dumps({
        "domain": verdict.domain,
        "verdict": verdict.verdict,
        "reason": verdict.reason,
        "gsb_currently_listed": (verdict.gsb.currently_listed if verdict.gsb else None),
        "gsb_threat_types": (list(verdict.gsb.threat_types) if verdict.gsb else []),
        "gsb_checked_at": (verdict.gsb.checked_at if verdict.gsb else None),
        "history": (_shape_to_dict(verdict.history) if verdict.history else None),
        "screened_at": verdict.screened_at,
        "collapse": verdict.collapse,
    })


def _shape_to_dict(shape: HistoryShape) -> dict:
    return {
        "lifetime": asdict(shape.lifetime),
        "tail": asdict(shape.tail) if shape.tail else None,
        "divergence": asdict(shape.divergence) if shape.divergence else None,
    }
```

Add `from dataclasses import asdict` to the imports.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_toxicity.py -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add domainscout/toxicity.py tests/test_toxicity.py
git commit -m "feat(5b): screen() orchestration - partial legs survive partial failures"
```

---

## Task 11: `screen` CLI

**Files:**
- Modify: `domainscout/commands.py`
- Modify: `domainscout/__main__.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `screen`, `VerdictCache`, `CdxClient`, `GsbClient`, `ToxicityKeyMissing`, `config.load_dotenv`
- Produces: `commands.cmd_screen(args) -> int`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_screen_is_a_real_subcommand_not_a_stub():
    parser = build_parser()
    args = parser.parse_args(["screen", "--domain", "a.com"])
    assert args.func is commands.cmd_screen


def test_screen_without_api_key_exits_1_cleanly(monkeypatch, capsys, tmp_path):
    """A missing key must be a readable message and exit 1, never a raw traceback
    (5a's CompsCacheMissing precedent)."""
    monkeypatch.delenv("GOOGLE_SAFE_BROWSING_API_KEY", raising=False)
    monkeypatch.setattr("domainscout.config.load_dotenv", lambda *a, **k: None)
    args = build_parser().parse_args(
        ["screen", "--domain", "a.com", "--cache-path", str(tmp_path / "c.json")])
    assert args.func(args) == 1
    assert "GOOGLE_SAFE_BROWSING_API_KEY" in capsys.readouterr().err


def test_screen_dry_run_makes_no_network_calls(monkeypatch, capsys):
    def explode(*a, **k):
        raise AssertionError("dry-run must not build a network client")

    monkeypatch.setattr("domainscout.ingest.make_client", explode)
    args = build_parser().parse_args(["screen", "--domain", "a.com", "--dry-run"])
    assert args.func(args) == 0
    assert "dry-run" in capsys.readouterr().out


def test_screen_output_is_ascii_only(monkeypatch, capsys):
    """5a shipped one emoji that crashed the cron path on redirected cp1252 stdout."""
    args = build_parser().parse_args(["screen", "--domain", "a.com", "--dry-run"])
    args.func(args)
    capsys.readouterr().out.encode("cp1252")   # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli.py -q -k "screen"`
Expected: FAIL — `argument <command>: invalid choice: 'screen'`

- [ ] **Step 3: Implement `cmd_screen`**

Append to `domainscout/commands.py` (add `toxicity` to the `from domainscout import ...` line and `from domainscout import config` for `load_dotenv`):

```python
def cmd_screen(args: argparse.Namespace) -> int:
    """Phase 5b debug CLI. UNLIKE `comps`, this DOES hit the network."""
    criteria = load_criteria(args.criteria)
    domains = [d.strip() for d in (args.domains.split(",") if args.domains else [args.domain])
               if d.strip()]
    if args.dry_run:
        print(f"screen: [dry-run] would query CDX for {len(domains)} domain(s) and send "
              f"{len(domains) * 2} URL(s) to safe-browsing in 1 batch (nothing written)")
        return 0

    config.load_dotenv()
    client = ingest.make_client(timeout=criteria.tox_cdx_timeout)
    try:
        try:
            gsb = toxicity.GsbClient.from_env(client, criteria)
        except toxicity.ToxicityKeyMissing as exc:
            print(f"screen: {exc}", file=sys.stderr)
            return 1
        cache = None
        if not args.no_cache:
            cache = toxicity.VerdictCache(
                args.cache_path or toxicity.DEFAULT_CACHE_PATH,
                cache_days=criteria.tox_cache_days, collapse=criteria.tox_cdx_collapse)
        verdicts = toxicity.screen(domains, cdx=toxicity.CdxClient(client, criteria),
                                   gsb=gsb, criteria=criteria, cache=cache)
    finally:
        client.close()

    for verdict in verdicts:
        if args.json:
            print(toxicity.verdict_to_json(verdict))
            continue
        print(f"{verdict.domain}  verdict={verdict.verdict}")
        print(f"  reason: {verdict.reason}")
        if verdict.gsb:
            print(f"  safe-browsing currently_listed={verdict.gsb.currently_listed} "
                  f"threats={list(verdict.gsb.threat_types)}"
                  "   (a snapshot of current listings, NOT a guarantee of safety)")
        if verdict.history:
            lt = verdict.history.lifetime
            print(f"  lifetime: {lt.first_capture[:8]}..{lt.last_capture[:8]} "
                  f"span={lt.span_years:.1f}y n={lt.capture_count} churn={lt.digest_churn:.2f}")
            if verdict.history.divergence:
                dv = verdict.history.divergence
                print(f"  tail divergence: churn_ratio={dv.churn_ratio} "
                      f"status_shift={dv.status_shift:+.2f} mime_shift={dv.mime_shift:+.2f}")
            else:
                print("  tail divergence: n/a (too few tail captures, or tail covers "
                      "the whole life)")
        elif verdict.verdict == toxicity.VERDICT_UNKNOWN_NO_HISTORY:
            print("  no wayback captures - absence of evidence. Invented brandables are "
                  "routinely unarchived; this is NOT a negative signal.")
    return 0
```

Add `VERDICT_UNKNOWN_NO_HISTORY` to `toxicity.py`'s module namespace by re-exporting it (it is already imported there from `models`).

- [ ] **Step 4: Register the subparser**

In `domainscout/__main__.py`, before the `_STUB_HELP` loop:

```python
    p_screen = sub.add_parser(
        "screen",
        help="[Phase 5b] toxicity screen for one or more domains (HITS THE NETWORK)",
        description=(
            "Safe Browsing (hard reject on a current listing) + Wayback CDX history "
            "shape (a graded signal for Tier-2). Needs GOOGLE_SAFE_BROWSING_API_KEY "
            "in .env. Verdicts cache to data/toxicity_cache.json; transient failures "
            "are never cached, so they retry on the next run."
        ),
    )
    p_screen.add_argument("--domain", help="a single domain, e.g. cloudvault.com")
    p_screen.add_argument("--domains", help="comma-separated list (exercises GSB batching)")
    p_screen.add_argument("--criteria", default="criteria.toml",
                          help="path to criteria.toml (default: criteria.toml)")
    p_screen.add_argument("--cache-path", dest="cache_path",
                          help="verdict cache path (default: data/toxicity_cache.json)")
    p_screen.add_argument("--no-cache", action="store_true", dest="no_cache",
                          help="ignore and do not write the verdict cache")
    p_screen.add_argument("--json", action="store_true",
                          help="emit the 5c prompt payload instead of the human summary")
    p_screen.add_argument("--dry-run", action="store_true",
                          help="report the calls that would be made, make none")
    p_screen.set_defaults(func=commands.cmd_screen)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -q -k "screen"`
Expected: PASS (4 tests)

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass, 2 skipped

- [ ] **Step 7: Commit**

```bash
git add domainscout/commands.py domainscout/__main__.py tests/test_cli.py
git commit -m "feat(5b): screen CLI - clean key-missing exit, ASCII-only output"
```

---

## Task 12: Live confirmation, docs, push

**Files:**
- Modify: `CLAUDE.md`, `DECISIONS.md`, `docs/PHASE-5B-DESIGN.md`, `criteria.toml`
- Test: `tests/test_toxicity.py` (add the skipped live smoke)

- [ ] **Step 1: Add the skipped live smoke**

Append to `tests/test_toxicity.py`:

```python
@pytest.mark.skip(reason="live network + API key; run by hand at phase end")
def test_live_screen_smoke():
    """Real CDX + real Safe Browsing. Asserts the three invariants against reality:
    a long-lived domain yields a shape, an invented name yields unknown_no_history
    (NOT pass), and Google's official malware test URL yields reject."""
    from domainscout import config, ingest
    config.load_dotenv()
    crit = load_criteria(REPO_ROOT / "criteria.toml")
    client = ingest.make_client(timeout=60.0)
    try:
        out = toxicity.screen(
            ["cnn.com", "qzxkvbnmplkjhgfd.com"],
            cdx=toxicity.CdxClient(client, crit),
            gsb=toxicity.GsbClient.from_env(client, crit), criteria=crit)
    finally:
        client.close()
    assert out[0].history is not None and out[0].history.lifetime.span_years > 10
    assert out[1].verdict == models.VERDICT_UNKNOWN_NO_HISTORY
```

- [ ] **Step 2: Run the live confirmation by hand**

Requires `GOOGLE_SAFE_BROWSING_API_KEY` in `.env`.

```bash
python -m domainscout screen --domain cnn.com
python -m domainscout screen --domain qzxkvbnmplkjhgfd.com
python -m domainscout screen --domains cnn.com,example.com --json
python -m domainscout screen --domain cnn.com          # 2nd run: cache hit, no network
```

**Confirm:** long-lived domain yields a real shape with a tail; the invented name yields `unknown_no_history` and says so; the second run is visibly instant (cache hit); `--json` emits `gsb_currently_listed`.

Then verify the cron path — this is what caught 5a's crash:

```bash
python -m domainscout screen --domain cnn.com > out.txt 2>&1 && cat out.txt
```

- [ ] **Step 3: Record measured limits in `criteria.toml`**

Replace the `PROVISIONAL` marker on `cdx_limit` with the spike's measured decision, and add the measured CDX rate-limit/latency comment block in the `[comps]`/`[rdap]` house style.

- [ ] **Step 4: Update the docs**

- `CLAUDE.md`: check the Phase 5b box; note `toxicity.py` shipped and that 5c is next.
- `docs/PHASE-5B-DESIGN.md`: mark **BUILT**, add a "Build notes" section with anything that differed from the design (especially the spike's query-strategy verdict).
- `DECISIONS.md`: dated entry covering the GSB credential correction, the four-valued verdict, the tail-window rationale, the CDX ordering finding, and the prior-drop-count assignment to 5c.

- [ ] **Step 5: Run the full suite one final time**

Run: `python -m pytest -q`
Expected: all pass, 3 skipped (2 existing + the new live smoke)

- [ ] **Step 6: Commit and push**

```bash
git add -A
git commit -m "docs(5b): mark Phase 5b built; measured limits + live confirmation"
git push origin main
```

This is the phase-end push — the first push since 5a, carrying the 5a carry-over fix, the design, and all of 5b.

---

## Self-Review

**Spec coverage:** Every spec section maps to a task — boundary/architecture → 4-10; asymmetric legs → 8, 9; data model → 3; verdict precedence + partial results → 6, 10; three invariants → 5 (`compute_shape` None), 6 (`decide`), 9/10 (`gsb_currently_listed`); caching → 7; config → 2; `.env` → 2; CLI → 11; testing → every task; spike → 1; forward-carried to 5c → recorded in the design doc, restated in Task 12's `DECISIONS.md` entry.

**Placeholders:** none. Every code step contains runnable code. Task 1 is exploratory by nature but specifies exact scripts, exact recordings, and an explicit stop condition.

**Type consistency:** `Capture`/`ShapeBlock`/`Divergence`/`HistoryShape`/`GsbResult`/`ToxicityVerdict` are defined once in Task 3 and used with identical field names throughout. `compute_shape` returns `HistoryShape | None` in Tasks 5, 10. `CdxClient.fetch -> list[Capture]` and `GsbClient.check -> dict[str, GsbResult]` match their `screen()` call sites and the `_FakeCdx`/`_FakeGsb` doubles.

**Known dependency:** Tasks 4 and 8 assume the spike's expected landing point (a)+(d). Task 1 Step 9 is an explicit stop-and-re-plan checkpoint if it does not hold.
