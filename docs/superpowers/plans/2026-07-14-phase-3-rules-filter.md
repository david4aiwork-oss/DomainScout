# Phase 3 — Rules Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A deterministic, no-network, fully-logged tunable filter over open `candidates`: classify primary/secondary, score dictionary commonness (`wordfreq`) and phonotactic pronounceability (own n-gram model), and write a track-specific pass/fail + reason + both raw scores — cutting ~thousands/day → ~50–200 survivors for Phase 4.

**Architecture:** Pure scoring (`classify`, `dict_score`, `decide` in `filters.py`; `build_tables`/`score` in `pronounce.py`) separated from a DB loop (`filter_candidates`). Pronounceability is a boundary-padded **trigram** model scored in **log space** (mean log conditional probability), tables stored as **integer counts** in tracked package data. Track-specific gating with a `[primary] allow_invented` knob.

**Tech Stack:** Python 3.11+ (dev 3.14), `wordfreq` (3rd runtime dep), stdlib `sqlite3`/`json`/`math`/`re`, `pytest`.

## Global Constraints

- **Python:** `requires-python = ">=3.11"`. 3.11-safe syntax only.
- **New runtime dep:** exactly one — `wordfreq`. Powers *both* the dictionary gate (`zipf_frequency`) and the n-gram training corpus. (`httpx`, `truststore` already present.)
- **No network** anywhere in Phase 3, including the test suite. `wordfreq` data is local; n-gram tables are a committed artifact / test fixture.
- **Gate composition (owner-approved, track-specific):** `dict_ok = dict_score >= zipf_min`; `pron_ok = pronounce_score >= pronounce_min_score`.
  - **primary** (label length ≤ `primary_max_length`=8): `pass = dict_ok or pron_ok` when `primary_allow_invented` (default **true**), else `pass = dict_ok`.
  - **secondary** (9–12): `pass = pron_ok or dict_ok`.
