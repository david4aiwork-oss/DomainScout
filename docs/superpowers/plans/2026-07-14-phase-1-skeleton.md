# Phase 1 — Skeleton Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a runnable, tested Python scaffold for DomainScout — CLI dispatch, config loader, and an idempotent `init-db` that creates the full open-cycle SQLite schema — with **no pipeline logic** yet.

**Architecture:** A single `domainscout` package. `db.py` owns the schema (DDL string) + `init_db`/`connect`/upsert helpers; `config.py` loads and validates `criteria.toml` into a frozen `Criteria`; `models.py` holds dataclasses and the lifecycle-status sets; `__main__.py` builds an argparse parser and dispatches to handlers in `commands.py` (only `init-db` does real work — every other subcommand is a friendly stub). State lives in SQLite, never in memory between phases (per TDD §4.1).

**Tech Stack:** Python 3.11+ (developed on 3.14), **stdlib only at runtime** (`argparse`, `tomllib`, `sqlite3`, `re`, `pathlib`, `datetime`), `pytest` for tests. No third-party runtime dependencies in Phase 1.

## Global Constraints

- **Python:** `requires-python = ">=3.11"` (needs stdlib `tomllib`).
- **Runtime deps:** none in Phase 1 — stdlib only. `pytest>=8` is a dev-only extra. Later-phase libs (whodap, wordfreq, fastapi, uvicorn, anthropic) are NOT added now (YAGNI).
- **Package / entry point:** package name `domainscout`; must run as `python -m domainscout <subcommand>`.
- **`.com`-only invariant:** `criteria.toml [ingestion].tld` MUST equal `"com"`; config loader rejects anything else.
- **Open-cycle partial index — exact predicate (verbatim):** `WHERE lifecycle_status NOT IN ('renewed','reregistered','dismissed')`. Used identically in the `CREATE UNIQUE INDEX` and in every upsert's `ON CONFLICT` target.
- **Schema invariant:** `lifecycle_status TEXT NOT NULL DEFAULT 'unknown'` (a NULL would silently escape the partial index).
- **Ingestion length ceiling is DERIVED, never stored twice:** `Criteria.ingest_max_length == max(primary_max_length, secondary_max_length)`. No second literal `12` in code.
- **Re-ingest must not reset lifecycle:** the candidate upsert's `DO UPDATE` refreshes `source`/`feed_category` only — it must NOT overwrite `lifecycle_status` (RDAP owns it after ingestion) nor `first_seen` (insert-only).
- **TDD:** every task writes the failing test first, watches it fail, implements minimally, watches it pass, commits.
- **Commit style:** conventional prefixes (`feat:`, `test:`, `chore:`, `docs:`). Commit at the end of each task.

**Spec references:** approved spec `docs/PHASE-1-DESIGN.md`; parent design `docs/TECHNICAL-DESIGN.md` §4.1 (layout), §4.2 (ingestion gate config), §5 (schema).

---

## File Structure

**Created in this plan:**

| Path | Responsibility |
|------|----------------|
| `pyproject.toml` | Packaging metadata, `requires-python`, dev extra, pytest config, console-script. |
| `domainscout/__init__.py` | Package marker + `__version__`. |
| `domainscout/models.py` | `Candidate`, `IngestCounts` dataclasses; `OPEN_STATUSES`/`CLOSED_STATUSES`/`DEFAULT_STATUS`. |
| `domainscout/config.py` | `Criteria` dataclass + `load_criteria()` + `ConfigError`; derives `ingest_max_length`. |
| `domainscout/db.py` | `SCHEMA` DDL, `connect()`, `init_db()`, `upsert_candidate()`, `record_ingest()`, `DEFAULT_DB_PATH`. |
| `domainscout/commands.py` | Subcommand handlers: real `cmd_init_db`, generic `cmd_stub`, `STUB_PHASES` map. |
| `domainscout/__main__.py` | `build_parser()` + `main()`; wires subcommands, dispatches, is the `python -m` entry. |
| `domainscout/sources/__init__.py` | Empty package marker (feed adapters land here in Phase 2). |
| `domainscout/scoring/__init__.py` | Empty package marker (scorers land here in Phase 5). |
| `domainscout/web/__init__.py` | Empty package marker (FastAPI app lands here in Phase 8). |
| `criteria.toml` | Owner criteria as tunable config, incl. the `[ingestion]` table. |
| `.env.example` | Documents required keys; no secrets. |
| `README.md` | Short "how to install / run / test". |
| `tests/test_package.py` | Import + version smoke. |
| `tests/test_models.py` | Status-set partition; dataclass defaults. |
| `tests/test_config.py` | Load/validate `criteria.toml`; derived ceiling; error cases. |
| `tests/test_db.py` | Schema creation, idempotency, NOT NULL/default, partial-index behavior, upsert, ingest_log. |
| `tests/test_cli.py` | Dispatch, `init-db` deliverable, stub messages, `outcome --dismiss` help note, `python -m` entry. |

