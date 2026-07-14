# Phase 2 — Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the WhoisFreaks free GitHub feed into gated `candidates` rows on an idempotent daily run — download → hard-invariant gate (`.com` → `^[a-z]+$` → label length ≤ 12) → open-cycle upsert → per-run `ingest_log` counts — reusing the Phase-1 DB helpers, behind a shared source-adapter interface.

**Architecture:** A `FeedSource` protocol (`sources/base.py`) isolates source-specific format knowledge; `WhoisFreaksSource` is the real adapter and `DynadotSource` a `NotImplementedError` stub that locks the interface for a later "Phase 2b". `ingest.py` holds the pure `gate()`, the network-only `download()`, and orchestration (`ingest_file` → `ingest_source` → `run_ingest`). Network lives only in `download()`, which takes an **injected `httpx.Client`** so the test suite never hits the network. Feed location is **config** (`criteria.toml [sources.whoisfreaks]`), confirmed against the live repo.

**Tech Stack:** Python 3.11+ (dev on 3.14), `httpx` (first runtime dep), stdlib `sqlite3`/`re`/`tomllib`, `pytest`.

## Global Constraints

- **Python:** `requires-python = ">=3.11"`. Use only 3.11-safe syntax.
- **Runtime deps:** exactly one new dependency this phase — `httpx`. No others.
- **.com only, ever:** the gate is the hard invariant; only survivors land in the permanent DB.
- **Gate order (first failure wins, buckets mutually exclusive):** normalize (`strip().lower()`) → ends with `.com`? else `rejected_tld` → label (name without `.com`) matches `criteria.charset` (`^[a-z]+$`)? else `rejected_charset` → `len(label) <= criteria.ingest_max_length` (=12, derived)? else `rejected_length` → pass. **Length is measured on the LABEL.**
- **`lifecycle_status` is NOT written at ingestion** — it stays at its `'unknown'` DB default (RDAP owns it in Phase 4). Writing `'dropped'` from the filename would re-open the born-closed duplicate bug (TDD §5).
- **`feed_category`** is set from the feed (`expired` | `dropped`), never from lifecycle.
- **Idempotency:** every daily run is safe to re-run. Reuse Phase-1 `db.upsert_candidate` (open-cycle `ON CONFLICT`, `first_seen` insert-only, refreshes source/feed_category only) and `db.record_ingest` (keyed `(run_date, source, feed_file)`, overwrites counts) **unchanged**.
- **No real network in the test suite.** `download()` takes an injected `httpx.Client`; tests pass `httpx.MockTransport`.
- **Git cadence:** commit per task locally; **push only once, at phase end** (Task 10).
- **Confirmed feed facts (verified against the live repo 2026-07-14):**
  - Base URL: `https://raw.githubusercontent.com/WhoisFreaks/daily-expired-and-dropped-domains/main`
  - Filenames: `<YYYY-MM-DD>-free-expired-domains.csv` and `<YYYY-MM-DD>-free-dropped-domains.csv` (date-stamped, at repo root).
  - Format: **plain-text, single column of domain names, no header row, one name per line** (despite the `.csv` extension). ~40–45 % are `.com`.

## File structure

**Create:**
- `domainscout/sources/base.py` — `FeedFile` dataclass + `FeedSource` protocol (the shared interface).
- `domainscout/sources/whoisfreaks.py` — `WhoisFreaksSource` (real adapter).
- `domainscout/sources/dynadot.py` — `DynadotSource` (stub → `NotImplementedError`).
- `domainscout/ingest.py` — `gate()`, `download()`, `ingest_file()`, `ingest_source()`, `run_ingest()`, `build_source()`, `infer_feed_category()`, `ingest_local_file()`, `summary_line()`, `DEFAULT_FEEDS_DIR`, `SOURCE_FACTORIES`.
- `tests/fixtures/whoisfreaks-sample.csv` — parse/gate fixture with deliberate junk.
- `tests/test_gate.py`, `tests/test_sources.py`, `tests/test_ingest.py`.

**Modify:**
- `pyproject.toml` — `dependencies = ["httpx"]`.
- `domainscout/config.py` — add `WhoisFreaksConfig` + `Criteria.whoisfreaks` field + loader.
- `criteria.toml` — add `[sources.whoisfreaks]`.
- `domainscout/commands.py` — drop `ingest` from `STUB_PHASES`; add `cmd_ingest`.
- `domainscout/__main__.py` — drop `ingest` from `_STUB_HELP`; register a real `ingest` subparser.
- `tests/test_config.py`, `tests/test_cli.py` — extend / adjust.

**No change:** `domainscout/db.py`, `domainscout/models.py` (reused as-is). `.gitignore` already ignores `data/feeds/` via its existing `feeds/` rule.

---

### Task 1: Add `httpx` runtime dependency

**Files:**
- Modify: `pyproject.toml:11`

**Interfaces:**
- Produces: `httpx` importable at runtime; declared so `pip install -e .` pulls it.

- [ ] **Step 1: Declare the dependency**

In `pyproject.toml`, change line 11 from `dependencies = []` to:

```toml
dependencies = ["httpx"]
```

- [ ] **Step 2: Install it**

Run: `python -m pip install httpx`
Expected: installs `httpx` (and its deps `httpcore`, `h11`, `certifi`, `idna`, `sniffio`, `anyio`) or reports "Requirement already satisfied".

- [ ] **Step 3: Verify it imports**

Run: `python -c "import httpx; print(httpx.__version__)"`
Expected: prints a version string (e.g. `0.27.x` or newer), exit 0.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build: add httpx as first runtime dependency (Phase 2)"
```

---

### Task 2: `[sources.whoisfreaks]` feed configuration

**Files:**
- Modify: `domainscout/config.py`
- Modify: `criteria.toml`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: existing `Criteria`, `ConfigError`, `load_criteria`, `_require` in `config.py`.
- Produces:
  - `class WhoisFreaksConfig` — frozen dataclass, fields `base_url: str`, `expired_filename: str`, `dropped_filename: str`.
  - `Criteria.whoisfreaks: WhoisFreaksConfig | None` (default `None` when the section is absent).

- [ ] **Step 1: Write the failing config tests**

Add to `tests/test_config.py` (after the existing `WF`-less tests). First add this constant near `VALID_TOML`:

```python
WF_SECTION = """
[sources.whoisfreaks]
base_url = "https://raw.githubusercontent.com/WhoisFreaks/daily-expired-and-dropped-domains/main"
expired_filename = "{date}-free-expired-domains.csv"
dropped_filename = "{date}-free-dropped-domains.csv"
"""
```

Then add these tests:

```python
def test_whoisfreaks_config_absent_is_none(tmp_path):
    crit = load_criteria(_write(tmp_path, VALID_TOML))
    assert crit.whoisfreaks is None