- **Dictionary score:** `max(zipf(whole), best 2-way split with combine op)`; both split parts length ≥ 2; combine default `min`. Winning segmentation recorded in `filter_reason`.
- **Pronounceability score:** boundary-pad `^^label$`; **mean of log P(c3|c1c2)** over the `len(label)+1` trigram positions; **trigram-uniform for all lengths** (single threshold scale — refines the design's "bigram fallback" note per the length-consistency requirement). Add-one smoothing at load, `V = 27` (26 letters + end `$`). Always finite and ≤ 0.
- **`pronounce_min_score` is a negative log-space floor set by calibration** (Task 9). The Phase-1 `0.02` is obsolete.
- **`filter_reason` names the admitting/failing gate** (the tuning histogram): pass→`"{track} dict={d:.2f} {seg}"` or `"{track} pronounce={p:.2f}"`; reject→names what failed. Dict takes precedence in the label when both pass.
- **`filtered_at` is the idempotency guard.** Default run filters `WHERE filtered_at IS NULL` (open rows); `--recompute` = all open rows.
- **`--recompute` never clears downstream columns** (`tier1_score`, `tier2_scores`, `value_range`, `verified_at`, `scored_at`, outcomes). Filter writes only its 6 columns. Downstream selects `WHERE filter_pass = 1`, so a demoted row simply stops flowing forward.
- **Filter never writes `lifecycle_status`** (Phase 4 owns it).
- **Git cadence:** commit per task locally; **push once, at phase end** (Task 9).

## File structure

**Create:**
- `domainscout/filters.py` — `classify`, `dict_score`, `decide`, `filter_candidates`.
- `domainscout/pronounce.py` — `build_tables`, `save_tables`, `Model`, `load_tables`, `default_model`, `score`, `DEFAULT_TABLES_PATH`.
- `domainscout/pronounce_tables.json` — tracked package data (integer counts + `_meta`), generated in Task 9.
- `tests/test_filters.py`, `tests/test_pronounce.py`.

**Modify:**
- `pyproject.toml` — `dependencies` += `wordfreq`; ship `pronounce_tables.json` as package data.
- `domainscout/config.py` — `Criteria.primary_allow_invented`, `Criteria.dictionary_combine` + loader + `_as_bool`.
- `criteria.toml` — `[primary] allow_invented`, `[dictionary] combine`, `[pronounceability] min_score` → log-space (Task 9).
- `domainscout/db.py` — 4 columns in DDL + `_migrate` + `set_filter_result`.
- `domainscout/models.py` — `FilterCounts` dataclass.
- `domainscout/commands.py` — `cmd_filter`, `cmd_build_ngrams`; drop `filter` from `STUB_PHASES`.
- `domainscout/__main__.py` — real `filter` + `build-ngrams` subparsers; drop `filter` from `_STUB_HELP`.
- `tests/test_config.py`, `tests/test_db.py`, `tests/test_cli.py` — extend/adjust.

**No change:** `ingest.py`, `sources/*`.

---

### Task 1: Add `wordfreq` runtime dependency

**Files:**
- Modify: `pyproject.toml:11`

**Interfaces:**
- Produces: `wordfreq` importable; `zipf_frequency(word, "en")` available.

- [ ] **Step 1: Declare it**

In `pyproject.toml`, change `dependencies = ["httpx", "truststore"]` to:

```toml
dependencies = ["httpx", "truststore", "wordfreq"]
```

- [ ] **Step 2: Install**

Run: `python -m pip install wordfreq`
Expected: installs `wordfreq` (+ `regex`, `msgpack`, `langcodes`, …) or "Requirement already satisfied".

- [ ] **Step 3: Verify import + API (arg order is `(word, lang)`)**

Run: `python -c "from wordfreq import zipf_frequency; print('apple', zipf_frequency('apple','en')); print('xqzk', zipf_frequency('xqzk','en'))"`
Expected: `apple` ≈ 4.76, `xqzk` = 0.0, exit 0.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build: add wordfreq runtime dependency (Phase 3)"
```

---

### Task 2: Config — `[primary] allow_invented` + `[dictionary] combine`

**Files:**
- Modify: `domainscout/config.py`
- Modify: `criteria.toml`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: existing `Criteria`, `_require`, `load_criteria`.
- Produces: `Criteria.primary_allow_invented: bool` (default `True`), `Criteria.dictionary_combine: str` (default `"min"`, must be `"min"`|`"mean"`); `_as_bool` helper.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_config.py`:

```python
def test_filter_knobs_default_when_absent(tmp_path):
    crit = load_criteria(_write(tmp_path, VALID_TOML))  # VALID_TOML has no allow_invented/combine
    assert crit.primary_allow_invented is True
    assert crit.dictionary_combine == "min"


def test_filter_knobs_explicit(tmp_path):
    toml = VALID_TOML.replace("[primary]\n", "[primary]\nallow_invented = false\n")
    toml = toml.replace("[dictionary]\n", "[dictionary]\ncombine = \"mean\"\n")
    crit = load_criteria(_write(tmp_path, toml))
    assert crit.primary_allow_invented is False
    assert crit.dictionary_combine == "mean"


def test_bad_combine_rejected(tmp_path):
    toml = VALID_TOML.replace("[dictionary]\n", "[dictionary]\ncombine = \"median\"\n")
    with pytest.raises(ConfigError, match="combine"):
        load_criteria(_write(tmp_path, toml))
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_config.py -k "filter_knobs or combine" -v`
Expected: FAIL (`Criteria` has no attribute `primary_allow_invented`).

- [ ] **Step 3: Implement**

In `domainscout/config.py`, add the two fields as the **last** dataclass fields (defaults come after non-defaults; `whoisfreaks` stays before them is fine — all three are defaulted):

```python
    whoisfreaks: WhoisFreaksConfig | None = None
    primary_allow_invented: bool = True
    dictionary_combine: str = "min"
```

Add a `_as_bool` helper next to `_as_float`:

```python
def _as_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"criteria.toml: {where} must be a boolean, got {value!r}")
    return value
```

In `load_criteria`, before the `return Criteria(...)`, build and validate the knobs:

```python
    allow_invented = _as_bool(
        data["primary"].get("allow_invented", True), "[primary].allow_invented"
    )
    combine = str(data["dictionary"].get("combine", "min"))
    if combine not in ("min", "mean"):
        raise ConfigError(
            f"criteria.toml: [dictionary].combine must be 'min' or 'mean', got {combine!r}"
        )
```

(`data["primary"]`/`data["dictionary"]` are guaranteed present — `max_length`/`zipf_min` are already `_require`d above.)

Add to the `Criteria(...)` call (after `whoisfreaks=whoisfreaks,`):

```python
        primary_allow_invented=allow_invented,
        dictionary_combine=combine,
```

- [ ] **Step 4: Add knobs to shipped `criteria.toml`**

Edit `criteria.toml`:

```toml
[primary]                         # <=8-char dictionary .com
max_length = 8
max_words = 2
allow_invented = true             # true: short invented-but-pronounceable names pass via the pronounce gate

[dictionary]
zipf_min = 3.0                    # wordfreq zipf_frequency threshold (tunable)
combine = "min"                   # two-word split score = min|mean of the parts
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_config.py -v`
Expected: all config tests PASS.

- [ ] **Step 6: Commit**

```bash
git add domainscout/config.py criteria.toml tests/test_config.py
git commit -m "feat: add [primary].allow_invented + [dictionary].combine knobs"
```

---

### Task 3: Schema — 4 filter columns, migration, `set_filter_result`

**Files:**
- Modify: `domainscout/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: existing `SCHEMA`, `connect`, `init_db`.
- Produces:
  - 4 new `candidates` columns: `track TEXT`, `dict_score REAL`, `pronounce_score REAL`, `filtered_at TIMESTAMP`.
  - `init_db` migrates existing DBs idempotently (all 6 filter columns present after: `filter_pass`, `filter_reason`, + the 4 new).
  - `set_filter_result(conn, candidate_id, *, track, dict_score, pronounce_score, filter_pass, filter_reason, filtered_at=None) -> None`.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_db.py`:

```python
def test_init_db_adds_filter_columns_to_existing_db(tmp_path):
    dbp = tmp_path / "d.db"
    # simulate a pre-Phase-3 DB: candidates without the 4 filter columns
    conn = sqlite3.connect(dbp)
    conn.executescript(
        "CREATE TABLE candidates (id INTEGER PRIMARY KEY, domain TEXT NOT NULL, "
        "first_seen TIMESTAMP NOT NULL, lifecycle_status TEXT NOT NULL DEFAULT 'unknown', "
        "filter_pass BOOLEAN, filter_reason TEXT);"
    )
    conn.commit()
    conn.close()
    db.init_db(dbp)  # must migrate
    conn = db.connect(dbp)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(candidates)")}
    assert {"track", "dict_score", "pronounce_score", "filtered_at"} <= cols


def test_init_db_migration_is_idempotent(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    db.init_db(dbp)  # second run must not raise (columns already exist)
    conn = db.connect(dbp)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(candidates)")}
    assert {"track", "dict_score", "pronounce_score", "filtered_at"} <= cols


def test_set_filter_result_writes_all_fields(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    cid = db.upsert_candidate(conn, Candidate(domain="redfox.com", source="whoisfreaks"))
    db.set_filter_result(
        conn, cid, track="primary", dict_score=3.4, pronounce_score=-2.1,
        filter_pass=True, filter_reason="primary dict=3.40 red+fox",
    )
    row = conn.execute(
        "SELECT track, dict_score, pronounce_score, filter_pass, filter_reason, filtered_at "
        "FROM candidates WHERE id=?", (cid,)
    ).fetchone()
    assert row["track"] == "primary"
    assert row["dict_score"] == 3.4
    assert row["pronounce_score"] == -2.1
    assert row["filter_pass"] == 1
    assert "red+fox" in row["filter_reason"]
    assert row["filtered_at"] is not None  # timestamp set
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_db.py -k "filter_columns or migration or set_filter_result" -v`
Expected: FAIL (`set_filter_result` missing; migration not applied on the hand-made old DB).

- [ ] **Step 3: Add the columns to the DDL**

In `domainscout/db.py` `SCHEMA`, insert the 4 columns right after `filter_reason      TEXT,`:

```sql
  filter_pass        BOOLEAN,
  filter_reason      TEXT,
  track              TEXT,
  dict_score         REAL,
  pronounce_score    REAL,
  filtered_at        TIMESTAMP,
  tier1_score        REAL,
```

- [ ] **Step 4: Add the migration + helper**

In `domainscout/db.py`, add a module constant near `SCHEMA`:

```python
# Columns added after the initial candidates schema — migrated in on existing DBs.
_MIGRATION_COLUMNS = [
    ("track", "TEXT"),
    ("dict_score", "REAL"),
    ("pronounce_score", "REAL"),
    ("filtered_at", "TIMESTAMP"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotently add any missing post-initial columns (PRAGMA-guarded)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(candidates)")}
    for name, decl in _MIGRATION_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE candidates ADD COLUMN {name} {decl}")
```

Call `_migrate` inside `init_db`, after `executescript`, before `commit`:

```python
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()
```

Add the writer helper (uses `datetime` already imported in db.py):

```python
def set_filter_result(
    conn: sqlite3.Connection,
    candidate_id: int,
    *,
    track: str,
    dict_score: float,
    pronounce_score: float,
    filter_pass: bool,
    filter_reason: str,
    filtered_at: str | None = None,
) -> None:
    """Write the 6 Phase-3 filter columns for one candidate. Touches nothing else."""
    stamp = filtered_at or datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE candidates
           SET track = ?, dict_score = ?, pronounce_score = ?,
               filter_pass = ?, filter_reason = ?, filtered_at = ?
         WHERE id = ?
        """,
        (track, dict_score, pronounce_score, 1 if filter_pass else 0,
         filter_reason, stamp, candidate_id),
    )
    conn.commit()
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_db.py -v`
Expected: all db tests PASS (Phase-1/2 + the 3 new).

- [ ] **Step 6: Commit**

```bash
git add domainscout/db.py tests/test_db.py
git commit -m "feat: add filter columns + idempotent migration + set_filter_result"
```

---

### Task 4: `classify` + `dict_score`

**Files:**
- Create: `domainscout/filters.py`
- Test: `tests/test_filters.py`

**Interfaces:**
- Consumes: `Criteria` (`primary_max_length`, `zipf_min`, `dictionary_combine`); `wordfreq.zipf_frequency`.
- Produces:
  - `classify(label: str, criteria: Criteria) -> str` → `"primary"` | `"secondary"`.
  - `dict_score(label: str, criteria: Criteria) -> tuple[float, str]` → `(score, segmentation)`; segmentation is `label` or `"left+right"`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_filters.py`:

```python
from pathlib import Path

from domainscout.config import load_criteria
from domainscout.filters import classify, dict_score

CRIT = load_criteria(Path(__file__).resolve().parents[1] / "criteria.toml")


def test_classify_boundaries():
    assert classify("converse", CRIT) == "primary"     # len 8
    assert classify("ninechars", CRIT) == "secondary"  # len 9
    assert classify("zebuervamat", CRIT) == "secondary" # len 11


def test_dict_score_whole_word():
    score, seg = dict_score("apple", CRIT)
    assert score > 4.0
    assert seg == "apple"


def test_dict_score_two_way_split():
    score, seg = dict_score("redfox", CRIT)   # red + fox, min-combine
    assert seg == "red+fox"
    assert score > 3.0


def test_dict_score_nonword_near_zero():
    score, seg = dict_score("xqzk", CRIT)
    assert score == 0.0


def test_dict_score_no_single_char_fragments():
    # 'a'+'pple' must not win via common single letter 'a'
    score, seg = dict_score("apple", CRIT)
    assert "+" not in seg  # whole word wins, not a 1-char split
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_filters.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'domainscout.filters'`.

- [ ] **Step 3: Implement**

Create `domainscout/filters.py`:

```python
"""Phase 3 rules filter: track classification + graded dictionary + pronounceability
gates. Pure scoring functions + one DB loop. No network."""

from __future__ import annotations

from wordfreq import zipf_frequency

from domainscout.config import Criteria


def classify(label: str, criteria: Criteria) -> str:
    return "primary" if len(label) <= criteria.primary_max_length else "secondary"


def dict_score(label: str, criteria: Criteria) -> tuple[float, str]:
    """Best of the whole label and every 2-way split (both parts >= 2 chars),
    parts combined by criteria.dictionary_combine ('min'|'mean'). Returns
    (score, winning_segmentation)."""
    best = zipf_frequency(label, "en")
    best_seg = label
    for i in range(2, len(label) - 1):  # both parts length >= 2
        left, right = label[:i], label[i:]
        lz, rz = zipf_frequency(left, "en"), zipf_frequency(right, "en")
        combined = min(lz, rz) if criteria.dictionary_combine == "min" else (lz + rz) / 2
        if combined > best:
            best, best_seg = combined, f"{left}+{right}"
    return best, best_seg
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_filters.py -v`
Expected: the 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add domainscout/filters.py tests/test_filters.py
git commit -m "feat: add classify + graded dict_score (whole-word + 2-way split)"
```

---

### Task 5: `pronounce.build_tables` + `save_tables`

**Files:**
- Create: `domainscout/pronounce.py`
- Test: `tests/test_pronounce.py`

**Interfaces:**
- Consumes: `wordfreq.top_n_list` (optional — tests pass `words=` directly).
- Produces:
  - `build_tables(top_n: int = 50000, words: list[str] | None = None) -> dict` → `{"_meta": {...}, "trigram_counts": {...}, "context2_totals": {...}}`, integer counts, boundary-padded `^^word$`.
  - `save_tables(tables: dict, path: str | Path) -> None` → JSON, `sort_keys=True`, compact separators (byte-deterministic except `_meta.built`).
  - `DEFAULT_TABLES_PATH = Path(__file__).parent / "pronounce_tables.json"`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_pronounce.py`:

```python
import json

from domainscout import pronounce


def test_build_tables_counts_and_padding():
    t = pronounce.build_tables(words=["fox", "ox"])
    # "^^fox$" trigrams: ^^f ^fo fox ox$ ; "^^ox$": ^^o ^ox ox$
    assert t["trigram_counts"]["ox$"] == 2   # appears in both
    assert t["trigram_counts"]["fox"] == 1
    assert t["context2_totals"]["ox"] == 2   # 'ox' precedes '$' in both words
    assert "_meta" in t and t["_meta"]["alphabet"]


def test_build_tables_filters_non_alpha_words():
    t = pronounce.build_tables(words=["fox", "f0x", "fo-x", "FOX", ""])
    # only "fox" survives the ^[a-z]+$ filter
    assert t["trigram_counts"].get("fox") == 1


def test_save_tables_is_sorted_and_loadable(tmp_path):
    t = pronounce.build_tables(words=["fox", "ox"])
    p = tmp_path / "tables.json"
    pronounce.save_tables(t, p)
    raw = p.read_text(encoding="utf-8")
    loaded = json.loads(raw)
    assert loaded["trigram_counts"]["fox"] == 1
    # sorted keys => "context2_totals" appears before "trigram_counts"
    assert raw.index('"context2_totals"') < raw.index('"trigram_counts"')
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_pronounce.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'domainscout.pronounce'`.

- [ ] **Step 3: Implement**

Create `domainscout/pronounce.py`:

```python
"""N-gram phonotactic pronounceability scorer.

Boundary-padded trigram model, scored in LOG space (mean log conditional
probability) for a single length-consistent threshold scale. Tables are stored
as INTEGER COUNTS (byte-deterministic in git); add-one smoothing is applied at
load. No network at scoring time."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

DEFAULT_TABLES_PATH = Path(__file__).parent / "pronounce_tables.json"

_WORD_RE = re.compile(r"^[a-z]+$")
V = 27  # smoothing vocabulary: 26 letters + end marker '$' (start '^' is context-only)


def build_tables(top_n: int = 50000, words: list[str] | None = None) -> dict:
    """Count boundary-padded trigrams over English word TYPES (unweighted)."""
    if words is None:
        from wordfreq import top_n_list  # local import: not needed for tests that pass words=
        words = top_n_list("en", top_n)
    trigram_counts: dict[str, int] = {}
    context2_totals: dict[str, int] = {}
    kept = 0
    for w in words:
        if not _WORD_RE.match(w):
            continue
        kept += 1
        padded = f"^^{w}$"
        for i in range(len(padded) - 2):
            tri = padded[i:i + 3]
            ctx = padded[i:i + 2]
            trigram_counts[tri] = trigram_counts.get(tri, 0) + 1
            context2_totals[ctx] = context2_totals.get(ctx, 0) + 1
    try:
        import wordfreq
        wf_version = getattr(wordfreq, "__version__", "unknown")
    except Exception:
        wf_version = "unknown"
    meta = {
        "top_n": top_n,
        "words_kept": kept,
        "wordfreq_version": wf_version,
        "built": date.today().isoformat(),
        "alphabet": "a-z + '^' start (context-only) + '$' end",
        "smoothing": f"add-one at load, V={V}",
        "scoring": "mean log P(c3|c1c2), boundary-padded '^^label$', trigram-uniform",
    }
    return {
        "_meta": meta,
        "trigram_counts": trigram_counts,
        "context2_totals": context2_totals,
    }


def save_tables(tables: dict, path: str | Path) -> None:
    Path(path).write_text(
        json.dumps(tables, sort_keys=True, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_pronounce.py -v`
Expected: the 3 build/save tests PASS.

- [ ] **Step 5: Commit**

```bash
git add domainscout/pronounce.py tests/test_pronounce.py
git commit -m "feat: n-gram table builder (integer counts, boundary-padded trigrams)"
```

---

### Task 6: `pronounce` load + `score` (log space) + scale-contract test

**Files:**
- Modify: `domainscout/pronounce.py`
- Test: `tests/test_pronounce.py` (extend)

**Interfaces:**
- Consumes: table dicts from Task 5.
- Produces:
  - `class Model` with `logp(trigram: str) -> float` (add-one smoothed, log space).
  - `Model.from_tables(tables: dict) -> Model`.
  - `load_tables(path=DEFAULT_TABLES_PATH) -> Model`; `default_model() -> Model` (lazy singleton).
  - `score(label: str, model: Model | None = None) -> float` — mean log P over `^^label$` trigrams; `model=None` → `default_model()`.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_pronounce.py`:

```python
import math

import pytest

# an English-ish training vocab that makes '-and'/'br-' patterns common
FIXTURE_WORDS = ["brand", "brandy", "band", "land", "sand", "hand", "grand",
                 "stand", "bland", "brain", "bread", "break", "brown"]


@pytest.fixture
def model():
    return pronounce.Model.from_tables(pronounce.build_tables(words=FIXTURE_WORDS))


def test_score_orders_realish_above_mash(model):
    assert pronounce.score("brand", model) > pronounce.score("xqzk", model)
    assert pronounce.score("bland", model) > pronounce.score("xqzk", model)


def test_score_smoothing_is_finite(model):
    s = pronounce.score("xqzk", model)  # all-unseen trigrams
    assert math.isfinite(s)             # add-one => never -inf


def test_score_scale_contract(model):
    # pins the SPACE: every score finite, log-space bound (<= 0), and monotonic
    labels = ["brand", "bland", "xqzk"]
    scores = [pronounce.score(x, model) for x in labels]
    assert all(math.isfinite(s) and s <= 0.0 for s in scores)
    assert scores[0] >= scores[1] >= scores[2]
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_pronounce.py -k "score" -v`
Expected: FAIL — `pronounce.Model` / `pronounce.score` missing.

- [ ] **Step 3: Implement**

Append to `domainscout/pronounce.py` (add `import math` to the imports):

```python
class Model:
    """Add-one smoothed trigram log-probabilities over the built counts."""

    def __init__(self, trigram_counts: dict[str, int], context2_totals: dict[str, int]) -> None:
        self._tri = trigram_counts
        self._ctx = context2_totals

    @classmethod
    def from_tables(cls, tables: dict) -> "Model":
        return cls(tables["trigram_counts"], tables["context2_totals"])

    def logp(self, trigram: str) -> float:
        num = self._tri.get(trigram, 0) + 1
        den = self._ctx.get(trigram[:2], 0) + V
        return math.log(num / den)


def load_tables(path: str | Path = DEFAULT_TABLES_PATH) -> Model:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Model.from_tables(data)


_DEFAULT_MODEL: Model | None = None


def default_model() -> Model:
    global _DEFAULT_MODEL
    if _DEFAULT_MODEL is None:
        _DEFAULT_MODEL = load_tables()
    return _DEFAULT_MODEL


def score(label: str, model: Model | None = None) -> float:
    """Mean log P(c3|c1c2) over the boundary-padded trigrams of the label.
    Log space => always <= 0, finite (smoothing). Trigram-uniform for all lengths."""
    m = model if model is not None else default_model()
    padded = f"^^{label}$"
    trigrams = [padded[i:i + 3] for i in range(len(padded) - 2)]
    return sum(m.logp(t) for t in trigrams) / len(trigrams)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_pronounce.py -v`
Expected: all `test_pronounce.py` tests PASS.

- [ ] **Step 5: Commit**

```bash
git add domainscout/pronounce.py tests/test_pronounce.py
git commit -m "feat: log-space trigram pronounceability score (add-one smoothed)"
```

---

### Task 7: `decide` + `filter_candidates` + `FilterCounts`

**Files:**
- Modify: `domainscout/filters.py`
- Modify: `domainscout/models.py`
- Test: `tests/test_filters.py` (extend)

**Interfaces:**
- Consumes: `classify`, `dict_score`, `pronounce.score`, `db.set_filter_result`; `Criteria` (`zipf_min`, `pronounce_min_score`, `primary_allow_invented`).
- Produces:
  - `models.FilterCounts` — `processed, passed, primary, secondary, rejected` (ints, default 0).
  - `decide(track, dict_score_val, seg, pronounce_score_val, criteria) -> tuple[bool, str]`.
  - `filter_candidates(conn, criteria, *, recompute=False, limit=None, dry_run=False) -> FilterCounts`.

- [ ] **Step 1: Write failing tests**

Add `FilterCounts` import + tests to `tests/test_filters.py`:

```python
from datetime import datetime

from domainscout import db
from domainscout.filters import decide, filter_candidates
from domainscout.models import Candidate


def _crit_invented(value):
    # clone CRIT with primary_allow_invented toggled
    from dataclasses import replace
    return replace(CRIT, primary_allow_invented=value)


def test_decide_primary_dictionary_pass():
    ok, reason = decide("primary", 4.2, "red+fox", -9.0, CRIT)
    assert ok and reason.startswith("primary dict=4.2")


def test_decide_primary_invented_pass_when_allowed():
    ok, reason = decide("primary", 0.0, "zylo", -1.0, _crit_invented(True))
    assert ok and "pronounce=" in reason


def test_decide_primary_invented_reject_when_disallowed():
    ok, reason = decide("primary", 0.0, "zylo", -1.0, _crit_invented(False))
    assert not ok and reason.startswith("reject primary")


def test_decide_secondary_pronounce_only():
    ok, reason = decide("secondary", 0.0, "brixly", -1.0, CRIT)
    assert ok and "pronounce=" in reason


def test_decide_secondary_dict_only():
    ok, reason = decide("secondary", 3.4, "maple+desk", -99.0, CRIT)
    assert ok and reason.startswith("secondary dict=3.4")


def test_decide_secondary_both_fail():
    ok, reason = decide("secondary", 1.0, "zzqx", -99.0, CRIT)
    assert not ok and reason.startswith("reject secondary")


def _seed(conn, domains):
    return [db.upsert_candidate(conn, Candidate(domain=d, source="whoisfreaks")) for d in domains]


def test_filter_candidates_writes_fields_and_counts(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    _seed(conn, ["apple.com", "zzqxvv.com"])   # apple passes, zzqxvv rejects
    counts = filter_candidates(conn, CRIT)
    assert counts.processed == 2
    row = conn.execute("SELECT track, dict_score, pronounce_score, filter_pass, filtered_at "
                       "FROM candidates WHERE domain='apple.com'").fetchone()
    assert row["track"] == "primary"
    assert row["filter_pass"] == 1
    assert row["filtered_at"] is not None
    assert row["dict_score"] is not None and row["pronounce_score"] is not None


def test_filter_candidates_idempotent_and_recompute(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    _seed(conn, ["apple.com"])
    filter_candidates(conn, CRIT)
    again = filter_candidates(conn, CRIT)          # nothing new (filtered_at set)
    assert again.processed == 0
    forced = filter_candidates(conn, CRIT, recompute=True)
    assert forced.processed == 1


def test_recompute_does_not_touch_downstream_columns(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    (cid,) = _seed(conn, ["apple.com"])
    filter_candidates(conn, CRIT)
    conn.execute("UPDATE candidates SET tier1_score=7.0, verified_at='2026-07-14' WHERE id=?", (cid,))
    conn.commit()
    filter_candidates(conn, CRIT, recompute=True)
    row = conn.execute("SELECT tier1_score, verified_at FROM candidates WHERE id=?", (cid,)).fetchone()
    assert row["tier1_score"] == 7.0 and row["verified_at"] == "2026-07-14"


def test_filter_candidates_dry_run_writes_nothing(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    _seed(conn, ["apple.com"])
    counts = filter_candidates(conn, CRIT, dry_run=True)
    assert counts.processed == 1
    row = conn.execute("SELECT filtered_at FROM candidates WHERE domain='apple.com'").fetchone()
    assert row["filtered_at"] is None
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_filters.py -k "decide or filter_candidates or recompute" -v`
Expected: FAIL (`decide`/`filter_candidates`/`FilterCounts` missing).

- [ ] **Step 3: Add `FilterCounts`**

In `domainscout/models.py`, append:

```python
@dataclass
class FilterCounts:
    """One filter run's tally (printed summary; per-domain detail lives in filter_reason)."""

    processed: int = 0
    passed: int = 0
    primary: int = 0      # passed & primary track
    secondary: int = 0    # passed & secondary track
    rejected: int = 0
```

- [ ] **Step 4: Implement `decide` + `filter_candidates`**

In `domainscout/filters.py`, add imports at the top:

```python
from datetime import datetime

from domainscout import db, pronounce
from domainscout.models import Candidate, FilterCounts
```

Append the functions:

```python
_OPEN_PREDICATE = "lifecycle_status NOT IN ('renewed','reregistered','dismissed')"


def decide(
    track: str,
    dict_score_val: float,
    seg: str,
    pronounce_score_val: float,
    criteria: Criteria,
) -> tuple[bool, str]:
    """Track-specific pass/fail. Reason names the admitting/failing gate."""
    dict_ok = dict_score_val >= criteria.zipf_min
    pron_ok = pronounce_score_val >= criteria.pronounce_min_score
    if track == "primary":
        passed = dict_ok or (pron_ok if criteria.primary_allow_invented else False)
    else:
        passed = pron_ok or dict_ok
    if passed:
        if dict_ok:  # dict takes precedence in the label when both pass
            return True, f"{track} dict={dict_score_val:.2f} {seg}"
        return True, f"{track} pronounce={pronounce_score_val:.2f}"
    if track == "primary" and not criteria.primary_allow_invented:
        return False, (
            f"reject primary: not dictionary "
            f"(dict={dict_score_val:.2f}<{criteria.zipf_min})"
        )
    return False, (
        f"reject {track}: dict={dict_score_val:.2f}<{criteria.zipf_min}, "
        f"pronounce={pronounce_score_val:.2f}<{criteria.pronounce_min_score}"
    )


def _label(domain: str) -> str:
    return domain[:-4] if domain.endswith(".com") else domain


def filter_candidates(
    conn,
    criteria: Criteria,
    *,
    recompute: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
) -> FilterCounts:
    """Classify + score + decide each open candidate; write the 6 filter columns
    (unless dry_run). Default processes filtered_at IS NULL; recompute = all open."""
    where = _OPEN_PREDICATE if recompute else f"{_OPEN_PREDICATE} AND filtered_at IS NULL"
    sql = f"SELECT id, domain FROM candidates WHERE {where} ORDER BY id"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()

    counts = FilterCounts()
    stamp = datetime.now().isoformat(timespec="seconds")
    for row in rows:
        counts.processed += 1
        label = _label(row["domain"])
        track = classify(label, criteria)
        d_score, seg = dict_score(label, criteria)
        p_score = pronounce.score(label)
        passed, reason = decide(track, d_score, seg, p_score, criteria)
        if passed:
            counts.passed += 1
            counts.primary += track == "primary"
            counts.secondary += track == "secondary"
        else:
            counts.rejected += 1
        if not dry_run:
            db.set_filter_result(
                conn, row["id"], track=track, dict_score=d_score,
                pronounce_score=p_score, filter_pass=passed, filter_reason=reason,
                filtered_at=stamp,
            )
    return counts
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_filters.py -v`
Expected: all `test_filters.py` tests PASS.

Note: these tests call `pronounce.score(label)` with **no** injected model, so they load `domainscout/pronounce_tables.json` via `default_model()`. That file is generated in Task 9. **Until Task 9 exists, run this task's tests with a small tables file present**, OR (preferred) order the work so Task 9's real table build precedes running the full suite. To keep Task 7 self-contained and green now, add this fixture step:

- [ ] **Step 5a: Provide a minimal tables file so `default_model()` loads**

If `domainscout/pronounce_tables.json` does not yet exist, generate a small real one now (it will be regenerated at full size in Task 9):

Run: `python -c "from domainscout import pronounce; pronounce.save_tables(pronounce.build_tables(top_n=20000), pronounce.DEFAULT_TABLES_PATH); print('wrote', pronounce.DEFAULT_TABLES_PATH)"`
Expected: writes the file; re-run Step 5 → PASS.

- [ ] **Step 6: Commit**

```bash
git add domainscout/filters.py domainscout/models.py tests/test_filters.py
git commit -m "feat: track-specific decide + filter_candidates DB loop"
```

---

### Task 8: `filter` + `build-ngrams` CLI subcommands

**Files:**
- Modify: `domainscout/commands.py`
- Modify: `domainscout/__main__.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `filters.filter_candidates`, `pronounce.build_tables`/`save_tables`/`DEFAULT_TABLES_PATH`, `config.load_criteria`, `db.connect`.
- Produces: `cmd_filter(args) -> int`, `cmd_build_ngrams(args) -> int`; real `filter` + `build-ngrams` subparsers; `filter` removed from stubs.

- [ ] **Step 1: Adjust the stub test + add CLI tests**

In `tests/test_cli.py`, **replace** `test_stub_subcommand_reports_phase` (filter is now real) to target `verify` (Phase 4):

```python
def test_stub_subcommand_reports_phase(capsys):
    rc = main(["verify"])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "not implemented" in out
    assert "phase 4" in out
```

Add (uses the existing `sqlite3`, `main`, `REPO_ROOT`, `FIXTURE` from Phase 2):

```python
def test_filter_cli_runs_on_seeded_db(tmp_path, capsys):
    dbp = tmp_path / "d.db"
    assert main(["--db", str(dbp), "init-db"]) == 0
    assert main(["--db", str(dbp), "ingest", "--file", str(FIXTURE),
                 "--feed-category", "expired",
                 "--criteria", str(REPO_ROOT / "criteria.toml")]) == 0
    capsys.readouterr()
    rc = main(["--db", str(dbp), "filter", "--criteria", str(REPO_ROOT / "criteria.toml")])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "processed" in out and "passed" in out
    conn = sqlite3.connect(dbp)
    n = conn.execute("SELECT COUNT(*) FROM candidates WHERE filtered_at IS NOT NULL").fetchone()[0]
    assert n == 6  # all six landed candidates got filtered


def test_build_ngrams_cli_writes_sorted_json(tmp_path):
    out = tmp_path / "t.json"
    rc = main(["build-ngrams", "--top-n", "5000", "--out", str(out)])
    assert rc == 0
    assert out.exists()
    import json
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "trigram_counts" in data and data["_meta"]["top_n"] == 5000
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_cli.py -v`
Expected: `test_filter_cli_runs_on_seeded_db` + `test_build_ngrams_cli_writes_sorted_json` FAIL (filter routes to stub; build-ngrams unknown). The `verify` stub test PASSES.

- [ ] **Step 3: Drop `filter` from the stubs**

In `domainscout/commands.py`, remove `"filter": 3,` from `STUB_PHASES`.
In `domainscout/__main__.py`, remove the `"filter": ...` entry from `_STUB_HELP`.

- [ ] **Step 4: Implement the handlers**

In `domainscout/commands.py`, add imports + handlers (`filters`, `pronounce`):

```python
from domainscout import db, filters, ingest, pronounce
```

(replace the existing `from domainscout import db, ingest`), then append:

```python
def cmd_filter(args: argparse.Namespace) -> int:
    criteria = load_criteria(args.criteria)
    conn = db.connect(args.db)
    try:
        counts = filters.filter_candidates(
            conn, criteria, recompute=args.recompute, limit=args.limit,
            dry_run=args.dry_run,
        )
    finally:
        conn.close()
    print(
        f"filter: processed={counts.processed} passed={counts.passed} "
        f"(primary={counts.primary} secondary={counts.secondary}) "
        f"rejected={counts.rejected}"
        + ("  [dry-run]" if args.dry_run else "")
    )
    return 0


def cmd_build_ngrams(args: argparse.Namespace) -> int:
    out = Path(args.out) if args.out else pronounce.DEFAULT_TABLES_PATH
    tables = pronounce.build_tables(top_n=args.top_n)
    pronounce.save_tables(tables, out)
    size_kb = out.stat().st_size / 1024
    print(f"build-ngrams: wrote {out} ({size_kb:.0f} KB, {tables['_meta']['words_kept']} words)")
    return 0
```

- [ ] **Step 5: Register the subparsers**

In `domainscout/__main__.py` `build_parser()`, after the `ingest` subparser block, add:

```python
    p_filter = sub.add_parser(
        "filter", help="[Phase 3] classify + dictionary/pronounceability gates on candidates")
    p_filter.add_argument("--criteria", default="criteria.toml",
                          help="path to criteria.toml (default: criteria.toml)")
    p_filter.add_argument("--recompute", action="store_true",
                          help="re-filter all open rows (after tuning thresholds)")
    p_filter.add_argument("--limit", type=int, help="max candidates to process")
    p_filter.add_argument("--dry-run", action="store_true",
                          help="compute + print summary, write nothing")
    p_filter.set_defaults(func=commands.cmd_filter)

    p_ngrams = sub.add_parser(
        "build-ngrams", help="[Phase 3] (re)build the pronounceability n-gram tables")
    p_ngrams.add_argument("--top-n", type=int, default=50000, dest="top_n",
                          help="number of top English words to train on (default: 50000)")
    p_ngrams.add_argument("--out", help="output path (default: domainscout/pronounce_tables.json)")
    p_ngrams.set_defaults(func=commands.cmd_build_ngrams)
```

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -v`
Expected: every test PASSES (Phase 1/2/3). (Requires `domainscout/pronounce_tables.json` from Task 7 Step 5a or Task 9.)

- [ ] **Step 7: Commit**

```bash
git add domainscout/commands.py domainscout/__main__.py tests/test_cli.py
git commit -m "feat: real filter + build-ngrams CLI subcommands"
```

---

### Task 9: Real n-gram tables + calibration + finalize

**Files:**
- Create/replace: `domainscout/pronounce_tables.json` (full 50k build)
- Modify: `pyproject.toml` (package-data), `criteria.toml` (calibrated `min_score`), `CLAUDE.md`, `docs/PHASE-3-DESIGN.md`

**Interfaces:**
- Consumes: the finished `filter` + `build-ngrams` CLIs.
- Produces: committed real tables, a calibrated `pronounce_min_score`, phase pushed.

- [ ] **Step 1: Full suite green**

Run: `python -m pytest -q`
Expected: all pass. If not, STOP and fix.

- [ ] **Step 2: Build the real 50k tables + confirm size**

Run: `python -m domainscout build-ngrams`
Expected: writes `domainscout/pronounce_tables.json`; printed size is **low single-digit MB** (a few hundred KB–~3 MB). If it is materially larger, note it and consider gzip (out of scope to implement unless needed).

Run: `python -c "import os; print(round(os.path.getsize('domainscout/pronounce_tables.json')/1048576, 2), 'MB')"`
Record the size in the design doc.

- [ ] **Step 3: Ship the tables as package data**

In `pyproject.toml`, under `[tool.setuptools]`, add:

```toml
[tool.setuptools.package-data]
domainscout = ["pronounce_tables.json"]
```

- [ ] **Step 4: Calibrate `pronounce_min_score` on real survivors**

Re-ingest a live feed date (network — sandbox off) into a throwaway DB, then inspect the pronounceability score distribution:

```bash
python -m domainscout --db data/cal.db init-db
python -m domainscout --db data/cal.db ingest --date <recent-live-date>
python -c "
from domainscout import db, pronounce
from domainscout.filters import _label, classify
from domainscout.config import load_criteria
crit = load_criteria('criteria.toml')
conn = db.connect('data/cal.db')
labels = [_label(r['domain']) for r in conn.execute('SELECT domain FROM candidates')]
scored = sorted((pronounce.score(x), x) for x in labels)
print('n =', len(scored))
for pct in (5,10,25,50,75,90):
    i = int(len(scored)*pct/100); print(f'p{pct}: {scored[i][0]:.2f}  e.g. {scored[i][1]}')
for probe in ['zylo','quivo','brixly','vantor','xqzk','qwrtz']:
    print(f'{probe}: {pronounce.score(probe):.2f}')
"
```

Choose `min_score` so the invented probes (`zylo`/`quivo`/`brixly`/`vantor`) sit clearly above keyboard-mash (`xqzk`/`qwrtz`) and the survivor count lands near 50–200/day (check with `filter --dry-run` at candidate thresholds). Record the chosen value + reasoning.

- [ ] **Step 5: Commit the calibrated threshold**

In `criteria.toml`, set the log-space value (example shape — use YOUR calibrated number):

```toml
[pronounceability]
min_score = -4.0                  # LOG-space mean-log-prob floor (calibrated <date> on <feed-date>); pass if score >= this
```

- [ ] **Step 6: Verify a real filter run hits the target band**

```bash
python -m domainscout --db data/cal.db filter --recompute
```
Expected: `passed=` lands in a sane band (dozens–low hundreds for a 10k-name feed after Phase-3). Spot-check a few `filter_reason` values by track:

```bash
python -c "import sqlite3;c=sqlite3.connect('data/cal.db');[print(r) for r in c.execute('SELECT track,filter_pass,filter_reason FROM candidates ORDER BY random() LIMIT 12')]"
```

- [ ] **Step 7: Clean up calibration artifacts**

Run: `python -c "import pathlib,shutil; pathlib.Path('data/cal.db').unlink(missing_ok=True); shutil.rmtree('data/feeds', ignore_errors=True)"`

- [ ] **Step 8: Update checklist + design status**

`CLAUDE.md`: `- [ ] Phase 3: rules filter ...` → `- [x] Phase 3: rules filter ...`.
`docs/PHASE-3-DESIGN.md`: status `📝 DRAFT ...` → `✅ BUILT 2026-07-14` + append a one-line Build note with the tables size + the calibrated `pronounce_min_score` + the feed date used.

- [ ] **Step 9: Commit + push (only push of the phase)**

```bash
git add domainscout/pronounce_tables.json pyproject.toml criteria.toml CLAUDE.md docs/PHASE-3-DESIGN.md
git commit -m "feat: build real n-gram tables + calibrate pronounce_min_score; mark Phase 3 built"
git push origin main
```
Expected: `main -> main` succeeds; local in sync with `origin/main`.

---

## Self-Review

**Spec coverage (docs/PHASE-3-DESIGN.md):**
- Classification + dict + pronounce gates → Tasks 4, 5, 6, 7. ✓
- Track-specific gating + `allow_invented` → Task 2 (knob), Task 7 (`decide` matrix incl. both settings). ✓
- Whole-word + best 2-way min-split, recorded segmentation → Task 4. ✓
- Log-space mean-log-prob pronounceability, integer-count tables, `_meta`, smoothing at load → Tasks 5, 6. ✓
- Discrete columns + `filtered_at` guard + single-authority migration → Task 3. ✓
- `--recompute` never touches downstream → Task 7 (asserted). ✓
- `filter` + `build-ngrams` CLIs → Task 8. ✓
- `wordfreq` dep → Task 1. ✓
- No `filter_log`, no network → honored throughout (tests inject fixtures/`words=`). ✓
- Real-data: place-name spike (done in design), tables size check + threshold calibration → Task 9. ✓

**Placeholder scan:** the only TBD value — `pronounce_min_score` — is explicitly a Task 9 calibration output in the log space fixed by the design; the `-4.0` in Step 5 is labeled an example to be replaced. No other placeholders.

**Type consistency:** `dict_score -> (float, str)` consumed as `(d_score, seg)` in `filter_candidates` and passed to `decide(track, dict_score_val, seg, pronounce_score_val, criteria)`. `pronounce.score(label, model=None)` returns `float`; `Model.from_tables`/`load_tables`/`default_model` names align across Tasks 5–7. `set_filter_result(conn, id, *, track, dict_score, pronounce_score, filter_pass, filter_reason, filtered_at=None)` matches its call in `filter_candidates`. `FilterCounts` fields (`processed/passed/primary/secondary/rejected`) match the CLI summary in Task 8. `_OPEN_PREDICATE` mirrors the db.py predicate.

**Refinement flagged:** pronounceability is **trigram-uniform** (not the design's literal "bigram fallback for len<3") for a single threshold scale — consistent with the length-consistency requirement; the design doc's pronounce subsection is updated to match.