**Decomposition note (one judgment call, flag at review):** the approved spec's layout sketch lists per-phase "stub modules" (`ingest.py …`). This plan instead routes all not-yet-built subcommands through **one** `cmd_stub` handler in `commands.py` rather than creating nine near-empty module files — DRY/YAGNI; each phase gets its own module when that phase is built. The package dirs (`sources/`, `scoring/`, `web/`) ARE created as the spec shows. If the owner wants the empty per-phase files anyway, that's a one-line-per-file addition.

---

## Task 1: Packaging & scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `domainscout/__init__.py`
- Create: `domainscout/sources/__init__.py`
- Create: `domainscout/scoring/__init__.py`
- Create: `domainscout/web/__init__.py`
- Create: `.env.example`
- Create: `README.md`
- Test: `tests/test_package.py`

**Interfaces:**
- Consumes: nothing.
- Produces: importable package `domainscout` with `domainscout.__version__: str`. Establishes `pytest` runnable via `python -m pytest`.

- [ ] **Step 1: Write the failing test**

`tests/test_package.py`:
```python
def test_package_imports_and_has_version():
    import domainscout
    assert isinstance(domainscout.__version__, str)
    assert domainscout.__version__  # non-empty
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_package.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'domainscout'` (or collection error).

- [ ] **Step 3: Create the package files**

`domainscout/__init__.py`:
```python
"""DomainScout — personal expired-domain discovery pipeline (.com)."""

__version__ = "0.1.0"
```

`domainscout/sources/__init__.py`:
```python
"""Feed adapters (Phase 2): WhoisFreaks free feed, Dynadot expired-auction CSV."""
```

`domainscout/scoring/__init__.py`:
```python
"""AI scoring providers (Phase 5). Provider-agnostic score(domain, context) -> JSON."""
```

`domainscout/web/__init__.py`:
```python
"""Local review UI (Phase 8): FastAPI + uvicorn."""
```

`pyproject.toml`:
```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "domainscout"
version = "0.1.0"
description = "Personal expired-domain discovery pipeline (.com)"
readme = "README.md"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
domainscout = "domainscout.__main__:main"

[tool.setuptools]
packages = ["domainscout", "domainscout.sources", "domainscout.scoring", "domainscout.web"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`.env.example`:
```bash
# DomainScout secrets — copy this file to .env and fill in. NEVER commit .env.

# Phase 5 — Anthropic API (AI scoring).
# NOTE: a Claude Pro subscription does NOT include API credits; buy a min ~$5 credit.
ANTHROPIC_API_KEY=

# Phase 5 — Google Safe Browsing (toxicity gate). Free-tier Google Cloud API key.
GOOGLE_SAFE_BROWSING_API_KEY=

# Phase 2 — OPTIONAL. The Dynadot public expired-auction CSV export needs NO key.
# This key is only for the account-keyed aftermarket API (get_open_auctions, etc.).
# DYNADOT_API_KEY=
```

`README.md`:
```markdown
# DomainScout

Personal expired-domain discovery pipeline for quality **.com** domains.
See [`CLAUDE.md`](CLAUDE.md), [`DECISIONS.md`](DECISIONS.md), and
[`docs/TECHNICAL-DESIGN.md`](docs/TECHNICAL-DESIGN.md) for the design.

## Requirements
- Python 3.11+ (no third-party runtime dependencies in Phase 1).

## Install (editable, with dev tools)
```bash
python -m pip install -e ".[dev]"
```

## Run
```bash
python -m domainscout init-db        # create data/domainscout.db (idempotent)
python -m domainscout --help         # list subcommands
```
Later-phase subcommands (`ingest`, `filter`, `verify`, `score-submit`, …) are
stubs until their phase is built.

## Test
```bash
python -m pytest
```

## Config & secrets
- Criteria live in [`criteria.toml`](criteria.toml).
- Copy [`.env.example`](.env.example) to `.env` and fill in API keys (Phase 5+).
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_package.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml domainscout/ .env.example README.md tests/test_package.py
git commit -m "chore: scaffold domainscout package, packaging, and env template"
```

---

## Task 2: Domain models (`models.py`)

**Files:**
- Create: `domainscout/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `DEFAULT_STATUS: str = "unknown"`
  - `OPEN_STATUSES: frozenset[str]`, `CLOSED_STATUSES: frozenset[str]`, `ALL_STATUSES: frozenset[str]`
  - `@dataclass Candidate(domain: str, source: str, feed_category: str | None = None, lifecycle_status: str = "unknown", id: int | None = None, first_seen: datetime | None = None)`
  - `@dataclass IngestCounts(source: str, feed_file: str, seen: int = 0, rejected_tld: int = 0, rejected_charset: int = 0, rejected_length: int = 0, landed: int = 0, run_date: date | None = None)`

- [ ] **Step 1: Write the failing test**