def test_whoisfreaks_config_loads(tmp_path):
    crit = load_criteria(_write(tmp_path, VALID_TOML + WF_SECTION))
    assert crit.whoisfreaks is not None
    assert crit.whoisfreaks.base_url.endswith("/main")
    assert crit.whoisfreaks.expired_filename == "{date}-free-expired-domains.csv"
    assert crit.whoisfreaks.dropped_filename == "{date}-free-dropped-domains.csv"


def test_whoisfreaks_missing_key_raises(tmp_path):
    bad = VALID_TOML + WF_SECTION.replace(
        'expired_filename = "{date}-free-expired-domains.csv"\n', ""
    )
    with pytest.raises(ConfigError, match="expired_filename"):
        load_criteria(_write(tmp_path, bad))
```

Also extend the existing `test_repo_criteria_toml_is_valid` with:

```python
    assert crit.whoisfreaks is not None
    assert "WhoisFreaks/daily-expired-and-dropped-domains" in crit.whoisfreaks.base_url
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_config.py -v`
Expected: the 3 new tests plus the extended repo test FAIL (`Criteria` has no attribute `whoisfreaks`).

- [ ] **Step 3: Implement `WhoisFreaksConfig` + loader**

In `domainscout/config.py`, add the dataclass **above** `Criteria`:

```python
@dataclass(frozen=True)
class WhoisFreaksConfig:
    base_url: str
    expired_filename: str  # template containing "{date}"
    dropped_filename: str  # template containing "{date}"
```

Add the field as the **last** field of `Criteria` (defaults must come last):

```python
    retention_days: int
    whoisfreaks: WhoisFreaksConfig | None = None
```

In `load_criteria`, build it just before the `return Criteria(...)` and pass it in. Insert this block after the `sources` validation:

```python
    whoisfreaks = None
    sources_tbl = data.get("sources")
    if isinstance(sources_tbl, dict) and "whoisfreaks" in sources_tbl:
        wf = sources_tbl["whoisfreaks"]
        if not isinstance(wf, dict):
            raise ConfigError("criteria.toml: [sources.whoisfreaks] must be a table")
        for key in ("base_url", "expired_filename", "dropped_filename"):
            if key not in wf:
                raise ConfigError(
                    f"criteria.toml: missing '{key}' in [sources.whoisfreaks]"
                )
        whoisfreaks = WhoisFreaksConfig(
            base_url=str(wf["base_url"]),
            expired_filename=str(wf["expired_filename"]),
            dropped_filename=str(wf["dropped_filename"]),
        )
```

Then add `whoisfreaks=whoisfreaks,` to the `return Criteria(...)` call (last argument).

- [ ] **Step 4: Add the section to the shipped config**

Append to `criteria.toml`:

```toml

[sources.whoisfreaks]                # feed location (confirmed against the live repo 2026-07-14)
base_url = "https://raw.githubusercontent.com/WhoisFreaks/daily-expired-and-dropped-domains/main"
expired_filename = "{date}-free-expired-domains.csv"   # {date} -> YYYY-MM-DD
dropped_filename = "{date}-free-dropped-domains.csv"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_config.py -v`
Expected: all config tests PASS (existing + 3 new + extended repo test).

- [ ] **Step 6: Commit**

```bash
git add domainscout/config.py criteria.toml tests/test_config.py
git commit -m "feat: load [sources.whoisfreaks] feed config into Criteria"
```

---

### Task 3: Source interface (`sources/base.py`) + Dynadot stub

**Files:**
- Create: `domainscout/sources/base.py`
- Create: `domainscout/sources/dynadot.py`
- Test: `tests/test_sources.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) FeedFile(source: str, feed_category: str, remote_url: str, local_name: str)`.
  - `FeedSource` protocol: attribute `name: str`; `feed_files(run_date: date) -> list[FeedFile]`; `iter_domains(path: Path) -> Iterator[str]`.
  - `class DynadotSource` with `name = "dynadot"`, classmethod `from_criteria(criteria) -> DynadotSource`, and `feed_files`/`iter_domains` raising `NotImplementedError("Dynadot ingestion is Phase 2b")`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sources.py`:

```python
from datetime import date
from pathlib import Path

import pytest

from domainscout.sources.base import FeedFile
from domainscout.sources.dynadot import DynadotSource


def test_feedfile_is_frozen_with_expected_fields():
    ff = FeedFile(source="whoisfreaks", feed_category="expired",
                  remote_url="https://h/x.csv", local_name="x.csv")
    assert (ff.source, ff.feed_category, ff.remote_url, ff.local_name) == (
        "whoisfreaks", "expired", "https://h/x.csv", "x.csv")
    with pytest.raises(Exception):
        ff.source = "other"  # frozen


def test_dynadot_stub_raises_phase_2b():
    src = DynadotSource.from_criteria(criteria=None)
    assert src.name == "dynadot"
    with pytest.raises(NotImplementedError, match="Phase 2b"):
        src.feed_files(date(2026, 7, 13))
    with pytest.raises(NotImplementedError, match="Phase 2b"):
        list(src.iter_domains(Path("nope.csv")))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_sources.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'domainscout.sources.base'`.

- [ ] **Step 3: Implement `base.py`**

Create `domainscout/sources/base.py`:

```python
"""Shared feed-source interface: isolates source-specific format knowledge
(URLs, file parsing) from the generic ingestion orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable


@dataclass(frozen=True)
class FeedFile:
    """One downloadable feed file for a given run date."""

    source: str
    feed_category: str  # 'expired' | 'dropped'
    remote_url: str
    local_name: str


@runtime_checkable
class FeedSource(Protocol):
    """A data source. All source-specific format knowledge lives in the adapter."""

    name: str

    def feed_files(self, run_date: date) -> list[FeedFile]:
        """Which files to pull for run_date."""
        ...

    def iter_domains(self, path: Path) -> Iterator[str]:
        """Parse a local feed file into raw (un-gated) domain strings."""
        ...
```

- [ ] **Step 4: Implement `dynadot.py`**

Create `domainscout/sources/dynadot.py`:

```python
"""Dynadot expired-auction adapter — interface stub only.

Locks the FeedSource contract so a second source drops in later. Real wiring
(auction CSV: prices, auction-end dates) is deferred to a Phase 2b spec."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Iterator

from domainscout.sources.base import FeedFile

_MSG = "Dynadot ingestion is Phase 2b"


class DynadotSource:
    name = "dynadot"

    @classmethod
    def from_criteria(cls, criteria) -> "DynadotSource":
        return cls()

    def feed_files(self, run_date: date) -> list[FeedFile]:
        raise NotImplementedError(_MSG)

    def iter_domains(self, path: Path) -> Iterator[str]:
        raise NotImplementedError(_MSG)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_sources.py -v`
Expected: both tests PASS.

- [ ] **Step 6: Commit**

```bash
git add domainscout/sources/base.py domainscout/sources/dynadot.py tests/test_sources.py
git commit -m "feat: add FeedSource interface + Dynadot stub adapter"
```

---

### Task 4: WhoisFreaks adapter + test fixture

**Files:**
- Create: `domainscout/sources/whoisfreaks.py`
- Create: `tests/fixtures/whoisfreaks-sample.csv`
- Test: `tests/test_sources.py` (extend)

**Interfaces:**
- Consumes: `Criteria`, `ConfigError`, `WhoisFreaksConfig` from `config.py`; `FeedFile` from `sources/base.py`.
- Produces: `class WhoisFreaksSource`:
  - `name = "whoisfreaks"`
  - `__init__(self, base_url: str, expired_filename: str, dropped_filename: str)`
  - classmethod `from_criteria(criteria: Criteria) -> WhoisFreaksSource` (raises `ConfigError` if `criteria.whoisfreaks is None`)
  - `feed_files(run_date) -> [FeedFile(expired), FeedFile(dropped)]`
  - `iter_domains(path) -> Iterator[str]` yielding every non-blank line, stripped (raw, **un-gated**).

- [ ] **Step 1: Create the fixture**

Create `tests/fixtures/whoisfreaks-sample.csv` with EXACTLY these 13 lines (line 10 is blank), one name per line:

```
armorbeef.net
zebuervamate.com
apple.com
GOOGLE.COM
converse.com
bar-baz.com
abc123.com
toolongdomain.com
short.com

nickel.com
sub.domain.com
example.org
```

Gate outcomes (for reference in later tasks — label = name minus `.com`, ceiling 12):
`armorbeef.net`→tld · `zebuervamate.com`→land(len12) · `apple.com`→land · `GOOGLE.COM`→land(normalized) · `converse.com`→land(len8) · `bar-baz.com`→charset · `abc123.com`→charset · `toolongdomain.com`→length(len13) · `short.com`→land · (blank)→skipped · `nickel.com`→land · `sub.domain.com`→charset · `example.org`→tld.
Totals: **seen 12, rejected_tld 2, rejected_charset 3, rejected_length 1, landed 6.**

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_sources.py`:

```python
from domainscout.config import ConfigError, WhoisFreaksConfig
from domainscout.sources.whoisfreaks import WhoisFreaksSource

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "whoisfreaks-sample.csv"


def _wf():
    return WhoisFreaksSource(
        base_url="https://host/repo/main",
        expired_filename="{date}-free-expired-domains.csv",
        dropped_filename="{date}-free-dropped-domains.csv",
    )


def test_whoisfreaks_feed_files_builds_expired_and_dropped():
    files = _wf().feed_files(date(2026, 7, 13))
    assert [f.feed_category for f in files] == ["expired", "dropped"]
    assert files[0].local_name == "2026-07-13-free-expired-domains.csv"
    assert files[0].remote_url == "https://host/repo/main/2026-07-13-free-expired-domains.csv"
    assert files[1].local_name == "2026-07-13-free-dropped-domains.csv"
    assert all(f.source == "whoisfreaks" for f in files)


def test_whoisfreaks_iter_domains_yields_raw_names_skipping_blanks():
    names = list(_wf().iter_domains(FIXTURE))
    assert names == [
        "armorbeef.net", "zebuervamate.com", "apple.com", "GOOGLE.COM",
        "converse.com", "bar-baz.com", "abc123.com", "toolongdomain.com",
        "short.com", "nickel.com", "sub.domain.com", "example.org",
    ]  # 12 raw names, un-normalized, blank line dropped


def test_whoisfreaks_from_criteria_requires_config():
    with pytest.raises(ConfigError, match="whoisfreaks"):
        WhoisFreaksSource.from_criteria(_CriteriaStub(whoisfreaks=None))


class _CriteriaStub:
    def __init__(self, whoisfreaks):
        self.whoisfreaks = whoisfreaks