`tests/test_models.py`:
```python
from datetime import datetime

from domainscout.models import (
    ALL_STATUSES,
    CLOSED_STATUSES,
    DEFAULT_STATUS,
    OPEN_STATUSES,
    Candidate,
    IngestCounts,
)


def test_status_sets_partition_cleanly():
    # open and closed are disjoint, and 'dropped' is OPEN (the live opportunity)
    assert OPEN_STATUSES.isdisjoint(CLOSED_STATUSES)
    assert "dropped" in OPEN_STATUSES
    assert CLOSED_STATUSES == {"renewed", "reregistered", "dismissed"}
    assert ALL_STATUSES == OPEN_STATUSES | CLOSED_STATUSES
    assert DEFAULT_STATUS == "unknown"
    assert DEFAULT_STATUS in OPEN_STATUSES


def test_candidate_defaults_to_unknown_open_status():
    c = Candidate(domain="foo.com", source="whoisfreaks")
    assert c.lifecycle_status == "unknown"
    assert c.feed_category is None
    assert c.id is None
    assert c.first_seen is None


def test_ingest_counts_defaults_zero():
    ic = IngestCounts(source="whoisfreaks", feed_file="2026-07-14-free-dropped-domains.csv")
    assert ic.seen == 0
    assert ic.landed == 0
    assert ic.rejected_charset == 0
    assert ic.run_date is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'domainscout.models'`.

- [ ] **Step 3: Write minimal implementation**

`domainscout/models.py`:
```python
"""Dataclasses and lifecycle-status constants shared across phases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

# Lifecycle status is the single source of truth for whether a cycle is OPEN.
# 'dropped' is OPEN: a dropped-and-registerable domain is the live opportunity.
# A cycle closes ONLY on re-registration, renewal, or owner dismissal.
DEFAULT_STATUS = "unknown"
OPEN_STATUSES = frozenset(
    {"unknown", "expiring", "grace", "redemption", "pending_delete", "dropped"}
)
CLOSED_STATUSES = frozenset({"renewed", "reregistered", "dismissed"})
ALL_STATUSES = OPEN_STATUSES | CLOSED_STATUSES


@dataclass
class Candidate:
    """A domain in one open registration cycle. Phase 1 uses the ingestion-time
    fields; later phases fill the rest via UPDATE, not via this dataclass."""

    domain: str
    source: str
    feed_category: str | None = None  # 'expired' | 'dropped' (from feed filename)
    lifecycle_status: str = DEFAULT_STATUS
    id: int | None = None
    first_seen: datetime | None = None


@dataclass
class IngestCounts:
    """One ingestion audit row (see ingest_log). Not every feed row lands —
    the charset+length gate rejects most (TDD §4.2)."""

    source: str
    feed_file: str
    seen: int = 0
    rejected_tld: int = 0
    rejected_charset: int = 0
    rejected_length: int = 0
    landed: int = 0
    run_date: date | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_models.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add domainscout/models.py tests/test_models.py
git commit -m "feat: add Candidate/IngestCounts models and lifecycle-status sets"
```

---

## Task 3: Config loader (`config.py` + `criteria.toml`)

**Files:**
- Create: `domainscout/config.py`
- Create: `criteria.toml`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing (stdlib `tomllib`, `re`).
- Produces:
  - `class ConfigError(Exception)`
  - `@dataclass(frozen=True) Criteria` with fields: `tld: str`, `charset: str`, `sources: tuple[str, ...]`, `schedule_hint: str`, `primary_max_length: int`, `primary_max_words: int`, `secondary_min_length: int`, `secondary_max_length: int`, `zipf_min: float`, `pronounce_min_score: float`, `tier2_cutoff: int`, `digest_top_n: int`, `rdap_endpoint: str`, `rdap_max_rps: float`, `retention_days: int`; and property `ingest_max_length: int`.
  - `load_criteria(path: str | Path = "criteria.toml") -> Criteria`

- [ ] **Step 1: Write the failing test**

`tests/test_config.py`:
```python
from pathlib import Path

import pytest

from domainscout.config import ConfigError, load_criteria

REPO_ROOT = Path(__file__).resolve().parents[1]

VALID_TOML = """
[ingestion]
tld = "com"
charset = "^[a-z]+$"
sources = ["whoisfreaks", "dynadot"]
schedule_hint = "late-morning"

[primary]
max_length = 8
max_words = 2

[secondary]
min_length = 9
max_length = 12

[dictionary]
zipf_min = 3.0

[pronounceability]
min_score = 0.02

[scoring]
tier2_cutoff = 30
digest_top_n = 10

[rdap]
endpoint = "https://rdap.verisign.com/com/v1/"
max_requests_per_sec = 1.0

[retention]
days = 360
"""


def _write(tmp_path, text):
    p = tmp_path / "criteria.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_valid_config_loads_and_derives_ingest_ceiling(tmp_path):
    crit = load_criteria(_write(tmp_path, VALID_TOML))
    assert crit.tld == "com"
    assert crit.charset == "^[a-z]+$"
    assert crit.sources == ("whoisfreaks", "dynadot")
    assert crit.primary_max_length == 8
    assert crit.secondary_max_length == 12
    # DERIVED ceiling = widest target (12), never a duplicated literal
    assert crit.ingest_max_length == 12
    assert crit.retention_days == 360


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_criteria(tmp_path / "nope.toml")


def test_non_com_tld_rejected(tmp_path):
    bad = VALID_TOML.replace('tld = "com"', 'tld = "net"')
    with pytest.raises(ConfigError, match="tld must be 'com'"):
        load_criteria(_write(tmp_path, bad))


def test_missing_key_names_the_key(tmp_path):
    bad = VALID_TOML.replace("zipf_min = 3.0", "")
    with pytest.raises(ConfigError, match="zipf_min"):
        load_criteria(_write(tmp_path, bad))


def test_invalid_charset_regex_rejected(tmp_path):
    bad = VALID_TOML.replace('charset = "^[a-z]+$"', 'charset = "^[a-z"')
    with pytest.raises(ConfigError, match="charset"):
        load_criteria(_write(tmp_path, bad))


def test_repo_criteria_toml_is_valid():
    # Guards against the shipped config drifting out of sync with the loader.
    crit = load_criteria(REPO_ROOT / "criteria.toml")
    assert crit.tld == "com"
    assert crit.ingest_max_length == max(crit.primary_max_length, crit.secondary_max_length)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'domainscout.config'`.