def test_whoisfreaks_from_criteria_builds_from_config():
    cfg = WhoisFreaksConfig(
        base_url="https://host/repo/main",
        expired_filename="{date}-free-expired-domains.csv",
        dropped_filename="{date}-free-dropped-domains.csv",
    )
    src = WhoisFreaksSource.from_criteria(_CriteriaStub(whoisfreaks=cfg))
    assert src.name == "whoisfreaks"
    assert src.feed_files(date(2026, 7, 13))[0].remote_url.endswith(
        "/2026-07-13-free-expired-domains.csv")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_sources.py -v`
Expected: the new tests FAIL — `ModuleNotFoundError: No module named 'domainscout.sources.whoisfreaks'`.

- [ ] **Step 4: Implement the adapter**

Create `domainscout/sources/whoisfreaks.py`:

```python
"""WhoisFreaks free-feed adapter.

The feed is a date-stamped, newline-delimited list of domain NAMES (no header,
one per line) despite the .csv extension. Lifecycle comes from RDAP (Phase 4);
this adapter only yields raw names — the gate does the rejecting."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Iterator

from domainscout.config import ConfigError, Criteria
from domainscout.sources.base import FeedFile


class WhoisFreaksSource:
    name = "whoisfreaks"

    def __init__(self, base_url: str, expired_filename: str, dropped_filename: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._expired_filename = expired_filename
        self._dropped_filename = dropped_filename

    @classmethod
    def from_criteria(cls, criteria: Criteria) -> "WhoisFreaksSource":
        cfg = criteria.whoisfreaks
        if cfg is None:
            raise ConfigError(
                "criteria.toml: [sources.whoisfreaks] is required to run the "
                "whoisfreaks source"
            )
        return cls(cfg.base_url, cfg.expired_filename, cfg.dropped_filename)

    def feed_files(self, run_date: date) -> list[FeedFile]:
        stamp = run_date.isoformat()
        return [
            self._feed_file("expired", self._expired_filename, stamp),
            self._feed_file("dropped", self._dropped_filename, stamp),
        ]

    def _feed_file(self, category: str, template: str, stamp: str) -> FeedFile:
        name = template.format(date=stamp)
        return FeedFile(
            source=self.name,
            feed_category=category,
            remote_url=f"{self._base_url}/{name}",
            local_name=name,
        )

    def iter_domains(self, path: Path) -> Iterator[str]:
        with Path(path).open("r", encoding="utf-8") as fh:
            for line in fh:
                name = line.strip()
                if name:
                    yield name
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_sources.py -v`
Expected: all `test_sources.py` tests PASS.

- [ ] **Step 6: Commit**

```bash
git add domainscout/sources/whoisfreaks.py tests/fixtures/whoisfreaks-sample.csv tests/test_sources.py
git commit -m "feat: add WhoisFreaks feed adapter + parse fixture"
```

---

### Task 5: The pure `gate()`

**Files:**
- Create: `domainscout/ingest.py`
- Test: `tests/test_gate.py`

**Interfaces:**
- Consumes: `Criteria` (uses `criteria.charset` and `criteria.ingest_max_length`).
- Produces: `gate(domain: str, criteria: Criteria) -> tuple[bool, str | None]`. Returns `(True, None)` on pass, else `(False, reason)` where reason ∈ `{"rejected_tld", "rejected_charset", "rejected_length"}` (these strings match `IngestCounts` field names).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gate.py`:

```python
from pathlib import Path

from domainscout.config import load_criteria
from domainscout.ingest import gate

CRIT = load_criteria(Path(__file__).resolve().parents[1] / "criteria.toml")


def test_gate_passes_plain_com():
    assert gate("apple.com", CRIT) == (True, None)


def test_gate_normalizes_case_and_whitespace():
    assert gate("  GOOGLE.COM \n", CRIT) == (True, None)


def test_gate_rejects_non_com_tld():
    assert gate("armorbeef.net", CRIT) == (False, "rejected_tld")
    assert gate("example.org", CRIT) == (False, "rejected_tld")


def test_gate_rejects_hyphen_digit_dot_in_label():
    assert gate("bar-baz.com", CRIT) == (False, "rejected_charset")
    assert gate("abc123.com", CRIT) == (False, "rejected_charset")
    assert gate("sub.domain.com", CRIT) == (False, "rejected_charset")


def test_gate_rejects_empty_label():
    assert gate(".com", CRIT) == (False, "rejected_charset")


def test_gate_length_boundaries():
    assert gate("converse.com", CRIT) == (True, None)       # label len 8
    assert gate("zebuervamate.com", CRIT) == (True, None)   # label len 12 (ceiling)
    assert gate("toolongdomain.com", CRIT) == (False, "rejected_length")  # label len 13


def test_gate_first_failure_wins():
    # non-.com AND bad charset -> tld reported first
    assert gate("bad_label.net", CRIT) == (False, "rejected_tld")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_gate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'domainscout.ingest'`.

- [ ] **Step 3: Implement `gate()`**

Create `domainscout/ingest.py`:

```python
"""Ingestion: pure gate + network download + orchestration.