- [ ] **Step 3: Create `criteria.toml`**

`criteria.toml`:
```toml
# DomainScout criteria — tunable config. See docs/TECHNICAL-DESIGN.md.

[ingestion]                       # the hard-invariant gate (TDD §4.2), applied on the way in
tld = "com"                       # .com only, ever
charset = "^[a-z]+$"              # no hyphens/numbers; shared by both tracks
sources = ["whoisfreaks", "dynadot"]
schedule_hint = "late-morning"    # WhoisFreaks feed has ~1-day lag; don't race the upload
# NOTE: the ingestion length ceiling is DERIVED in code = max(primary.max_length,
# secondary.max_length). Do NOT add a separate value here — one source of truth.

[primary]                         # <=8-char dictionary .com
max_length = 8
max_words = 2

[secondary]                       # 9-12-char invented / geo+service
min_length = 9
max_length = 12                   # widest target => also the derived ingestion ceiling

[dictionary]
zipf_min = 3.0                    # wordfreq zipf_frequency threshold (tunable)

[pronounceability]
min_score = 0.02                  # n-gram floor (tunable; calibrate against outcomes later)

[scoring]
tier2_cutoff = 30                 # deep-score top N
digest_top_n = 10                 # digest shows top N

[rdap]
endpoint = "https://rdap.verisign.com/com/v1/"
max_requests_per_sec = 1.0        # polite default; Verisign publishes no numeric limit

[retention]
days = 360                        # raw feeds + digests pruned after this; DB is permanent
```

- [ ] **Step 4: Write the config loader**

`domainscout/config.py`:
```python
"""Load and validate criteria.toml into a frozen Criteria object."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """Raised when criteria.toml is missing, malformed, or violates an invariant."""


@dataclass(frozen=True)
class Criteria:
    tld: str
    charset: str
    sources: tuple[str, ...]
    schedule_hint: str
    primary_max_length: int
    primary_max_words: int
    secondary_min_length: int
    secondary_max_length: int
    zipf_min: float
    pronounce_min_score: float
    tier2_cutoff: int
    digest_top_n: int
    rdap_endpoint: str
    rdap_max_rps: float
    retention_days: int

    @property
    def ingest_max_length(self) -> int:
        """Charset+length gate ceiling = widest target (TDD §4.2). Derived, not stored."""
        return max(self.primary_max_length, self.secondary_max_length)


def _require(data: dict[str, Any], section: str, key: str) -> Any:
    if section not in data:
        raise ConfigError(f"criteria.toml: missing [{section}] section")
    if key not in data[section]:
        raise ConfigError(f"criteria.toml: missing '{key}' in [{section}]")
    return data[section][key]


def _as_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"criteria.toml: {where} must be an integer, got {value!r}")
    return value


def _as_float(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"criteria.toml: {where} must be a number, got {value!r}")
    return float(value)


def load_criteria(path: str | Path = "criteria.toml") -> Criteria:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"criteria.toml not found at {p}")
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"criteria.toml is not valid TOML: {exc}") from exc

    tld = _require(data, "ingestion", "tld")
    if tld != "com":
        raise ConfigError(
            f"criteria.toml: [ingestion].tld must be 'com' (.com only, ever), got {tld!r}"
        )

    charset = _require(data, "ingestion", "charset")
    try:
        re.compile(charset)
    except re.error as exc:
        raise ConfigError(
            f"criteria.toml: [ingestion].charset is not a valid regex: {exc}"
        ) from exc

    sources = _require(data, "ingestion", "sources")
    if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        raise ConfigError("criteria.toml: [ingestion].sources must be a list of strings")

    return Criteria(
        tld=tld,
        charset=charset,
        sources=tuple(sources),
        schedule_hint=str(_require(data, "ingestion", "schedule_hint")),
        primary_max_length=_as_int(_require(data, "primary", "max_length"), "[primary].max_length"),
        primary_max_words=_as_int(_require(data, "primary", "max_words"), "[primary].max_words"),
        secondary_min_length=_as_int(_require(data, "secondary", "min_length"), "[secondary].min_length"),
        secondary_max_length=_as_int(_require(data, "secondary", "max_length"), "[secondary].max_length"),
        zipf_min=_as_float(_require(data, "dictionary", "zipf_min"), "[dictionary].zipf_min"),
        pronounce_min_score=_as_float(_require(data, "pronounceability", "min_score"), "[pronounceability].min_score"),
        tier2_cutoff=_as_int(_require(data, "scoring", "tier2_cutoff"), "[scoring].tier2_cutoff"),
        digest_top_n=_as_int(_require(data, "scoring", "digest_top_n"), "[scoring].digest_top_n"),
        rdap_endpoint=str(_require(data, "rdap", "endpoint")),
        rdap_max_rps=_as_float(_require(data, "rdap", "max_requests_per_sec"), "[rdap].max_requests_per_sec"),
        retention_days=_as_int(_require(data, "retention", "days"), "[retention].days"),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
git add domainscout/config.py criteria.toml tests/test_config.py
git commit -m "feat: add criteria.toml and validating config loader"
```

---

## Task 4: Database schema & `init-db` (`db.py`)

**Files:**
- Create: `domainscout/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: nothing (stdlib `sqlite3`).
- Produces:
  - `DEFAULT_DB_PATH: str = "data/domainscout.db"`
  - `SCHEMA: str` (the full DDL, all `IF NOT EXISTS`)
  - `connect(db_path: str | Path) -> sqlite3.Connection` (sets `row_factory = sqlite3.Row`)
  - `init_db(db_path: str | Path = DEFAULT_DB_PATH) -> None` (creates parent dir, runs `SCHEMA`, idempotent)

- [ ] **Step 1: Write the failing test**

`tests/test_db.py` (Task-4 portion — the upsert tests come in Task 5, appended to this same file):
```python
import sqlite3

import pytest

from domainscout import db


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _indexes(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def test_init_db_creates_tables_and_indexes(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = sqlite3.connect(dbp)
    assert {"candidates", "ingest_log"} <= _tables(conn)
    assert "ux_open_cycle" in _indexes(conn)


def test_init_db_is_idempotent(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    db.init_db(dbp)  # must not raise
    conn = sqlite3.connect(dbp)
    assert {"candidates", "ingest_log"} <= _tables(conn)


def test_init_db_creates_parent_directory(tmp_path):
    dbp = tmp_path / "nested" / "data" / "d.db"
    db.init_db(dbp)
    assert dbp.exists()


def test_lifecycle_status_defaults_to_unknown(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    conn.execute(
        "INSERT INTO candidates (domain, source, first_seen) VALUES ('foo.com', 'wf', '2026-07-14')"
    )
    conn.commit()
    row = conn.execute("SELECT lifecycle_status FROM candidates WHERE domain='foo.com'").fetchone()
    assert row["lifecycle_status"] == "unknown"


def test_lifecycle_status_not_null_rejects_explicit_null(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO candidates (domain, first_seen, lifecycle_status) "
            "VALUES ('bar.com', '2026-07-14', NULL)"
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'domainscout.db'`.

- [ ] **Step 3: Write the schema + init/connect**

`domainscout/db.py`:
```python
"""SQLite schema, connection helpers, and idempotent init-db.

Open-cycle identity model (TDD §5): a surrogate id PK plus a partial unique
index so at most ONE open cycle exists per domain. 'dropped' is an OPEN state,
so a cycle closes only on 'renewed'/'reregistered'/'dismissed'.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

from domainscout.models import Candidate, IngestCounts

DEFAULT_DB_PATH = "data/domainscout.db"

# Exact predicate — MUST be identical in the index and in every upsert conflict target.
_OPEN_PREDICATE = "lifecycle_status NOT IN ('renewed','reregistered','dismissed')"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS candidates (
  id                 INTEGER PRIMARY KEY,
  domain             TEXT NOT NULL,
  source             TEXT,
  feed_category      TEXT,
  first_seen         TIMESTAMP NOT NULL,
  expiry_date        DATE,
  drop_date_est      DATE,
  drop_date_actual   DATE,
  lifecycle_status   TEXT NOT NULL DEFAULT 'unknown',
  rdap_status        TEXT,
  verified_at        TIMESTAMP,
  filter_pass        BOOLEAN,
  filter_reason      TEXT,
  tier1_score        REAL,
  tier2_scores       TEXT,
  value_range        TEXT,
  rationale          TEXT,
  recommended_action TEXT,
  scored_at          TIMESTAMP,
  outcome            TEXT,
  outcome_price      REAL,
  outcome_date       DATE
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_open_cycle ON candidates(domain)
  WHERE {_OPEN_PREDICATE};

CREATE INDEX IF NOT EXISTS idx_drop_est    ON candidates(drop_date_est);
CREATE INDEX IF NOT EXISTS idx_filter_pass ON candidates(filter_pass);
CREATE INDEX IF NOT EXISTS idx_lifecycle   ON candidates(lifecycle_status);

CREATE TABLE IF NOT EXISTS ingest_log (
  run_date         DATE NOT NULL,
  source           TEXT NOT NULL,
  feed_file        TEXT NOT NULL,
  seen             INTEGER,
  rejected_tld     INTEGER,
  rejected_charset INTEGER,
  rejected_length  INTEGER,
  landed           INTEGER,
  PRIMARY KEY (run_date, source, feed_file)
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    """Create the schema. Idempotent: safe to run on every daily invocation."""
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_db.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add domainscout/db.py tests/test_db.py
git commit -m "feat: add open-cycle SQLite schema and idempotent init_db"
```

---

## Task 5: Upsert helpers (`upsert_candidate`, `record_ingest`)

**Files:**
- Modify: `domainscout/db.py` (append two functions)
- Test: `tests/test_db.py` (append upsert/ingest tests)

**Interfaces:**
- Consumes: `Candidate`, `IngestCounts` from `domainscout.models`; `connect` from Task 4.
- Produces:
  - `upsert_candidate(conn: sqlite3.Connection, candidate: Candidate) -> int` — inserts a new open cycle or updates the existing open row; returns its `id`. On conflict, refreshes `source`/`feed_category` only; NEVER overwrites `lifecycle_status` or `first_seen`.
  - `record_ingest(conn: sqlite3.Connection, counts: IngestCounts) -> None` — upserts one `ingest_log` row keyed on `(run_date, source, feed_file)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py`:
```python
from domainscout.models import Candidate, IngestCounts


def test_upsert_two_open_rows_collapse_to_one(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    id1 = db.upsert_candidate(conn, Candidate(domain="foo.com", source="whoisfreaks"))
    id2 = db.upsert_candidate(conn, Candidate(domain="foo.com", source="dynadot"))
    assert id1 == id2  # same open cycle
    rows = conn.execute("SELECT source FROM candidates WHERE domain='foo.com'").fetchall()
    assert len(rows) == 1
    assert rows[0]["source"] == "dynadot"  # source refreshed


def test_reingest_does_not_reset_lifecycle_status(tmp_path):
    # The exact bug the open-cycle amendment guards: re-ingest must not clobber
    # an RDAP-advanced lifecycle_status back to 'unknown'.
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    db.upsert_candidate(conn, Candidate(domain="foo.com", source="whoisfreaks"))
    conn.execute("UPDATE candidates SET lifecycle_status='dropped' WHERE domain='foo.com'")
    conn.commit()
    db.upsert_candidate(conn, Candidate(domain="foo.com", source="whoisfreaks"))  # incoming 'unknown'
    row = conn.execute("SELECT lifecycle_status FROM candidates WHERE domain='foo.com'").fetchone()
    assert row["lifecycle_status"] == "dropped"  # preserved
    assert conn.execute("SELECT COUNT(*) c FROM candidates").fetchone()["c"] == 1


def test_upsert_preserves_first_seen(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    db.upsert_candidate(conn, Candidate(domain="foo.com", source="whoisfreaks"))
    first = conn.execute("SELECT first_seen FROM candidates WHERE domain='foo.com'").fetchone()["first_seen"]
    db.upsert_candidate(conn, Candidate(domain="foo.com", source="dynadot"))
    second = conn.execute("SELECT first_seen FROM candidates WHERE domain='foo.com'").fetchone()["first_seen"]
    assert first == second


def test_closed_and_open_rows_coexist(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    # cycle 1 closed (reregistered) — inserted directly, outside the partial index
    conn.execute(
        "INSERT INTO candidates (domain, source, first_seen, lifecycle_status) "
        "VALUES ('foo.com', 'whoisfreaks', '2026-01-01', 'reregistered')"
    )
    conn.commit()
    # cycle 2 opens via upsert — no conflict with a (nonexistent) open row
    db.upsert_candidate(conn, Candidate(domain="foo.com", source="whoisfreaks"))
    count = conn.execute("SELECT COUNT(*) c FROM candidates WHERE domain='foo.com'").fetchone()["c"]
    assert count == 2


def test_record_ingest_is_idempotent_per_file(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    counts = IngestCounts(
        source="whoisfreaks",
        feed_file="2026-07-14-free-dropped-domains.csv",
        seen=10000, rejected_tld=5000, rejected_charset=4800, rejected_length=150, landed=50,
        run_date=None,
    )
    db.record_ingest(conn, counts)
    counts.landed = 55  # a re-run recomputes
    db.record_ingest(conn, counts)
    rows = conn.execute("SELECT landed FROM ingest_log").fetchall()
    assert len(rows) == 1
    assert rows[0]["landed"] == 55
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_db.py -v -k "upsert or reingest or first_seen or coexist or record_ingest"`
Expected: FAIL — `AttributeError: module 'domainscout.db' has no attribute 'upsert_candidate'`.

- [ ] **Step 3: Append the upsert helpers to `domainscout/db.py`**

```python
def upsert_candidate(conn: sqlite3.Connection, candidate: Candidate) -> int:
    """Insert a new open cycle for candidate.domain, or update the existing open
    row. Returns the row id. Refreshes source/feed_category only — lifecycle_status
    (RDAP owns it post-ingestion) and first_seen (insert-only) are never touched."""
    first_seen = candidate.first_seen or datetime.now()
    if isinstance(first_seen, datetime):
        first_seen = first_seen.isoformat(timespec="seconds")
    cur = conn.execute(
        f"""
        INSERT INTO candidates (domain, source, feed_category, first_seen, lifecycle_status)
        VALUES (:domain, :source, :feed_category, :first_seen, :lifecycle_status)
        ON CONFLICT(domain) WHERE {_OPEN_PREDICATE}
        DO UPDATE SET
            source = excluded.source,
            feed_category = excluded.feed_category
        RETURNING id
        """,
        {
            "domain": candidate.domain,
            "source": candidate.source,
            "feed_category": candidate.feed_category,
            "first_seen": first_seen,
            "lifecycle_status": candidate.lifecycle_status,
        },
    )
    row_id = cur.fetchone()[0]
    conn.commit()
    return row_id


def record_ingest(conn: sqlite3.Connection, counts: IngestCounts) -> None:
    """Upsert one ingest_log row, keyed (run_date, source, feed_file). Re-running
    a day's file recomputes and overwrites the counts (idempotent)."""
    run_date = counts.run_date or date.today()
    if isinstance(run_date, date):
        run_date = run_date.isoformat()
    conn.execute(
        """
        INSERT INTO ingest_log
            (run_date, source, feed_file, seen, rejected_tld, rejected_charset, rejected_length, landed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_date, source, feed_file) DO UPDATE SET
            seen = excluded.seen,
            rejected_tld = excluded.rejected_tld,
            rejected_charset = excluded.rejected_charset,
            rejected_length = excluded.rejected_length,
            landed = excluded.landed
        """,
        (
            run_date, counts.source, counts.feed_file, counts.seen,
            counts.rejected_tld, counts.rejected_charset, counts.rejected_length, counts.landed,
        ),
    )
    conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_db.py -v`
Expected: PASS (all Task-4 + Task-5 tests, 10 total).

- [ ] **Step 5: Commit**

```bash
git add domainscout/db.py tests/test_db.py
git commit -m "feat: add candidate upsert and ingest_log helpers (open-cycle safe)"
```

---

## Task 6: CLI dispatch (`commands.py` + `__main__.py`)

**Files:**
- Create: `domainscout/commands.py`
- Create: `domainscout/__main__.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `db.init_db`, `db.DEFAULT_DB_PATH` (Task 4).
- Produces:
  - `commands.STUB_PHASES: dict[str, int]`
  - `commands.cmd_init_db(args) -> int`
  - `commands.cmd_stub(args) -> int`
  - `__main__.build_parser() -> argparse.ArgumentParser`
  - `__main__.main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py`:
```python
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from domainscout.__main__ import main

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_init_db_subcommand_creates_database(tmp_path, capsys):
    dbp = tmp_path / "d.db"
    rc = main(["--db", str(dbp), "init-db"])
    assert rc == 0
    assert dbp.exists()
    out = capsys.readouterr().out.lower()
    assert "initialized" in out
    conn = sqlite3.connect(dbp)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"candidates", "ingest_log"} <= names


def test_init_db_is_idempotent_via_cli(tmp_path):
    dbp = tmp_path / "d.db"
    assert main(["--db", str(dbp), "init-db"]) == 0
    assert main(["--db", str(dbp), "init-db"]) == 0  # second run must not error


def test_stub_subcommand_reports_phase(capsys):
    rc = main(["ingest"])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "not implemented" in out
    assert "phase 2" in out


def test_score_subcommands_exist_and_stub(capsys):
    assert main(["score-submit"]) == 0
    assert main(["score-collect"]) == 0
    out = capsys.readouterr().out.lower()
    assert out.count("phase 5") == 2


def test_outcome_help_records_dismiss_intent(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["outcome", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out.lower()
    assert "dismiss" in out


def test_module_entrypoint_runs(tmp_path):
    dbp = tmp_path / "e.db"
    result = subprocess.run(
        [sys.executable, "-m", "domainscout", "--db", str(dbp), "init-db"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert dbp.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'domainscout.__main__'`.

- [ ] **Step 3: Write the command handlers**

`domainscout/commands.py`:
```python
"""Subcommand handlers. Only init-db does real work in Phase 1; the rest are
friendly stubs that name the phase that will implement them."""

from __future__ import annotations

import argparse

from domainscout import db

# Subcommand -> the phase number that will implement it.
STUB_PHASES: dict[str, int] = {
    "ingest": 2,
    "filter": 3,
    "verify": 4,
    "score-submit": 5,
    "score-collect": 5,
    "outcome": 6,
    "digest": 7,
    "prune": 8,
    "web": 8,
}


def cmd_init_db(args: argparse.Namespace) -> int:
    db.init_db(args.db)
    print(f"Initialized DomainScout database at {args.db}")
    return 0


def cmd_stub(args: argparse.Namespace) -> int:
    phase = STUB_PHASES[args.command]
    print(f"domainscout: '{args.command}' is not implemented yet (Phase {phase}).")
    return 0
```

- [ ] **Step 4: Write the parser + entry point**

`domainscout/__main__.py`:
```python
"""python -m domainscout — argparse dispatch across the pipeline phases."""

from __future__ import annotations

import argparse
import sys

from domainscout import __version__, commands
from domainscout.db import DEFAULT_DB_PATH

_STUB_HELP = {
    "ingest": "[Phase 2] pull daily feeds, apply the .com+charset+length gate, upsert candidates",
    "filter": "[Phase 3] deterministic rules filter (dictionary + pronounceability)",
    "verify": "[Phase 4] RDAP verification and status-driven drop dates",
    "score-submit": "[Phase 5] submit the AI scoring batch",
    "score-collect": "[Phase 5] collect AI scoring batch results",
    "digest": "[Phase 7] generate the ranked daily digest",
    "prune": "[Phase 8] prune retained feeds/digests past the retention window",
    "web": "[Phase 8] run the FastAPI review UI",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="domainscout", description="Expired-domain discovery pipeline (.com).")
    parser.add_argument("--version", action="version", version=f"domainscout {__version__}")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help=f"SQLite path (default: {DEFAULT_DB_PATH})")

    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    p_init = sub.add_parser("init-db", help="create the database schema (idempotent)")
    p_init.set_defaults(func=commands.cmd_init_db)

    # outcome carries the dismissal-intent note now; the --dismiss flag lands in Phase 6.
    p_outcome = sub.add_parser(
        "outcome",
        help="[Phase 6] record real-world outcomes",
        description=(
            "Phase 6 (stub). Will also be the manual dismissal path: "
            "`outcome <domain> --dismiss` sets lifecycle_status='dismissed' to close "
            "an open cycle from the CLI before the Phase 8 UI exists."
        ),
    )
    p_outcome.set_defaults(func=commands.cmd_stub)

    for name, help_text in _STUB_HELP.items():
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=commands.cmd_stub)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -v`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
git add domainscout/commands.py domainscout/__main__.py tests/test_cli.py
git commit -m "feat: add argparse CLI dispatch with init-db and phase stubs"
```

---

## Task 7: Finalize — full suite, status checklist, done

**Files:**
- Modify: `CLAUDE.md` (flip the Phase 1 checkbox)
- Modify: `docs/PHASE-1-DESIGN.md` (mark built)

**Interfaces:**
- Consumes: everything above.
- Produces: a green full test suite and updated project status.

- [ ] **Step 1: Run the entire suite**

Run: `python -m pytest -v`
Expected: PASS — all tests across `test_package`, `test_models`, `test_config`, `test_db`, `test_cli` green.

- [ ] **Step 2: Verify the deliverable by hand**

Run: `python -m domainscout init-db && python -m domainscout init-db && python -m domainscout --help`
Expected: prints "Initialized DomainScout database at data/domainscout.db" twice (idempotent, no error), then the subcommand list. Confirm `data/domainscout.db` exists and is gitignored (it is — `.gitignore` excludes `data/*.db`).

- [ ] **Step 3: Update the Phase 1 status**

In `CLAUDE.md`, under `## Current Status`, change:
```
- [ ] Phase 1: skeleton
```
to:
```
- [x] Phase 1: skeleton
```

In `docs/PHASE-1-DESIGN.md`, change the Status line's leading marker from `✅ **APPROVED 2026-07-14**` to `✅ **BUILT 2026-07-14**` and append: `Implemented per docs/superpowers/plans/2026-07-14-phase-1-skeleton.md.`

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/PHASE-1-DESIGN.md
git commit -m "docs: mark Phase 1 (skeleton) built"
```

- [ ] **Step 5: Push**

```bash
git push origin main
```

---

## Self-Review

**Spec coverage (against `docs/PHASE-1-DESIGN.md`):**
- CLI = stdlib argparse, `python -m domainscout <cmd>` → Task 6. ✅
- `init-db` creates `candidates` (amended partial index + `NOT NULL DEFAULT 'unknown'`) **and** `ingest_log`, idempotent → Tasks 4/6. ✅
- Config = TOML with new `[ingestion]` table; length ceiling **derived** = `max(primary,secondary).max_length` → Task 3 (`Criteria.ingest_max_length`, tested). ✅
- `score-submit` / `score-collect` (not one `score`) → Task 6 (`STUB_PHASES`, tested both exist + report Phase 5). ✅
- `web/` stub is FastAPI-destined package dir → Task 1 (`web/__init__.py` docstring). ✅
- `.env.example` documents `ANTHROPIC_API_KEY`, `GOOGLE_SAFE_BROWSING_API_KEY`, commented `# DYNADOT_API_KEY` → Task 1. ✅
- `outcome` stub help records the future `outcome <domain> --dismiss` intent → Task 6 (tested `outcome --help` contains "dismiss"). ✅
- Models dataclasses → Task 2. ✅
- TDD-first for config + db → Tasks 3/4/5 write tests before code. ✅
- Working deliverable `python -m domainscout init-db` → Task 6 (`test_module_entrypoint_runs`) + Task 7 manual check. ✅
- `pyproject.toml`, `README.md` → Task 1. ✅

**Placeholder scan:** no TBD/TODO; every code step shows complete code; every command shows expected output. ✅

**Type consistency:** `Candidate`/`IngestCounts` field names identical across `models.py`, `db.py` upserts, and tests; `_OPEN_PREDICATE` string reused verbatim in the index DDL and the upsert conflict target; `args.db`/`args.command`/`args.func` set in `build_parser` and read in `commands`. ✅

**One flagged judgment call:** consolidated stub handler (`cmd_stub`) instead of nine empty per-phase module files — noted in File Structure for owner veto at review.