Only survivors of the hard-invariant gate land in the permanent DB. Network
lives solely in download(); the httpx.Client is injected so tests never hit it."""

from __future__ import annotations

import re

from domainscout.config import Criteria

DEFAULT_FEEDS_DIR = "data/feeds"


def gate(domain: str, criteria: Criteria) -> tuple[bool, str | None]:
    """Apply the hard invariant. First failure wins; buckets are mutually
    exclusive. Length is measured on the label (name without the .com suffix)."""
    name = domain.strip().lower()
    if not name.endswith(".com"):
        return (False, "rejected_tld")
    label = name[:-4]  # strip ".com"
    if not re.match(criteria.charset, label):
        return (False, "rejected_charset")
    if len(label) > criteria.ingest_max_length:
        return (False, "rejected_length")
    return (True, None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_gate.py -v`
Expected: all gate tests PASS.

- [ ] **Step 5: Commit**

```bash
git add domainscout/ingest.py tests/test_gate.py
git commit -m "feat: add pure ingestion gate (.com -> charset -> length)"
```

---

### Task 6: `download()` — the only network code

**Files:**
- Modify: `domainscout/ingest.py`
- Test: `tests/test_ingest.py`

**Interfaces:**
- Consumes: `FeedFile` from `sources/base.py`; injected `httpx.Client`.
- Produces: `download(feed_file: FeedFile, feeds_dir: str | Path, client: httpx.Client) -> Path`. Writes `feeds_dir/local_name` and returns it; **skips the GET if the file already exists** (idempotent + this file is the retained copy). Propagates `httpx.HTTPStatusError` on non-2xx.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ingest.py`:

```python
from pathlib import Path

import httpx
import pytest

from domainscout import ingest
from domainscout.sources.base import FeedFile


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_download_writes_file_and_returns_path(tmp_path):
    ff = FeedFile(source="whoisfreaks", feed_category="expired",
                  remote_url="https://host/x.csv", local_name="x.csv")
    client = _client(lambda req: httpx.Response(200, content=b"apple.com\n"))
    dest = ingest.download(ff, tmp_path / "feeds", client)
    assert dest == tmp_path / "feeds" / "x.csv"
    assert dest.read_bytes() == b"apple.com\n"


def test_download_skips_when_file_exists(tmp_path):
    ff = FeedFile(source="whoisfreaks", feed_category="expired",
                  remote_url="https://host/x.csv", local_name="x.csv")
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, content=b"apple.com\n")

    client = _client(handler)
    ingest.download(ff, tmp_path / "feeds", client)
    ingest.download(ff, tmp_path / "feeds", client)  # second call: file present
    assert calls["n"] == 1  # network hit only once


def test_download_raises_on_404(tmp_path):
    ff = FeedFile(source="whoisfreaks", feed_category="expired",
                  remote_url="https://host/missing.csv", local_name="missing.csv")
    client = _client(lambda req: httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        ingest.download(ff, tmp_path / "feeds", client)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ingest.py -v`
Expected: FAIL — `AttributeError: module 'domainscout.ingest' has no attribute 'download'`.

- [ ] **Step 3: Implement `download()`**

In `domainscout/ingest.py`, add `import httpx` and `from pathlib import Path` to the imports, add `from domainscout.sources.base import FeedFile`, then append:

```python
def download(feed_file: FeedFile, feeds_dir: str | Path, client: httpx.Client) -> Path:
    """GET the feed file to feeds_dir/local_name; skip if it already exists."""
    dest = Path(feeds_dir) / feed_file.local_name
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = client.get(feed_file.remote_url)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest
```

Resulting import block at the top of `ingest.py`:

```python
from __future__ import annotations

import re
from pathlib import Path

import httpx

from domainscout.config import Criteria
from domainscout.sources.base import FeedFile
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_ingest.py -v`
Expected: the 3 download tests PASS.

- [ ] **Step 5: Commit**

```bash
git add domainscout/ingest.py tests/test_ingest.py
git commit -m "feat: add injected-client feed download (skip-if-exists)"
```

---

### Task 7: `ingest_file()` — gate → upsert → log over one local file

**Files:**
- Modify: `domainscout/ingest.py`
- Test: `tests/test_ingest.py` (extend)

**Interfaces:**
- Consumes: `gate()`; `db.upsert_candidate`, `db.record_ingest` (Phase 1, unchanged); `Candidate`, `IngestCounts` (models); a `FeedSource` (for `iter_domains`).
- Produces:
  `ingest_file(conn, source, *, path: Path, feed_category: str, feed_file_name: str, run_date: date, criteria: Criteria, dry_run: bool = False) -> IngestCounts`.
  Tallies seen/rejected_*/landed; upserts each survivor as `Candidate(domain, source=source.name, feed_category=feed_category)` (leaving `lifecycle_status` at its `'unknown'` default); calls `record_ingest`. When `dry_run`, writes nothing (no upsert, no log) but still returns the tally.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_ingest.py`:

```python
from datetime import date

from domainscout import db
from domainscout.config import load_criteria
from domainscout.sources.whoisfreaks import WhoisFreaksSource

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "whoisfreaks-sample.csv"
CRIT = load_criteria(REPO_ROOT / "criteria.toml")
LANDED = {"zebuervamate.com", "apple.com", "google.com",
          "converse.com", "short.com", "nickel.com"}


def _conn(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    return db.connect(dbp)


def _source():
    return WhoisFreaksSource.from_criteria(CRIT)


def test_ingest_file_counts_and_lands_survivors(tmp_path):
    conn = _conn(tmp_path)
    counts = ingest.ingest_file(
        conn, _source(), path=FIXTURE, feed_category="expired",
        feed_file_name="whoisfreaks-sample.csv", run_date=date(2026, 7, 13),
        criteria=CRIT,
    )
    assert (counts.seen, counts.rejected_tld, counts.rejected_charset,
            counts.rejected_length, counts.landed) == (12, 2, 3, 1, 6)
    rows = {r["domain"] for r in conn.execute("SELECT domain FROM candidates")}
    assert rows == LANDED


def test_ingest_file_sets_category_leaves_lifecycle_unknown(tmp_path):
    conn = _conn(tmp_path)
    ingest.ingest_file(
        conn, _source(), path=FIXTURE, feed_category="dropped",
        feed_file_name="f.csv", run_date=date(2026, 7, 13), criteria=CRIT,
    )
    row = conn.execute(
        "SELECT feed_category, lifecycle_status, source FROM candidates "
        "WHERE domain='apple.com'"
    ).fetchone()
    assert row["feed_category"] == "dropped"
    assert row["lifecycle_status"] == "unknown"
    assert row["source"] == "whoisfreaks"


def test_ingest_file_writes_ingest_log(tmp_path):
    conn = _conn(tmp_path)
    ingest.ingest_file(
        conn, _source(), path=FIXTURE, feed_category="expired",
        feed_file_name="f.csv", run_date=date(2026, 7, 13), criteria=CRIT,
    )
    log = conn.execute("SELECT * FROM ingest_log").fetchone()
    assert log["seen"] == 12 and log["landed"] == 6
    assert log["source"] == "whoisfreaks" and log["feed_file"] == "f.csv"


def test_ingest_file_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    kw = dict(path=FIXTURE, feed_category="expired", feed_file_name="f.csv",
              run_date=date(2026, 7, 13), criteria=CRIT)
    ingest.ingest_file(conn, _source(), **kw)
    first_seen = conn.execute(
        "SELECT first_seen FROM candidates WHERE domain='apple.com'").fetchone()[0]
    ingest.ingest_file(conn, _source(), **kw)  # re-run
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM ingest_log").fetchone()[0] == 1
    again = conn.execute(
        "SELECT first_seen FROM candidates WHERE domain='apple.com'").fetchone()[0]
    assert again == first_seen  # first_seen preserved


def test_ingest_file_dry_run_writes_nothing(tmp_path):
    conn = _conn(tmp_path)
    counts = ingest.ingest_file(
        conn, _source(), path=FIXTURE, feed_category="expired",
        feed_file_name="f.csv", run_date=date(2026, 7, 13), criteria=CRIT,
        dry_run=True,
    )
    assert counts.landed == 6  # still tallied
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM ingest_log").fetchone()[0] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ingest.py -k ingest_file -v`
Expected: FAIL — `AttributeError: module 'domainscout.ingest' has no attribute 'ingest_file'`.

- [ ] **Step 3: Implement `ingest_file()`**

In `domainscout/ingest.py`, extend imports and append the function. Add to the import block:

```python
from datetime import date

from domainscout import db
from domainscout.models import Candidate, IngestCounts
from domainscout.sources.base import FeedFile, FeedSource
```

(Replace the earlier `from domainscout.sources.base import FeedFile` line with the combined import above.)

Append:

```python
def ingest_file(
    conn,
    source: FeedSource,
    *,
    path: Path,
    feed_category: str,
    feed_file_name: str,
    run_date: date,
    criteria: Criteria,
    dry_run: bool = False,
) -> IngestCounts:
    """Gate every name in one local feed file; upsert survivors and log counts."""
    counts = IngestCounts(source=source.name, feed_file=feed_file_name, run_date=run_date)
    for raw in source.iter_domains(Path(path)):
        counts.seen += 1
        ok, reason = gate(raw, criteria)
        if ok:
            counts.landed += 1
            if not dry_run:
                db.upsert_candidate(
                    conn,
                    Candidate(
                        domain=raw.strip().lower(),
                        source=source.name,
                        feed_category=feed_category,
                    ),
                )
        elif reason == "rejected_tld":
            counts.rejected_tld += 1
        elif reason == "rejected_charset":
            counts.rejected_charset += 1
        else:  # "rejected_length"
            counts.rejected_length += 1
    if not dry_run:
        db.record_ingest(conn, counts)
    return counts
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_ingest.py -k ingest_file -v`
Expected: all 5 `ingest_file` tests PASS.

- [ ] **Step 5: Commit**

```bash
git add domainscout/ingest.py tests/test_ingest.py
git commit -m "feat: ingest_file gate->upsert->log with dry-run + idempotency"
```

---

### Task 8: Orchestration — `ingest_source`, `run_ingest`, helpers

**Files:**
- Modify: `domainscout/ingest.py`
- Test: `tests/test_ingest.py` (extend)

**Interfaces:**
- Consumes: `download`, `ingest_file`, `SOURCE_FACTORIES`.
- Produces:
  - `SOURCE_FACTORIES: dict[str, Callable[[Criteria], FeedSource]]` = `{"whoisfreaks": WhoisFreaksSource.from_criteria, "dynadot": DynadotSource.from_criteria}`.
  - `build_source(name: str, criteria: Criteria) -> FeedSource` (raises `ValueError` for unknown name).
  - `infer_feed_category(filename: str) -> str | None` (`"expired"`/`"dropped"`/`None`).
  - `ingest_source(conn, source, run_date, criteria, feeds_dir, client, *, dry_run=False) -> list[IngestCounts]` — download+ingest each `FeedFile`; a 404 is a **warning + skip** (not a crash).
  - `ingest_local_file(conn, *, path, criteria, run_date, source_name="whoisfreaks", feed_category=None, dry_run=False) -> IngestCounts` — for the `--file` path; infers category from filename when not given (raises `ValueError` if it can't).
  - `run_ingest(conn, *, criteria, run_date, source_names, feeds_dir, client, dry_run=False) -> list[IngestCounts]` — loop sources; a source that raises `NotImplementedError` (the Dynadot stub) is **skipped with a printed notice**.
  - `summary_line(counts: IngestCounts) -> str`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_ingest.py`:

```python
def test_infer_feed_category():
    assert ingest.infer_feed_category("2026-07-13-free-expired-domains.csv") == "expired"
    assert ingest.infer_feed_category("2026-07-13-free-dropped-domains.csv") == "dropped"
    assert ingest.infer_feed_category("mystery.csv") is None


def test_build_source_unknown_raises():
    with pytest.raises(ValueError, match="unknown source"):
        ingest.build_source("nope", CRIT)


def test_ingest_source_downloads_and_ingests_both_files(tmp_path):
    conn = _conn(tmp_path)
    body = FIXTURE.read_bytes()
    client = _client(lambda req: httpx.Response(200, content=body))
    results = ingest.ingest_source(
        conn, _source(), date(2026, 7, 13), CRIT, tmp_path / "feeds", client)
    assert [c.feed_file for c in results] == [
        "2026-07-13-free-expired-domains.csv",
        "2026-07-13-free-dropped-domains.csv",
    ]
    # both files carry the same fixture -> same 6 domains collapse to 6 open rows
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM ingest_log").fetchone()[0] == 2


def test_ingest_source_skips_404(tmp_path):
    conn = _conn(tmp_path)
    client = _client(lambda req: httpx.Response(404))
    results = ingest.ingest_source(
        conn, _source(), date(2026, 7, 13), CRIT, tmp_path / "feeds", client)
    assert results == []  # both 404 -> skipped, no crash
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0


def test_ingest_local_file_infers_category(tmp_path):
    conn = _conn(tmp_path)
    named = tmp_path / "2026-07-13-free-dropped-domains.csv"
    named.write_bytes(FIXTURE.read_bytes())
    counts = ingest.ingest_local_file(
        conn, path=named, criteria=CRIT, run_date=date(2026, 7, 13))
    assert counts.landed == 6
    row = conn.execute(
        "SELECT feed_category FROM candidates WHERE domain='apple.com'").fetchone()
    assert row["feed_category"] == "dropped"


def test_ingest_local_file_unknown_category_raises(tmp_path):
    conn = _conn(tmp_path)
    mystery = tmp_path / "mystery.csv"
    mystery.write_bytes(FIXTURE.read_bytes())
    with pytest.raises(ValueError, match="feed.category"):
        ingest.ingest_local_file(
            conn, path=mystery, criteria=CRIT, run_date=date(2026, 7, 13))


def test_run_ingest_skips_dynadot_stub_with_notice(tmp_path, capsys):
    conn = _conn(tmp_path)
    body = FIXTURE.read_bytes()
    client = _client(lambda req: httpx.Response(200, content=body))
    results = ingest.run_ingest(
        conn, criteria=CRIT, run_date=date(2026, 7, 13),
        source_names=["whoisfreaks", "dynadot"], feeds_dir=tmp_path / "feeds",
        client=client)
    out = capsys.readouterr().out.lower()
    assert "dynadot" in out and "phase 2b" in out
    assert len(results) == 2  # only whoisfreaks' two files


def test_summary_line_mentions_landed():
    from domainscout.models import IngestCounts
    line = ingest.summary_line(IngestCounts(
        source="whoisfreaks", feed_file="f.csv", seen=12,
        rejected_tld=2, rejected_charset=3, rejected_length=1, landed=6))
    assert "landed=6" in line and "whoisfreaks" in line
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ingest.py -k "source or run_ingest or local_file or infer or build_source or summary" -v`
Expected: FAIL — the new attributes don't exist yet.

- [ ] **Step 3: Implement the orchestration**

In `domainscout/ingest.py`, add to imports:

```python
from typing import Callable

from domainscout.sources.dynadot import DynadotSource
from domainscout.sources.whoisfreaks import WhoisFreaksSource
```

Add the registry near `DEFAULT_FEEDS_DIR`:

```python
SOURCE_FACTORIES: "dict[str, Callable[[Criteria], FeedSource]]" = {
    "whoisfreaks": WhoisFreaksSource.from_criteria,
    "dynadot": DynadotSource.from_criteria,
}
```

Append the functions:

```python
def build_source(name: str, criteria: Criteria) -> FeedSource:
    try:
        factory = SOURCE_FACTORIES[name]
    except KeyError:
        raise ValueError(f"unknown source: {name!r}") from None
    return factory(criteria)


def infer_feed_category(filename: str) -> str | None:
    low = filename.lower()
    if "expired" in low:
        return "expired"
    if "dropped" in low:
        return "dropped"
    return None


def ingest_source(
    conn,
    source: FeedSource,
    run_date: date,
    criteria: Criteria,
    feeds_dir: str | Path,
    client: httpx.Client,
    *,
    dry_run: bool = False,
) -> list[IngestCounts]:
    """Download + ingest each of the source's feed files. A file that is not
    published yet (404 during the ~1-day lag) is a warning + skip, not a crash."""
    results: list[IngestCounts] = []
    for feed_file in source.feed_files(run_date):
        try:
            path = download(feed_file, feeds_dir, client)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                print(f"warning: {feed_file.remote_url} not published yet (404) — skipping")
                continue
            raise
        results.append(
            ingest_file(
                conn,
                source,
                path=path,
                feed_category=feed_file.feed_category,
                feed_file_name=feed_file.local_name,
                run_date=run_date,
                criteria=criteria,
                dry_run=dry_run,
            )
        )
    return results


def ingest_local_file(
    conn,
    *,
    path: str | Path,
    criteria: Criteria,
    run_date: date,
    source_name: str = "whoisfreaks",
    feed_category: str | None = None,
    dry_run: bool = False,
) -> IngestCounts:
    """Ingest a LOCAL feed file (offline/TDD + re-ingest-from-retained path)."""
    source = build_source(source_name, criteria)
    category = feed_category or infer_feed_category(Path(path).name)
    if category is None:
        raise ValueError(
            f"cannot infer feed_category from {Path(path).name!r}; pass --feed-category"
        )
    return ingest_file(
        conn,
        source,
        path=Path(path),
        feed_category=category,
        feed_file_name=Path(path).name,
        run_date=run_date,
        criteria=criteria,
        dry_run=dry_run,
    )


def run_ingest(
    conn,
    *,
    criteria: Criteria,
    run_date: date,
    source_names: "list[str]",
    feeds_dir: str | Path,
    client: httpx.Client,
    dry_run: bool = False,
) -> list[IngestCounts]:
    """Ingest every requested source. Not-yet-implemented sources (the Dynadot
    stub, which raises NotImplementedError) are skipped with a notice."""
    results: list[IngestCounts] = []
    for name in source_names:
        source = build_source(name, criteria)
        try:
            results.extend(
                ingest_source(conn, source, run_date, criteria, feeds_dir, client,
                              dry_run=dry_run)
            )
        except NotImplementedError as exc:
            print(f"skipping source {name!r}: {exc}")
    return results


def summary_line(counts: IngestCounts) -> str:
    return (
        f"{counts.source} {counts.feed_file}: seen={counts.seen} "
        f"tld={counts.rejected_tld} charset={counts.rejected_charset} "
        f"length={counts.rejected_length} landed={counts.landed}"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_ingest.py -v`
Expected: all `test_ingest.py` tests PASS.

- [ ] **Step 5: Commit**

```bash
git add domainscout/ingest.py tests/test_ingest.py
git commit -m "feat: ingest orchestration (per-source, run, local-file, 404 skip)"
```

---

### Task 9: `ingest` CLI subcommand

**Files:**
- Modify: `domainscout/commands.py`
- Modify: `domainscout/__main__.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `ingest.run_ingest`, `ingest.ingest_local_file`, `ingest.summary_line`, `ingest.DEFAULT_FEEDS_DIR`; `config.load_criteria`; `db.connect`.
- Produces: `cmd_ingest(args) -> int` and a real `ingest` subparser with flags `--source` (repeatable; default = `criteria.sources`), `--date YYYY-MM-DD` (default: yesterday), `--file PATH`, `--feed-category {expired,dropped}`, `--criteria PATH` (default `criteria.toml`), `--dry-run`.

- [ ] **Step 1: Adjust the stub test + add CLI tests**

In `tests/test_cli.py`, **replace** `test_stub_subcommand_reports_phase` with a still-stubbed command (`ingest` is now real):

```python
def test_stub_subcommand_reports_phase(capsys):
    rc = main(["filter"])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "not implemented" in out
    assert "phase 3" in out
```

Add these tests (top of file already imports `sqlite3`, `main`, `Path`, `REPO_ROOT`):

```python
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "whoisfreaks-sample.csv"


def test_ingest_cli_file_creates_rows_and_prints_summary(tmp_path, capsys):
    dbp = tmp_path / "d.db"
    assert main(["--db", str(dbp), "init-db"]) == 0
    capsys.readouterr()  # drop init-db output
    rc = main(["--db", str(dbp), "ingest", "--file", str(FIXTURE),
               "--feed-category", "expired",
               "--criteria", str(REPO_ROOT / "criteria.toml")])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "landed=6" in out
    conn = sqlite3.connect(dbp)
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 6


def test_ingest_cli_dry_run_writes_nothing(tmp_path):
    dbp = tmp_path / "d.db"
    assert main(["--db", str(dbp), "init-db"]) == 0
    rc = main(["--db", str(dbp), "ingest", "--file", str(FIXTURE),
               "--feed-category", "expired", "--dry-run",
               "--criteria", str(REPO_ROOT / "criteria.toml")])
    assert rc == 0
    conn = sqlite3.connect(dbp)
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli.py -v`
Expected: the two new CLI tests FAIL (ingest routes to the stub → no rows, no `landed=6`). `test_stub_subcommand_reports_phase` now targets `filter` and should already pass.

- [ ] **Step 3: Drop `ingest` from the stubs**

In `domainscout/commands.py`, remove the `"ingest": 2,` entry from `STUB_PHASES`.

In `domainscout/__main__.py`, remove the `"ingest": ...` entry from `_STUB_HELP`.

- [ ] **Step 4: Implement `cmd_ingest`**

In `domainscout/commands.py`, add imports at the top and the handler:

```python
from datetime import date, timedelta
from pathlib import Path

import httpx

from domainscout import db, ingest
from domainscout.config import load_criteria


def cmd_ingest(args: argparse.Namespace) -> int:
    criteria = load_criteria(args.criteria)
    run_date = date.fromisoformat(args.date) if args.date else date.today() - timedelta(days=1)
    conn = db.connect(args.db)
    try:
        if args.file:
            results = [
                ingest.ingest_local_file(
                    conn, path=Path(args.file), criteria=criteria, run_date=run_date,
                    feed_category=args.feed_category, dry_run=args.dry_run,
                )
            ]
        else:
            source_names = args.source or list(criteria.sources)
            client = httpx.Client(timeout=30.0, follow_redirects=True)
            try:
                results = ingest.run_ingest(
                    conn, criteria=criteria, run_date=run_date,
                    source_names=source_names, feeds_dir=ingest.DEFAULT_FEEDS_DIR,
                    client=client, dry_run=args.dry_run,
                )
            finally:
                client.close()
        for counts in results:
            print(ingest.summary_line(counts))
    finally:
        conn.close()
    return 0
```

- [ ] **Step 5: Register the real `ingest` subparser**

In `domainscout/__main__.py`, inside `build_parser()` **before** the `_STUB_HELP` loop, add:

```python
    p_ingest = sub.add_parser(
        "ingest",
        help="[Phase 2] pull daily feeds, apply the .com+charset+length gate, upsert candidates",
    )
    p_ingest.add_argument("--source", action="append",
                          help="source name (repeatable; default: criteria.sources)")
    p_ingest.add_argument("--date", help="feed date YYYY-MM-DD (default: yesterday)")
    p_ingest.add_argument("--file", help="ingest a LOCAL feed file instead of downloading")
    p_ingest.add_argument("--feed-category", choices=["expired", "dropped"],
                          dest="feed_category",
                          help="feed_category for --file when the name is ambiguous")
    p_ingest.add_argument("--criteria", default="criteria.toml",
                          help="path to criteria.toml (default: criteria.toml)")
    p_ingest.add_argument("--dry-run", action="store_true",
                          help="gate + print counts, write nothing")
    p_ingest.set_defaults(func=commands.cmd_ingest)
```

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -v`
Expected: every test PASSES (Phase-1 tests + all Phase-2 tests).

- [ ] **Step 7: Commit**

```bash
git add domainscout/commands.py domainscout/__main__.py tests/test_cli.py
git commit -m "feat: real ingest CLI subcommand (download/--file/--dry-run)"
```

---

### Task 10: Real-data smoke test + finalize phase

**Files:**
- Modify: `CLAUDE.md` (check the Phase 2 box)
- Modify: `docs/PHASE-2-DESIGN.md` (status → BUILT)

**Interfaces:**
- Consumes: the finished `ingest` CLI.
- Produces: a proven real-data run; the phase pushed to `origin/main`.

- [ ] **Step 1: Full suite green**

Run: `python -m pytest -v`
Expected: all tests pass. If not, STOP and fix before continuing.

- [ ] **Step 2: One real ingest against the live feed**

Initialize a throwaway DB and pull yesterday's real feed (network — the one real-data confirmation the phase requires):

```bash
python -m domainscout --db data/smoke.db init-db
python -m domainscout --db data/smoke.db ingest --date 2026-07-13
```

Expected: two summary lines printed (expired + dropped), e.g. `whoisfreaks 2026-07-13-free-expired-domains.csv: seen=... tld=... charset=... length=... landed=...`. Sanity: `seen` in the thousands; `rejected_tld` is a large share (~55–60 %, since ~40–45 % are `.com`); `landed` is a small positive number. A 404 warning (feed for that date not published) is acceptable — retry with `--date 2026-07-12` or the current yesterday. `data/smoke.db` and `data/feeds/` are gitignored.

- [ ] **Step 3: Prove idempotency on real data**

Run the same ingest again:

```bash
python -m domainscout --db data/smoke.db ingest --date 2026-07-13
```

Expected: the feed files are already present (skipped download), counts are identical, and:

```bash
python -c "import sqlite3; c=sqlite3.connect('data/smoke.db'); print('candidates', c.execute('SELECT COUNT(*) FROM candidates').fetchone()[0]); print('ingest_log', c.execute('SELECT COUNT(*) FROM ingest_log').fetchone()[0])"
```

Expected: `candidates` count unchanged between the two runs; `ingest_log` has exactly 2 rows (not 4).

- [ ] **Step 4: Clean up the smoke artifacts**

Run: `python -c "import pathlib, shutil; pathlib.Path('data/smoke.db').unlink(missing_ok=True); shutil.rmtree('data/feeds', ignore_errors=True)"`
(Both are gitignored, but remove them so they don't linger.)

- [ ] **Step 5: Update the phase checklist + design status**

In `CLAUDE.md`, change `- [ ] Phase 2: ingestion ...` to `- [x] Phase 2: ingestion ...`.

In `docs/PHASE-2-DESIGN.md`, change the status line from `📝 **DRAFT — pending owner approval (2026-07-14).**` to `✅ **BUILT 2026-07-14.**` and append one line: `Implemented per docs/superpowers/plans/2026-07-14-phase-2-ingestion.md (feed confirmed: single-column plain-text names, main branch).`

- [ ] **Step 6: Commit the docs**

```bash
git add CLAUDE.md docs/PHASE-2-DESIGN.md
git commit -m "docs: mark Phase 2 (ingestion) built"
```

- [ ] **Step 7: Push the phase (only push of the phase)**

Run: `git push origin main`
Expected: `main -> main` succeeds; local in sync with `origin/main`.

---

## Self-Review

**Spec coverage (docs/PHASE-2-DESIGN.md):**
- Real WhoisFreaks ingestion (expired + dropped) → Tasks 4, 8, 9, 10. ✓
- Shared `FeedSource` interface + Dynadot stub → Tasks 3 (base + stub), 4 (real). ✓
- Pure hard-invariant gate at ingestion → Task 5. ✓
- `ingest` CLI replacing the stub + `ingest_log` rows → Tasks 7 (log), 9 (CLI). ✓
- `httpx` first runtime dep → Task 1. ✓
- Feed location = config, confirmed with real data → Task 2 (config), Task 10 (real confirmation). ✓
- `feed_category` from feed, `lifecycle_status` untouched → Task 7 (asserted). ✓
- Idempotency (skip-if-exists download, upsert, keyed log) → Tasks 6, 7, 8, 10. ✓
- Network isolation via injected client → Tasks 6, 8. ✓
- `--file` + `--dry-run` + 404 warn-skip → Tasks 7 (dry-run), 8 (404, local-file), 9 (CLI). ✓
- Out-of-scope items (Dynadot real, retention prune, filtering, RDAP) → not implemented, as intended. ✓

**Placeholder scan:** none — the single unknown (feed URL/format) was confirmed against the live repo during planning and is baked into Task 2, then re-proven live in Task 10.

**Type consistency:** `gate` returns reasons `rejected_tld|rejected_charset|rejected_length`, matching `IngestCounts` fields and the `ingest_file` tally branches. `IngestCounts`/`Candidate` constructor kwargs match Phase-1 `models.py`. `FeedFile(source, feed_category, remote_url, local_name)` is consistent across `base.py`, `whoisfreaks.py`, and every test. `from_criteria(criteria)` signature is uniform across both adapters and `SOURCE_FACTORIES`. `db.upsert_candidate`/`db.record_ingest` are called with their existing Phase-1 signatures (unchanged).
