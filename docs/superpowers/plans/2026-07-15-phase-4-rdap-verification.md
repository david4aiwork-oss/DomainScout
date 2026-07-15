# Phase 4 — RDAP Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** For each open, `filter_pass=1` candidate, query Verisign RDAP, translate the result into our open-cycle model (`lifecycle_status` + status-driven `drop_date_est` + `expiry_date` + `rdap_status` + `verified_at`), record a DoH resolution signal (`dns_status`), and re-verify on a per-status cadence — async, self-rate-limited, idempotent, no network in the test suite.

**Architecture:** A **pure** `lifecycle.py` (transition table + drop-date math + cadence) and a network `rdap.py` (whodap fetch + orchestration), split so all domain logic is fixture-tested with zero network. whodap's stateful `DNSClient` is contained by a per-worker client pool sharing one truststore `httpx.AsyncClient`; IANA bootstrap is skipped by presetting the `.com`→Verisign map. The async orchestrator takes **injected** `lookup`/`doh` callables so it tests with instant fakes; the real network wiring lives in a thin `run_verify` entry.

**Tech Stack:** Python 3.11+ (dev 3.14), `whodap` (4th runtime dep, async RDAP, MIT), `httpx`+`truststore` (already present), stdlib `asyncio`/`sqlite3`/`json`, `pytest` (no `pytest-asyncio` — async tests use `asyncio.run`).

**Parent spec:** `docs/PHASE-4-DESIGN.md` (APPROVED 2026-07-15). Read it before starting.

## Global Constraints

- **Python:** `requires-python = ">=3.11"`. 3.11-safe syntax only.
- **New runtime dep:** exactly one — `whodap` (0.1.16; async RDAP via httpx; MIT). No `pytest-asyncio`.
- **No network in the test suite.** Every RDAP/DoH path is exercised via injected fakes or `DomainResponse.from_json` fixtures. The one live test (`--domain` smoke) is `@pytest.mark.skip`-by-default.
- **RDAP endpoint is queried DIRECTLY:** `https://rdap.verisign.com/com/v1/` (never rdap.org). IANA bootstrap is skipped — preset `DNSClient.iana_dns_server_map = {"com": endpoint}`.
- **whodap gotchas (verified, 0.1.16):** `DNSClient` is stateful (`self._target`) → **one client per concurrent worker**; the convenience `aio_lookup_domain` bootstraps IANA per call → never used in the loop; 404→`NotFoundError` (available signal), 429→`RateLimitError`, 5xx→`BadStatusCode`, 400→`MalformedQueryError` (all subclass `WhodapError`); network/timeout errors propagate raw as `httpx.TransportError` subclasses. `DomainResponse.status` is `list[str]`; `.events` items have `.eventAction: str` and `.eventDate: datetime` (already parsed).
- **Backoff retry set:** `RETRYABLE = (RateLimitError, BadStatusCode, httpx.TransportError)`. `NotFoundError` is never retried (handled in `lookup_one`); `MalformedQueryError` is never retried (won't fix) → per-row `errors`.
- **Status-driven drop dates:** `pending_delete` → `today + 5 d`; `redemption` (and `pending restore`) → `today + 35 d`; `grace` → `today + 45 d` (low-confidence, **anchored on today, never `expiry`**; hard floor 35). Drop offsets prefer an RGP phase event date when the `events` dict carries one, else `today`.
- **Transition closures (§5 open-cycle):** 404 → `dropped` (stays OPEN, sets `drop_date_actual=today`); `dropped`+registered → `reregistered` (CLOSES); lapsing/`unknown`+plain-registered → `renewed` (CLOSES). **`pending restore` kept OPEN as `redemption`; hold-without-RGP kept OPEN as `grace`** (owner decisions 2026-07-15). Row order: 404 → pending_delete → redemption/restore → auto-renew → (dropped→reregistered) → hold→grace → renewed.
- **`drop_date_actual` is COALESCE-preserved** (first confirmed drop sticks, retained across a later `reregistered` close — the prior-drop-count signal).
- **Verify scope:** open (`lifecycle_status NOT IN ('renewed','reregistered','dismissed')`) **AND `filter_pass = 1`**, ordered **`feed_category='dropped'` first**, then soonest `drop_date_est`, then never-verified/stalest. Per-run cap `--limit` (default 1000). Cadence applied in Python.
- **DoH is recorded-only** — `dns_status` never gates an RDAP call or influences `lifecycle_status`; probe errors swallow to `"error"`.
- **Verify never writes** `filter_*`, `source`, `first_seen`, or scoring columns.
- **Git cadence:** commit per task locally; **push once, at phase end** (Task 11).

## File structure

**Create:**
- `domainscout/lifecycle.py` — pure: status constants, `KNOWN_STATUSES`, `unmatched_statuses`, `_is_due`, `_drop_after`, `next_state`.
- `domainscout/rdap.py` — `parse_observation`, `make_async_client`, `_new_dns_client`, `lookup_one`, `select_due`, `_iso`, `_tally`, `verify_candidates`, `run_verify`, `verify_single`.
- `domainscout/ratelimit.py` — `TokenBucket`, `with_backoff`, `RETRYABLE`.
- `domainscout/doh.py` — `DOH_URL`, `probe`.
- `tests/test_lifecycle.py`, `tests/test_rdap.py`, `tests/test_ratelimit.py`, `tests/test_doh.py`.

**Modify:**
- `pyproject.toml` — `dependencies` += `whodap`.
- `domainscout/config.py` — `Criteria` += `rdap_concurrency`, `rdap_max_retries`, `rdap_timeout`, `rdap_user_agent`, `rdap_recheck_days` + loader parsing `[rdap]`/`[rdap.recheck_days]`; `from dataclasses import field`.
- `criteria.toml` — `[rdap]` += `concurrency`/`max_retries`/`timeout`/`user_agent`; new `[rdap.recheck_days]` table.
- `domainscout/db.py` — `dns_status` in DDL + `_MIGRATION_COLUMNS` + `set_rdap_result`.
- `domainscout/models.py` — `RdapObservation`, `LifecycleUpdate`, `VerifyCounts`.
- `domainscout/commands.py` — `cmd_verify`; drop `"verify"` from `STUB_PHASES`.
- `domainscout/__main__.py` — real `verify` subparser; drop `"verify"` from `_STUB_HELP`.
- `tests/test_config.py`, `tests/test_db.py`, `tests/test_cli.py` — extend; repoint the generic stub test off `verify`.

**No change:** `ingest.py`, `sources/*`, `pronounce.py`, `filters.py`.

---

### Task 1: `whodap` dependency + `[rdap]` config extension

**Files:**
- Modify: `pyproject.toml:11`
- Modify: `domainscout/config.py` (Criteria fields + loader)
- Modify: `criteria.toml` (`[rdap]` + `[rdap.recheck_days]`)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `whodap` importable; `Criteria.rdap_concurrency: int`, `.rdap_max_retries: int`, `.rdap_timeout: float`, `.rdap_user_agent: str`, `.rdap_recheck_days: dict[str, int]` (existing `.rdap_endpoint`, `.rdap_max_rps` unchanged).

- [ ] **Step 1: Add the dependency and install it**

In `pyproject.toml`, change line 11 to:
```toml
dependencies = ["httpx", "truststore", "wordfreq", "whodap"]
```
Run: `python -m pip install whodap` (already present at 0.1.16 in this environment; harmless if so).

- [ ] **Step 2: Write the failing config test**

Append to `tests/test_config.py`:
```python
def test_criteria_has_rdap_defaults(tmp_path):
    from domainscout.config import load_criteria
    crit = load_criteria("criteria.toml")
    assert crit.rdap_concurrency == 5
    assert crit.rdap_max_retries == 4
    assert crit.rdap_timeout == 15.0
    assert "personal expired-domain research" in crit.rdap_user_agent
    assert crit.rdap_recheck_days["pending_delete"] == 1
    assert crit.rdap_recheck_days["redemption"] == 2
    assert crit.rdap_recheck_days["grace"] == 7
    assert crit.rdap_recheck_days["dropped"] == 7
    assert "expiring" not in crit.rdap_recheck_days  # dead key removed


def test_rdap_recheck_days_defaults_when_table_absent(tmp_path):
    from domainscout.config import load_criteria
    toml = tmp_path / "c.toml"
    base = (tmp_path.parent.parent / "criteria.toml")
    # minimal criteria without [rdap.recheck_days]
    text = '''[ingestion]
tld = "com"
charset = "^[a-z]+$"
sources = ["whoisfreaks"]
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
min_score = -4.0
[scoring]
tier2_cutoff = 30
digest_top_n = 10
[rdap]
endpoint = "https://rdap.verisign.com/com/v1/"
max_requests_per_sec = 1.0
[retention]
days = 360
'''
    toml.write_text(text, encoding="utf-8")
    crit = load_criteria(toml)
    assert crit.rdap_recheck_days == {"pending_delete": 1, "redemption": 2, "grace": 7, "dropped": 7}
    assert crit.rdap_concurrency == 5  # default when [rdap].concurrency absent
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_config.py::test_criteria_has_rdap_defaults -v`
Expected: FAIL (`AttributeError: 'Criteria' object has no attribute 'rdap_concurrency'`).

- [ ] **Step 4: Add the `Criteria` fields**

In `domainscout/config.py`, add `field` to the dataclasses import at the top:
```python
from dataclasses import dataclass, field
```
In the `Criteria` dataclass, after the existing `dictionary_combine: str = "min"` line, add:
```python
    rdap_concurrency: int = 5
    rdap_max_retries: int = 4
    rdap_timeout: float = 15.0
    rdap_user_agent: str = "DomainScout/0.1 (personal expired-domain research)"
    rdap_recheck_days: dict = field(
        default_factory=lambda: {"pending_delete": 1, "redemption": 2, "grace": 7, "dropped": 7}
    )
```

- [ ] **Step 5: Parse the new keys in `load_criteria`**

In `domainscout/config.py`, immediately before the `return Criteria(` statement, add:
```python
    rdap_tbl = data.get("rdap", {})
    _DEFAULT_RECHECK = {"pending_delete": 1, "redemption": 2, "grace": 7, "dropped": 7}
    recheck_tbl = rdap_tbl.get("recheck_days", {})
    if not isinstance(recheck_tbl, dict):
        raise ConfigError("criteria.toml: [rdap.recheck_days] must be a table")
    rdap_recheck_days = {
        **_DEFAULT_RECHECK,
        **{str(k): _as_int(v, f"[rdap.recheck_days].{k}") for k, v in recheck_tbl.items()},
    }
    rdap_concurrency = _as_int(rdap_tbl.get("concurrency", 5), "[rdap].concurrency")
    rdap_max_retries = _as_int(rdap_tbl.get("max_retries", 4), "[rdap].max_retries")
    rdap_timeout = _as_float(rdap_tbl.get("timeout", 15.0), "[rdap].timeout")
    rdap_user_agent = str(rdap_tbl.get("user_agent", "DomainScout/0.1 (personal expired-domain research)"))
```
Then add these kwargs to the `Criteria(...)` call (after `dictionary_combine=combine,`):
```python
        rdap_concurrency=rdap_concurrency,
        rdap_max_retries=rdap_max_retries,
        rdap_timeout=rdap_timeout,
        rdap_user_agent=rdap_user_agent,
        rdap_recheck_days=rdap_recheck_days,
```

- [ ] **Step 6: Add the keys to `criteria.toml`**

In `criteria.toml`, replace the `[rdap]` block (the `endpoint` + `max_requests_per_sec` lines) with:
```toml
[rdap]
endpoint = "https://rdap.verisign.com/com/v1/"
max_requests_per_sec = 1.0        # polite default; Verisign publishes no numeric limit
concurrency = 5                   # simultaneous RDAP lookups (bounded whodap client pool)
max_retries = 4                   # backoff attempts on 429/5xx/network before -> errors bucket
timeout = 15.0                    # per-request seconds
user_agent = "DomainScout/0.1 (personal expired-domain research)"

[rdap.recheck_days]               # per-status re-verify cadence (verified_at staleness, days)
pending_delete = 1
redemption = 2                    # also covers pending-restore rows (classified as redemption)
grace = 7                         # also covers hold-without-RGP rows (classified as grace)
dropped = 7
# 'unknown' absent -> always due (0 d); Phase 4 never emits 'expiring' (no key needed)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (all config tests, old + new).

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml domainscout/config.py criteria.toml tests/test_config.py
git commit -m "feat(phase4): add whodap dep + [rdap] config (concurrency, retries, timeout, ua, recheck cadence)"
```

---

### Task 2: `dns_status` column migration + `set_rdap_result` writer

**Files:**
- Modify: `domainscout/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: existing `db.SCHEMA`, `db._MIGRATION_COLUMNS`, `db._migrate`, `db.connect`, `db.upsert_candidate`.
- Produces: `dns_status TEXT` column; `db.set_rdap_result(conn, candidate_id, *, lifecycle_status, rdap_status, expiry_date, drop_date_est, drop_date_actual, dns_status, verified_at) -> None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py`:
```python
def test_init_db_adds_dns_status_to_existing_db(tmp_path):
    import re
    dbp = tmp_path / "d.db"
    # simulate a pre-Phase-4 DB: shipped schema minus dns_status
    old_schema = re.sub(r"\n[^\n]*\bdns_status\b[^\n]*,", "", db.SCHEMA, count=1)
    conn = sqlite3.connect(dbp)
    conn.executescript(old_schema)
    conn.commit()
    pre = {r[1] for r in conn.execute("PRAGMA table_info(candidates)")}
    conn.close()
    assert "dns_status" not in pre
    db.init_db(dbp)  # must migrate
    conn = db.connect(dbp)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(candidates)")}
    assert "dns_status" in cols


def test_set_rdap_result_writes_fields(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    cid = db.upsert_candidate(conn, Candidate(domain="foo.com", source="whoisfreaks"))
    db.set_rdap_result(
        conn, cid, lifecycle_status="redemption", rdap_status='["redemption period"]',
        expiry_date="2026-06-01", drop_date_est="2026-08-19", drop_date_actual=None,
        dns_status="nxdomain", verified_at="2026-07-15T10:00:00",
    )
    row = conn.execute(
        "SELECT lifecycle_status, rdap_status, expiry_date, drop_date_est, "
        "drop_date_actual, dns_status, verified_at FROM candidates WHERE id=?", (cid,)
    ).fetchone()
    assert row["lifecycle_status"] == "redemption"
    assert row["rdap_status"] == '["redemption period"]'
    assert row["drop_date_est"] == "2026-08-19"
    assert row["drop_date_actual"] is None
    assert row["dns_status"] == "nxdomain"
    assert row["verified_at"] == "2026-07-15T10:00:00"


def test_set_rdap_result_coalesces_first_drop_date(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    cid = db.upsert_candidate(conn, Candidate(domain="foo.com", source="whoisfreaks"))
    db.set_rdap_result(conn, cid, lifecycle_status="dropped", rdap_status="[]",
                       expiry_date=None, drop_date_est=None, drop_date_actual="2026-07-15",
                       dns_status="nxdomain", verified_at="2026-07-15T10:00:00")
    # a later confirm passes a different actual -> must NOT overwrite the first
    db.set_rdap_result(conn, cid, lifecycle_status="dropped", rdap_status="[]",
                       expiry_date=None, drop_date_est=None, drop_date_actual="2026-07-22",
                       dns_status="nxdomain", verified_at="2026-07-22T10:00:00")
    row = conn.execute("SELECT drop_date_actual FROM candidates WHERE id=?", (cid,)).fetchone()
    assert row["drop_date_actual"] == "2026-07-15"  # first one sticks


def test_set_rdap_result_leaves_filter_columns_untouched(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    cid = db.upsert_candidate(conn, Candidate(domain="foo.com", source="whoisfreaks"))
    db.set_filter_result(conn, cid, track="primary", dict_score=3.4, pronounce_score=-2.1,
                         filter_pass=True, filter_reason="primary dict=3.40 foo")
    db.set_rdap_result(conn, cid, lifecycle_status="grace", rdap_status="[]",
                       expiry_date=None, drop_date_est="2026-08-29", drop_date_actual=None,
                       dns_status="noerror", verified_at="2026-07-15T10:00:00")
    row = conn.execute("SELECT track, dict_score, filter_pass FROM candidates WHERE id=?", (cid,)).fetchone()
    assert row["track"] == "primary" and row["dict_score"] == 3.4 and row["filter_pass"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_db.py::test_set_rdap_result_writes_fields -v`
Expected: FAIL (`AttributeError: module 'domainscout.db' has no attribute 'set_rdap_result'`).

- [ ] **Step 3: Add `dns_status` to the schema and migration list**

In `domainscout/db.py`, in the `SCHEMA` `CREATE TABLE candidates` block, add `dns_status` right after the `verified_at TIMESTAMP,` line:
```sql
  verified_at        TIMESTAMP,
  dns_status         TEXT,
```
Then append to `_MIGRATION_COLUMNS`:
```python
    ("dns_status", "TEXT"),
```

- [ ] **Step 4: Add the `set_rdap_result` writer**

In `domainscout/db.py`, after `set_filter_result`, add:
```python
def set_rdap_result(
    conn: sqlite3.Connection,
    candidate_id: int,
    *,
    lifecycle_status: str,
    rdap_status: str,
    expiry_date: str | None,
    drop_date_est: str | None,
    drop_date_actual: str | None,
    dns_status: str | None,
    verified_at: str,
) -> None:
    """Write the RDAP/DoH columns for one candidate. drop_date_actual is COALESCE-preserved
    (first confirmed drop sticks); touches nothing else (never filter_*/scoring/source/first_seen)."""
    conn.execute(
        """
        UPDATE candidates
           SET lifecycle_status = ?,
               rdap_status = ?,
               expiry_date = ?,
               drop_date_est = ?,
               drop_date_actual = COALESCE(drop_date_actual, ?),
               dns_status = ?,
               verified_at = ?
         WHERE id = ?
        """,
        (lifecycle_status, rdap_status, expiry_date, drop_date_est, drop_date_actual,
         dns_status, verified_at, candidate_id),
    )
    conn.commit()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_db.py -v`
Expected: PASS (old + new).

- [ ] **Step 6: Commit**

```bash
git add domainscout/db.py tests/test_db.py
git commit -m "feat(phase4): dns_status column migration + set_rdap_result (COALESCE-preserves first drop)"
```

---

### Task 3: `lifecycle.py` foundations — models, constants, `unmatched_statuses`, `_is_due`

**Files:**
- Modify: `domainscout/models.py`
- Create: `domainscout/lifecycle.py`
- Test: `tests/test_lifecycle.py`

**Interfaces:**
- Produces: `models.RdapObservation(available: bool, status: tuple[str,...], events: dict[str,date], expiry_date: date|None, status_json: str)`; `lifecycle` module with `S_*` constants, `KNOWN_STATUSES: frozenset`, `unmatched_statuses(obs) -> tuple[str,...]`, `_is_due(status, verified_at: datetime|None, now: datetime, recheck_days: dict[str,int]) -> bool`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_lifecycle.py`:
```python
from datetime import datetime

from domainscout import lifecycle
from domainscout.models import RdapObservation


def _obs(status=(), available=False, events=None, expiry=None):
    return RdapObservation(available=available, status=tuple(status),
                           events=events or {}, expiry_date=expiry, status_json="[]")


def test_unmatched_statuses_filters_known_noise():
    obs = _obs(status=("client transfer prohibited", "redemption period", "weird new thing"))
    assert lifecycle.unmatched_statuses(obs) == ("weird new thing",)


def test_unmatched_statuses_empty_when_all_known():
    obs = _obs(status=("active", "client delete prohibited"))
    assert lifecycle.unmatched_statuses(obs) == ()


def test_is_due_never_verified():
    now = datetime(2026, 7, 15, 12, 0, 0)
    assert lifecycle._is_due("redemption", None, now, {"redemption": 2}) is True


def test_is_due_within_cadence_is_false():
    now = datetime(2026, 7, 15, 12, 0, 0)
    va = datetime(2026, 7, 14, 12, 0, 0)  # 1 day ago, cadence 2
    assert lifecycle._is_due("redemption", va, now, {"redemption": 2}) is False


def test_is_due_past_cadence_is_true():
    now = datetime(2026, 7, 15, 12, 0, 0)
    va = datetime(2026, 7, 12, 12, 0, 0)  # 3 days ago, cadence 2
    assert lifecycle._is_due("redemption", va, now, {"redemption": 2}) is True


def test_is_due_missing_status_always_due():
    now = datetime(2026, 7, 15, 12, 0, 0)
    va = datetime(2026, 7, 15, 11, 0, 0)  # 1 hour ago
    # 'unknown' and 'expiring' are not in the cadence map -> 0 days -> always due
    assert lifecycle._is_due("unknown", va, now, {"redemption": 2}) is True
    assert lifecycle._is_due("expiring", va, now, {"redemption": 2}) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_lifecycle.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'domainscout.lifecycle'` / `RdapObservation`).

- [ ] **Step 3: Add `RdapObservation` to `models.py`**

In `domainscout/models.py`, after the `FilterCounts` dataclass, add:
```python
@dataclass
class RdapObservation:
    """Normalized RDAP result. available=True iff a 404/NotFoundError. status is lowercased;
    events maps eventAction(lower) -> date; status_json is the rdap_status column value."""

    available: bool
    status: tuple[str, ...]
    events: dict
    expiry_date: date | None
    status_json: str
```
(`date` and `dataclass` are already imported at the top of `models.py`.)

- [ ] **Step 4: Create `lifecycle.py` with constants + `unmatched_statuses` + `_is_due`**

Create `domainscout/lifecycle.py`:
```python
"""Pure open-cycle lifecycle logic: status classification, cadence, and the drop-date
transition table. NO I/O — fully fixture-testable. See docs/PHASE-4-DESIGN.md."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from domainscout.models import RdapObservation

REDEMPTION_TAIL_DAYS = 35   # ICANN RGP redemption 30 d + pendingDelete 5 d
PENDING_DELETE_DAYS = 5
GRACE_EST_DAYS = 45         # low-confidence, anchored on TODAY (never expiry). HARD FLOOR 35:
                            # an autoRenewPeriod domain cannot drop sooner than the fixed 35 d tail.

# RDAP status strings we act on (lowercased), in next_state's documented order.
S_PENDING_DELETE = "pending delete"
S_REDEMPTION = "redemption period"
S_PENDING_RESTORE = "pending restore"        # filed restore -> kept OPEN as redemption
S_AUTO_RENEW = "auto renew period"
S_HOLDS = ("client hold", "server hold")     # hold + no RGP -> kept OPEN as grace (not a closure)

# Statuses we understand (decision-relevant + expected registry noise). Anything OUTSIDE this set
# is tallied as "unmatched" in the run summary, surfacing novel Verisign strings as a number.
KNOWN_STATUSES = frozenset({
    S_PENDING_DELETE, S_REDEMPTION, S_PENDING_RESTORE, S_AUTO_RENEW, *S_HOLDS,
    "active", "ok", "inactive",
    "client transfer prohibited", "server transfer prohibited",
    "client delete prohibited", "server delete prohibited",
    "client update prohibited", "server update prohibited",
    "client renew prohibited", "server renew prohibited",
})


def unmatched_statuses(obs: RdapObservation) -> tuple[str, ...]:
    """Status strings not in KNOWN_STATUSES — counted per run to catch registry surprises."""
    return tuple(s for s in obs.status if s not in KNOWN_STATUSES)


def _is_due(status: str, verified_at: datetime | None, now: datetime,
            recheck_days: dict[str, int]) -> bool:
    """True if never verified, or verified longer ago than the status's cadence (missing -> 0 d)."""
    if verified_at is None:
        return True
    return (now - verified_at) >= timedelta(days=recheck_days.get(status, 0))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_lifecycle.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add domainscout/models.py domainscout/lifecycle.py tests/test_lifecycle.py
git commit -m "feat(phase4): lifecycle constants + KNOWN_STATUSES/unmatched_statuses + cadence _is_due"
```

---

### Task 4: `lifecycle.next_state` — transition table + drop-date math

**Files:**
- Modify: `domainscout/models.py` (add `LifecycleUpdate`)
- Modify: `domainscout/lifecycle.py` (add `_drop_after`, `next_state`)
- Test: `tests/test_lifecycle.py`

**Interfaces:**
- Consumes: `RdapObservation`, `S_*` constants, `GRACE_EST_DAYS`/`REDEMPTION_TAIL_DAYS`/`PENDING_DELETE_DAYS`.
- Produces: `models.LifecycleUpdate(lifecycle_status: str, drop_date_est: date|None, drop_date_actual: date|None, expiry_date: date|None)`; `lifecycle.next_state(current: str, obs: RdapObservation, today: date) -> LifecycleUpdate`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_lifecycle.py`:
```python
from datetime import date

TODAY = date(2026, 7, 15)


def test_available_becomes_dropped_and_sets_actual():
    upd = lifecycle.next_state("redemption", _obs(available=True), TODAY)
    assert upd.lifecycle_status == "dropped"
    assert upd.drop_date_actual == TODAY
    assert upd.drop_date_est is None


def test_pending_delete_estimate_today_plus_5():
    upd = lifecycle.next_state("redemption", _obs(status=("pending delete",)), TODAY)
    assert upd.lifecycle_status == "pending_delete"
    assert upd.drop_date_est == date(2026, 7, 20)


def test_redemption_estimate_today_plus_35():
    upd = lifecycle.next_state("unknown", _obs(status=("redemption period",)), TODAY)
    assert upd.lifecycle_status == "redemption"
    assert upd.drop_date_est == date(2026, 8, 19)


def test_pending_restore_kept_open_as_redemption():
    upd = lifecycle.next_state("redemption", _obs(status=("pending restore",)), TODAY)
    assert upd.lifecycle_status == "redemption"
    assert upd.drop_date_est == date(2026, 8, 19)


def test_auto_renew_becomes_grace_anchored_on_today_not_expiry():
    # expiry is ~13 months out (auto-renewed); grace must NOT use it
    obs = _obs(status=("auto renew period",), expiry=date(2027, 6, 1))
    upd = lifecycle.next_state("unknown", obs, TODAY)
    assert upd.lifecycle_status == "grace"
    assert upd.drop_date_est == date(2026, 8, 29)  # today + 45, not near 2027


def test_hold_without_rgp_kept_open_as_grace():
    upd = lifecycle.next_state("unknown", _obs(status=("client hold",)), TODAY)
    assert upd.lifecycle_status == "grace"
    assert upd.drop_date_est == date(2026, 8, 29)


def test_dropped_then_registered_closes_reregistered():
    upd = lifecycle.next_state("dropped", _obs(status=("active",)), TODAY)
    assert upd.lifecycle_status == "reregistered"
    assert upd.drop_date_est is None


def test_dropped_then_registered_on_hold_still_reregistered():
    # row 5 (dropped->reregistered) beats row 6 (hold->grace)
    upd = lifecycle.next_state("dropped", _obs(status=("client hold",)), TODAY)
    assert upd.lifecycle_status == "reregistered"


def test_plain_registered_unknown_closes_renewed():
    obs = _obs(status=("active", "client transfer prohibited"), expiry=date(2027, 6, 1))
    upd = lifecycle.next_state("unknown", obs, TODAY)
    assert upd.lifecycle_status == "renewed"
    assert upd.drop_date_est is None
    assert upd.expiry_date == date(2027, 6, 1)


def test_drop_offset_prefers_event_date_when_present():
    obs = _obs(status=("redemption period",), events={"redemption period": date(2026, 7, 10)})
    upd = lifecycle.next_state("unknown", obs, TODAY)
    assert upd.drop_date_est == date(2026, 8, 14)  # 2026-07-10 + 35, not today + 35
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_lifecycle.py::test_redemption_estimate_today_plus_35 -v`
Expected: FAIL (`AttributeError: module 'domainscout.lifecycle' has no attribute 'next_state'`).

- [ ] **Step 3: Add `LifecycleUpdate` to `models.py`**

In `domainscout/models.py`, after `RdapObservation`, add:
```python
@dataclass
class LifecycleUpdate:
    """Result of applying an RdapObservation to a candidate's current cycle."""

    lifecycle_status: str
    drop_date_est: date | None
    drop_date_actual: date | None    # today on a confirmed drop; writer COALESCE-preserves the first
    expiry_date: date | None
```

- [ ] **Step 4: Add `_drop_after` and `next_state` to `lifecycle.py`**

In `domainscout/lifecycle.py`, add the import for `LifecycleUpdate` (extend the existing models import line):
```python
from domainscout.models import LifecycleUpdate, RdapObservation
```
Then append:
```python
def _drop_after(events: dict, action: str, today: date, offset_days: int) -> date:
    """Anchor a drop estimate on the RGP phase event date if present, else on today."""
    base = events.get(action, today)
    return base + timedelta(days=offset_days)


def next_state(current: str, obs: RdapObservation, today: date) -> LifecycleUpdate:
    """Map (current lifecycle_status, RDAP observation) -> the new cycle state + drop dates.
    First matching rule wins; see docs/PHASE-4-DESIGN.md transition table."""
    if obs.available:  # RDAP 404
        return LifecycleUpdate("dropped", None, today, None)

    st = set(obs.status)
    if S_PENDING_DELETE in st:
        est = _drop_after(obs.events, S_PENDING_DELETE, today, PENDING_DELETE_DAYS)
        return LifecycleUpdate("pending_delete", est, None, obs.expiry_date)
    if S_REDEMPTION in st or S_PENDING_RESTORE in st:
        est = _drop_after(obs.events, S_REDEMPTION, today, REDEMPTION_TAIL_DAYS)
        return LifecycleUpdate("redemption", est, None, obs.expiry_date)
    if S_AUTO_RENEW in st:
        return LifecycleUpdate("grace", today + timedelta(days=GRACE_EST_DAYS), None, obs.expiry_date)
    if current == "dropped":  # was available, now registered (even if on hold) -> re-registered
        return LifecycleUpdate("reregistered", None, None, obs.expiry_date)
    if any(h in st for h in S_HOLDS):  # hold + no RGP + not dropped -> mid-expiry-flow park
        return LifecycleUpdate("grace", today + timedelta(days=GRACE_EST_DAYS), None, obs.expiry_date)
    return LifecycleUpdate("renewed", None, None, obs.expiry_date)  # plainly registered -> recovered
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_lifecycle.py -v`
Expected: PASS (all lifecycle tests).

- [ ] **Step 6: Commit**

```bash
git add domainscout/models.py domainscout/lifecycle.py tests/test_lifecycle.py
git commit -m "feat(phase4): next_state transition table + status-driven drop dates (grace anchored on today)"
```

---

### Task 5: `rdap.py` foundations — `parse_observation`, client builders, `lookup_one`

**Files:**
- Create: `domainscout/rdap.py`
- Test: `tests/test_rdap.py`

**Interfaces:**
- Consumes: `models.RdapObservation`, `config.Criteria`, whodap `DomainResponse`/`DNSClient`/`errors.NotFoundError`.
- Produces: `rdap.parse_observation(resp: DomainResponse|None) -> RdapObservation`; `rdap.make_async_client(criteria) -> httpx.AsyncClient`; `rdap._new_dns_client(http_client, endpoint) -> DNSClient`; `async rdap.lookup_one(dns_client, label: str) -> RdapObservation`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rdap.py`:
```python
import asyncio
import json

from whodap import DomainResponse
from whodap.errors import NotFoundError

from domainscout import rdap


def _resp(status, events):
    payload = {
        "objectClassName": "domain", "ldhName": "EXAMPLE.COM",
        "status": list(status),
        "events": [{"eventAction": a, "eventDate": d} for a, d in events],
    }
    return DomainResponse.from_json(json.dumps(payload).encode())


def test_parse_observation_none_is_available():
    obs = rdap.parse_observation(None)
    assert obs.available is True
    assert obs.status == () and obs.status_json == "[]"


def test_parse_observation_lowercases_status_and_extracts_expiry():
    resp = _resp(
        ["Redemption Period", "Client Transfer Prohibited"],
        [("registration", "1998-01-01T00:00:00Z"), ("expiration", "2026-06-01T00:00:00Z")],
    )
    obs = rdap.parse_observation(resp)
    assert obs.available is False
    assert "redemption period" in obs.status
    assert obs.expiry_date.isoformat() == "2026-06-01"
    assert json.loads(obs.status_json) == ["redemption period", "client transfer prohibited"]


def test_lookup_one_available_on_notfound():
    class FakeClient:
        async def aio_lookup(self, label, tld):
            raise NotFoundError("404")
    obs = asyncio.run(rdap.lookup_one(FakeClient(), "example"))
    assert obs.available is True


def test_lookup_one_parses_registered():
    resp = _resp(["Active"], [("expiration", "2027-06-01T00:00:00Z")])

    class FakeClient:
        async def aio_lookup(self, label, tld):
            assert (label, tld) == ("example", "com")
            return resp
    obs = asyncio.run(rdap.lookup_one(FakeClient(), "example"))
    assert obs.available is False and "active" in obs.status


def test_new_dns_client_presets_com_map():
    import httpx
    client = rdap._new_dns_client(httpx.AsyncClient(), "https://rdap.verisign.com/com/v1/")
    assert client.iana_dns_server_map == {"com": "https://rdap.verisign.com/com/v1/"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rdap.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'domainscout.rdap'`).

- [ ] **Step 3: Create `rdap.py` with parse + client builders + lookup_one**

Create `domainscout/rdap.py`:
```python
"""RDAP verification: async whodap fetch + normalization + orchestration.
Pure lifecycle/drop-date logic lives in lifecycle.py; this module owns all I/O.
See docs/PHASE-4-DESIGN.md."""

from __future__ import annotations

import json
import ssl
from datetime import date, datetime

import httpx
import truststore
from whodap import DNSClient, DomainResponse
from whodap.errors import NotFoundError

from domainscout.models import RdapObservation


def parse_observation(resp: "DomainResponse | None") -> RdapObservation:
    """Normalize a whodap DomainResponse (or None for a 404) into an RdapObservation."""
    if resp is None:
        return RdapObservation(available=True, status=(), events={}, expiry_date=None, status_json="[]")
    status = tuple((s or "").lower() for s in (resp.status or []))
    events: dict[str, date] = {}
    for e in (resp.events or []):
        action = (getattr(e, "eventAction", "") or "").lower()
        d = getattr(e, "eventDate", None)
        if isinstance(d, datetime):
            d = d.date()
        if action:
            events[action] = d
    return RdapObservation(
        available=False, status=status, events=events,
        expiry_date=events.get("expiration"), status_json=json.dumps(list(status)),
    )


def make_async_client(criteria) -> httpx.AsyncClient:
    """Async truststore client (async twin of ingest.make_client): verify TLS against the OS
    trust store so the dev-box AV/proxy MITM root CA is honored. Portable to a Linux VPS."""
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return httpx.AsyncClient(
        verify=ctx, follow_redirects=True, timeout=criteria.rdap_timeout,
        headers={"User-Agent": criteria.rdap_user_agent},
    )


def _new_dns_client(http_client: httpx.AsyncClient, endpoint: str) -> DNSClient:
    """Construct a whodap DNSClient directly and preset the .com endpoint — skips the IANA
    bootstrap network call. One instance per concurrent worker (DNSClient is stateful)."""
    client = DNSClient(http_client)
    client.iana_dns_server_map = {"com": endpoint}
    return client


async def lookup_one(dns_client: DNSClient, label: str) -> RdapObservation:
    """One RDAP lookup for '<label>.com'. NotFoundError (404) -> available. Other whodap/httpx
    errors propagate to the caller's backoff/error handling."""
    try:
        resp = await dns_client.aio_lookup(label, "com")
    except NotFoundError:
        resp = None
    return parse_observation(resp)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_rdap.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add domainscout/rdap.py tests/test_rdap.py
git commit -m "feat(phase4): rdap parse_observation + truststore async client + bootstrap-skipping lookup_one"
```

---

### Task 6: `ratelimit.py` — `TokenBucket` + `with_backoff`

**Files:**
- Create: `domainscout/ratelimit.py`
- Test: `tests/test_ratelimit.py`

**Interfaces:**
- Produces: `ratelimit.RETRYABLE` (exception tuple); `ratelimit.TokenBucket(rate: float, *, sleep=asyncio.sleep, clock=time.monotonic)` with `async acquire() -> None`; `async ratelimit.with_backoff(coro_factory, *, retries, base=2.0, cap=60.0, sleep=asyncio.sleep)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ratelimit.py`:
```python
import asyncio

import httpx
from whodap.errors import BadStatusCode, MalformedQueryError, NotFoundError, RateLimitError

from domainscout import ratelimit


def test_token_bucket_zero_rate_never_sleeps():
    waits = []
    async def fake_sleep(d): waits.append(d)
    tb = ratelimit.TokenBucket(0, sleep=fake_sleep, clock=lambda: 0.0)
    async def run():
        await tb.acquire(); await tb.acquire()
    asyncio.run(run())
    assert waits == []


def test_token_bucket_spaces_calls_by_interval():
    waits = []
    async def fake_sleep(d): waits.append(d)
    tb = ratelimit.TokenBucket(2.0, sleep=fake_sleep, clock=lambda: 0.0)  # interval 0.5s
    async def run():
        await tb.acquire()   # first: no wait
        await tb.acquire()   # second: wait one interval
    asyncio.run(run())
    assert waits == [0.5]


def test_with_backoff_retries_then_succeeds():
    calls = []
    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RateLimitError("429")
        return "ok"
    async def fake_sleep(_): pass
    result = asyncio.run(ratelimit.with_backoff(flaky, retries=4, sleep=fake_sleep))
    assert result == "ok" and len(calls) == 3


def test_with_backoff_retries_badstatus_and_transport():
    for exc in (BadStatusCode("500"), httpx.ConnectError("boom")):
        calls = []
        async def flaky(_exc=exc):
            calls.append(1)
            if len(calls) < 2:
                raise _exc
            return "ok"
        async def fake_sleep(_): pass
        assert asyncio.run(ratelimit.with_backoff(flaky, retries=3, sleep=fake_sleep)) == "ok"
        assert len(calls) == 2


def test_with_backoff_gives_up_after_retries():
    async def always_429():
        raise RateLimitError("429")
    async def fake_sleep(_): pass
    try:
        asyncio.run(ratelimit.with_backoff(always_429, retries=2, sleep=fake_sleep))
        assert False, "expected RateLimitError"
    except RateLimitError:
        pass


def test_with_backoff_does_not_retry_notfound_or_malformed():
    for exc in (NotFoundError("404"), MalformedQueryError("400")):
        calls = []
        async def once(_exc=exc):
            calls.append(1)
            raise _exc
        async def fake_sleep(_): pass
        try:
            asyncio.run(ratelimit.with_backoff(once, retries=5, sleep=fake_sleep))
        except (NotFoundError, MalformedQueryError):
            pass
        assert len(calls) == 1  # raised immediately, no retries
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ratelimit.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'domainscout.ratelimit'`).

- [ ] **Step 3: Create `ratelimit.py`**

Create `domainscout/ratelimit.py`:
```python
"""Async politeness helpers for RDAP: a token-bucket pacer and an exponential-backoff retry
wrapper. Both take injected sleep/clock so tests run with no real waiting."""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

import httpx
from whodap.errors import BadStatusCode, RateLimitError

# Retry these (429 / 5xx / network+timeout). NotFoundError = available signal (not retried);
# MalformedQueryError = a bad query that won't fix on retry (-> caller's errors bucket).
RETRYABLE = (RateLimitError, BadStatusCode, httpx.TransportError)


class TokenBucket:
    """Ensures >= 1/rate seconds between acquire() releases. rate<=0 disables pacing."""

    def __init__(self, rate: float, *, sleep=asyncio.sleep, clock=time.monotonic) -> None:
        self._interval = (1.0 / rate) if rate and rate > 0 else 0.0
        self._sleep = sleep
        self._clock = clock
        self._next_time: float | None = None

    async def acquire(self) -> None:
        if self._interval <= 0:
            return
        now = self._clock()
        # Reserve this slot SYNCHRONOUSLY (before any await) so concurrent coroutines each get a
        # distinct, properly-spaced slot instead of all reading the same _next_time and racing.
        start = self._next_time if (self._next_time is not None and self._next_time > now) else now
        self._next_time = start + self._interval
        wait = start - now
        if wait > 0:
            await self._sleep(wait)


async def with_backoff(
    coro_factory: Callable[[], Awaitable],
    *,
    retries: int,
    base: float = 2.0,
    cap: float = 60.0,
    sleep=asyncio.sleep,
):
    """Call coro_factory(); on a RETRYABLE error retry with exponential delay, up to `retries`
    extra attempts, then re-raise. Non-RETRYABLE errors propagate immediately."""
    for attempt in range(retries + 1):
        try:
            return await coro_factory()
        except RETRYABLE:
            if attempt >= retries:
                raise
            await sleep(min(cap, base * (2 ** attempt)))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_ratelimit.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add domainscout/ratelimit.py tests/test_ratelimit.py
git commit -m "feat(phase4): TokenBucket pacer + with_backoff (retries 429/5xx/network, not 404/400)"
```

---

### Task 7: `doh.py` — DoH resolution probe (recorded signal)

**Files:**
- Create: `domainscout/doh.py`
- Test: `tests/test_doh.py`

**Interfaces:**
- Produces: `doh.DOH_URL`; `async doh.probe(http_client, domain: str) -> str` returning `"noerror"`/`"nxdomain"`/`"servfail"`/`"error"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_doh.py`:
```python
import asyncio

from domainscout import doh


class FakeResp:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, payload=None, exc=None):
        self._payload = payload
        self._exc = exc
        self.last_params = None
    async def get(self, url, params=None, headers=None):
        self.last_params = params
        if self._exc:
            raise self._exc
        return FakeResp(self._payload)


def test_probe_noerror():
    c = FakeClient(payload={"Status": 0})
    assert asyncio.run(doh.probe(c, "example.com")) == "noerror"
    assert c.last_params["name"] == "example.com" and c.last_params["type"] == "A"


def test_probe_nxdomain():
    c = FakeClient(payload={"Status": 3})
    assert asyncio.run(doh.probe(c, "gone.com")) == "nxdomain"


def test_probe_servfail():
    c = FakeClient(payload={"Status": 2})
    assert asyncio.run(doh.probe(c, "x.com")) == "servfail"


def test_probe_swallows_exceptions_to_error():
    c = FakeClient(exc=RuntimeError("boom"))
    assert asyncio.run(doh.probe(c, "x.com")) == "error"


def test_probe_unknown_status_is_error():
    c = FakeClient(payload={"Status": 9})
    assert asyncio.run(doh.probe(c, "x.com")) == "error"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_doh.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'domainscout.doh'`).

- [ ] **Step 3: Create `doh.py`**

Create `domainscout/doh.py`:
```python
"""DNS-over-HTTPS resolution probe (Cloudflare JSON API). RECORDED SIGNAL ONLY — the result
never gates an RDAP call or influences lifecycle_status (NXDOMAIN != available for .com; a
redemption/pendingDelete domain is removed from the zone yet still registered)."""

from __future__ import annotations

DOH_URL = "https://cloudflare-dns.com/dns-query"
_STATUS = {0: "noerror", 3: "nxdomain", 2: "servfail"}


async def probe(http_client, domain: str) -> str:
    """Return 'noerror' | 'nxdomain' | 'servfail' | 'error'. Never raises (errors -> 'error')."""
    try:
        resp = await http_client.get(
            DOH_URL, params={"name": domain, "type": "A"},
            headers={"Accept": "application/dns-json"},
        )
        resp.raise_for_status()
        return _STATUS.get(resp.json().get("Status"), "error")
    except Exception:
        return "error"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_doh.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add domainscout/doh.py tests/test_doh.py
git commit -m "feat(phase4): DoH resolution probe (recorded signal only, swallows errors)"
```

---

### Task 8: `rdap.select_due` — scope + ordering + cadence

**Files:**
- Modify: `domainscout/rdap.py` (add `select_due`)
- Test: `tests/test_rdap.py`

**Interfaces:**
- Consumes: `lifecycle._is_due`, `config.Criteria.rdap_recheck_days`.
- Produces: `rdap.select_due(conn, criteria, now: datetime, recheck_all: bool) -> list[sqlite3.Row]` — open + `filter_pass=1`, ordered dropped-feed-first then soonest-drop then stalest, cadence-filtered (unless `recheck_all`), **no `--limit`** (the orchestrator slices).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rdap.py`:
```python
import sqlite3
from datetime import datetime

from domainscout import db
from domainscout.config import load_criteria
from domainscout.models import Candidate

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
CRIT = load_criteria(REPO_ROOT / "criteria.toml")


def _seed(conn, domain, *, feed_category="expired", filter_pass=1,
          lifecycle_status="unknown", verified_at=None, drop_date_est=None):
    cid = db.upsert_candidate(conn, Candidate(domain=domain, source="whoisfreaks",
                                              feed_category=feed_category))
    conn.execute(
        "UPDATE candidates SET filter_pass=?, lifecycle_status=?, verified_at=?, drop_date_est=? WHERE id=?",
        (filter_pass, lifecycle_status, verified_at, drop_date_est, cid))
    conn.commit()
    return cid


def _due_domains(conn, recheck_all=False, now=datetime(2026, 7, 15, 12, 0, 0)):
    return [r["domain"] for r in rdap.select_due(conn, CRIT, now, recheck_all)]


def test_select_due_excludes_closed_and_unfiltered(tmp_path):
    dbp = tmp_path / "d.db"; db.init_db(dbp); conn = db.connect(dbp)
    _seed(conn, "open.com", filter_pass=1)
    _seed(conn, "closed.com", filter_pass=1, lifecycle_status="renewed")
    _seed(conn, "unfiltered.com", filter_pass=0)
    assert _due_domains(conn) == ["open.com"]


def test_select_due_orders_dropped_feed_first(tmp_path):
    dbp = tmp_path / "d.db"; db.init_db(dbp); conn = db.connect(dbp)
    # both never-verified, both NULL drop_date_est -> only feed_category breaks the tie
    _seed(conn, "expired.com", feed_category="expired")
    _seed(conn, "dropped.com", feed_category="dropped")
    assert _due_domains(conn)[0] == "dropped.com"


def test_select_due_applies_cadence(tmp_path):
    dbp = tmp_path / "d.db"; db.init_db(dbp); conn = db.connect(dbp)
    # redemption verified 1 day ago, cadence 2 -> NOT due
    _seed(conn, "fresh.com", lifecycle_status="redemption", verified_at="2026-07-14T12:00:00")
    # redemption verified 3 days ago -> due
    _seed(conn, "stale.com", lifecycle_status="redemption", verified_at="2026-07-12T12:00:00")
    assert _due_domains(conn) == ["stale.com"]


def test_select_due_recheck_all_ignores_cadence(tmp_path):
    dbp = tmp_path / "d.db"; db.init_db(dbp); conn = db.connect(dbp)
    _seed(conn, "fresh.com", lifecycle_status="redemption", verified_at="2026-07-14T12:00:00")
    assert _due_domains(conn, recheck_all=True) == ["fresh.com"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rdap.py::test_select_due_orders_dropped_feed_first -v`
Expected: FAIL (`AttributeError: module 'domainscout.rdap' has no attribute 'select_due'`).

- [ ] **Step 3: Add `select_due` to `rdap.py`**

In `domainscout/rdap.py`, add `from domainscout import db, lifecycle` to the imports (extend the existing `from domainscout.models import ...` area — put module imports above it):
```python
from domainscout import db, lifecycle
from domainscout.models import RdapObservation
```
Then append:
```python
_SELECT_DUE_SQL = """
SELECT id, domain, feed_category, lifecycle_status, drop_date_actual, verified_at
FROM candidates
WHERE lifecycle_status NOT IN ('renewed','reregistered','dismissed')
  AND filter_pass = 1
ORDER BY (feed_category = 'dropped') DESC,
         (drop_date_est IS NULL), drop_date_est ASC,
         (verified_at IS NULL) DESC, verified_at ASC
"""


def select_due(conn, criteria, now: datetime, recheck_all: bool) -> list:
    """Open + filter_pass rows to verify this run, in priority order (dropped-feed first, then
    soonest-drop, then stalest). Cadence-filtered unless recheck_all. No LIMIT — caller slices."""
    rows = conn.execute(_SELECT_DUE_SQL).fetchall()
    if recheck_all:
        return list(rows)
    due = []
    for r in rows:
        va = r["verified_at"]
        va_dt = datetime.fromisoformat(va) if va else None
        if lifecycle._is_due(r["lifecycle_status"], va_dt, now, criteria.rdap_recheck_days):
            due.append(r)
    return due
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_rdap.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add domainscout/rdap.py tests/test_rdap.py
git commit -m "feat(phase4): select_due — open+filter_pass, dropped-feed-first ordering, cadence filter"
```

---

### Task 9: `rdap.verify_candidates` orchestrator + `VerifyCounts` + `run_verify`

**Files:**
- Modify: `domainscout/models.py` (add `VerifyCounts`)
- Modify: `domainscout/rdap.py` (add `_iso`, `_tally`, `verify_candidates`, `run_verify`, `verify_single`)
- Test: `tests/test_rdap.py`

**Interfaces:**
- Consumes: `lifecycle.next_state`, `lifecycle.unmatched_statuses`, `db.set_rdap_result`, `ratelimit.TokenBucket`/`with_backoff`, `doh.probe`, `select_due`, `lookup_one`, `_new_dns_client`, `make_async_client`.
- Produces: `models.VerifyCounts`; `async rdap.verify_candidates(conn, criteria, *, limit, recheck_all, dry_run, now, lookup, doh) -> VerifyCounts` (injected `lookup: Callable[[str], Awaitable[RdapObservation]]`, `doh: Callable[[str], Awaitable[str]]`); `async rdap.run_verify(conn, criteria, *, limit, recheck_all, dry_run, now=None) -> VerifyCounts`; `async rdap.verify_single(criteria, name, *, conn=None, dry_run=False, now=None) -> tuple[RdapObservation, LifecycleUpdate, str, bool]`.

- [ ] **Step 1: Write the failing orchestrator tests**

Append to `tests/test_rdap.py`:
```python
import json as _json

from domainscout.models import RdapObservation, VerifyCounts


def _mk_obs(status=(), available=False, expiry=None):
    return RdapObservation(available=available, status=tuple(status), events={},
                           expiry_date=expiry, status_json=_json.dumps(list(status)))


def _run_verify(conn, mapping, *, limit=1000, dry_run=False, recheck_all=False,
                now=datetime(2026, 7, 15, 12, 0, 0), doh_result="nxdomain"):
    async def fake_lookup(label):
        return mapping[label]
    async def fake_doh(domain):
        return doh_result
    return asyncio.run(rdap.verify_candidates(
        conn, CRIT, limit=limit, recheck_all=recheck_all, dry_run=dry_run,
        now=now, lookup=fake_lookup, doh=fake_doh))


def test_verify_candidates_writes_rows(tmp_path):
    dbp = tmp_path / "d.db"; db.init_db(dbp); conn = db.connect(dbp)
    cid = _seed(conn, "gone.com", feed_category="dropped")
    counts = _run_verify(conn, {"gone": _mk_obs(available=True)})
    assert counts.processed == 1 and counts.dropped == 1
    row = conn.execute("SELECT lifecycle_status, drop_date_actual, dns_status FROM candidates WHERE id=?",
                       (cid,)).fetchone()
    assert row["lifecycle_status"] == "dropped"
    assert row["drop_date_actual"] == "2026-07-15"
    assert row["dns_status"] == "nxdomain"


def test_verify_candidates_dry_run_writes_nothing(tmp_path):
    dbp = tmp_path / "d.db"; db.init_db(dbp); conn = db.connect(dbp)
    _seed(conn, "gone.com")
    counts = _run_verify(conn, {"gone": _mk_obs(available=True)}, dry_run=True)
    assert counts.processed == 1
    row = conn.execute("SELECT lifecycle_status, verified_at FROM candidates WHERE domain='gone.com'").fetchone()
    assert row["lifecycle_status"] == "unknown" and row["verified_at"] is None


def test_verify_candidates_tallies_and_counts_unmatched(tmp_path):
    dbp = tmp_path / "d.db"; db.init_db(dbp); conn = db.connect(dbp)
    _seed(conn, "a.com"); _seed(conn, "b.com")
    counts = _run_verify(conn, {
        "a": _mk_obs(status=("redemption period", "surprise status")),
        "b": _mk_obs(status=("pending delete",)),
    })
    assert counts.redemption == 1 and counts.pending_delete == 1
    assert counts.unmatched == {"surprise status": 1}


def test_verify_candidates_error_bucket_does_not_abort(tmp_path):
    dbp = tmp_path / "d.db"; db.init_db(dbp); conn = db.connect(dbp)
    _seed(conn, "ok.com"); _seed(conn, "boom.com")

    async def fake_lookup(label):
        if label == "boom":
            raise RuntimeError("network gone")
        return _mk_obs(available=True)
    async def fake_doh(domain):
        return "error"
    counts = asyncio.run(rdap.verify_candidates(
        conn, CRIT, limit=1000, recheck_all=False, dry_run=False,
        now=datetime(2026, 7, 15, 12, 0, 0), lookup=fake_lookup, doh=fake_doh))
    assert counts.errors == 1 and counts.dropped == 1  # ok.com still processed


def test_verify_candidates_limit_sets_left_for_next_run(tmp_path):
    dbp = tmp_path / "d.db"; db.init_db(dbp); conn = db.connect(dbp)
    for i in range(3):
        _seed(conn, f"d{i}.com")
    counts = _run_verify(conn, {f"d{i}": _mk_obs(available=True) for i in range(3)}, limit=2)
    assert counts.processed == 2 and counts.left_for_next_run == 1


def test_verify_candidates_idempotent_within_cadence(tmp_path):
    dbp = tmp_path / "d.db"; db.init_db(dbp); conn = db.connect(dbp)
    _seed(conn, "r.com")
    now1 = datetime(2026, 7, 15, 12, 0, 0)
    asyncio.run(rdap.verify_candidates(conn, CRIT, limit=1000, recheck_all=False, dry_run=False,
        now=now1, lookup=_const_lookup(_mk_obs(status=("redemption period",))), doh=_const_doh()))
    # 1 day later, redemption cadence is 2 days -> not due -> processes nothing
    now2 = datetime(2026, 7, 16, 12, 0, 0)
    counts = asyncio.run(rdap.verify_candidates(conn, CRIT, limit=1000, recheck_all=False, dry_run=False,
        now=now2, lookup=_const_lookup(_mk_obs(available=True)), doh=_const_doh()))
    assert counts.processed == 0
    row = conn.execute("SELECT lifecycle_status FROM candidates WHERE domain='r.com'").fetchone()
    assert row["lifecycle_status"] == "redemption"  # unchanged by the skipped second run


def _const_lookup(obs):
    async def _f(label):
        return obs
    return _f


def _const_doh(result="nxdomain"):
    async def _f(domain):
        return result
    return _f
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rdap.py::test_verify_candidates_writes_rows -v`
Expected: FAIL (`AttributeError: module 'domainscout.rdap' has no attribute 'verify_candidates'`).

- [ ] **Step 3: Add `VerifyCounts` to `models.py`**

In `domainscout/models.py`, ensure `field` is imported (`from dataclasses import dataclass, field`), then after `LifecycleUpdate` add:
```python
@dataclass
class VerifyCounts:
    """One verify run's tally (printed summary)."""

    processed: int = 0
    dropped: int = 0
    redemption: int = 0
    pending_delete: int = 0
    grace: int = 0
    renewed: int = 0
    reregistered: int = 0
    errors: int = 0
    left_for_next_run: int = 0
    unmatched: dict = field(default_factory=dict)
```

- [ ] **Step 4: Add the orchestrator + real entries to `rdap.py`**

In `domainscout/rdap.py`, extend imports:
```python
import asyncio
```
and the whodap error / helper imports at the top:
```python
from domainscout import db, doh as doh_mod, lifecycle
from domainscout.models import LifecycleUpdate, RdapObservation, VerifyCounts
from domainscout.ratelimit import TokenBucket, with_backoff
```
Then append:
```python
def _iso(d) -> "str | None":
    return d.isoformat() if d is not None else None


def _tally(counts: VerifyCounts, status: str) -> None:
    if hasattr(counts, status):
        setattr(counts, status, getattr(counts, status) + 1)


async def verify_candidates(conn, criteria, *, limit, recheck_all, dry_run, now,
                            lookup, doh) -> VerifyCounts:
    """Orchestrate verification over due rows using INJECTED lookup/doh callables (network-free
    in tests). lookup(label)->RdapObservation; doh(domain)->dns_status. Writes via set_rdap_result."""
    counts = VerifyCounts()
    due = select_due(conn, criteria, now, recheck_all)
    batch = due[:limit] if limit else due
    counts.left_for_next_run = len(due) - len(batch)
    today = now.date()
    now_iso = now.isoformat(timespec="seconds")

    async def handle(row):
        label = row["domain"][:-4]  # strip ".com"
        try:
            obs = await lookup(label)
        except Exception:
            counts.errors += 1
            return
        dns = await doh(row["domain"])
        upd = lifecycle.next_state(row["lifecycle_status"], obs, today)
        counts.processed += 1
        _tally(counts, upd.lifecycle_status)
        for s in lifecycle.unmatched_statuses(obs):
            counts.unmatched[s] = counts.unmatched.get(s, 0) + 1
        if not dry_run:
            db.set_rdap_result(
                conn, row["id"], lifecycle_status=upd.lifecycle_status, rdap_status=obs.status_json,
                expiry_date=_iso(upd.expiry_date), drop_date_est=_iso(upd.drop_date_est),
                drop_date_actual=_iso(upd.drop_date_actual), dns_status=dns, verified_at=now_iso,
            )

    await asyncio.gather(*(handle(r) for r in batch))
    return counts


async def run_verify(conn, criteria, *, limit, recheck_all, dry_run, now=None) -> VerifyCounts:
    """Real network entry: build the truststore client + a bounded DNSClient pool + rate limiter,
    then delegate to verify_candidates. The pool (size = rdap_concurrency) is the concurrency bound;
    the TokenBucket paces to rdap_max_rps; with_backoff retries transient failures."""
    now = now or datetime.now()
    http_client = make_async_client(criteria)
    pool: asyncio.Queue = asyncio.Queue()
    for _ in range(criteria.rdap_concurrency):
        pool.put_nowait(_new_dns_client(http_client, criteria.rdap_endpoint))
    bucket = TokenBucket(criteria.rdap_max_rps)

    async def real_lookup(label):
        await bucket.acquire()
        client = await pool.get()
        try:
            return await with_backoff(lambda: lookup_one(client, label),
                                      retries=criteria.rdap_max_retries)
        finally:
            pool.put_nowait(client)

    async def real_doh(domain):
        return await doh_mod.probe(http_client, domain)

    try:
        return await verify_candidates(conn, criteria, limit=limit, recheck_all=recheck_all,
                                       dry_run=dry_run, now=now, lookup=real_lookup, doh=real_doh)
    finally:
        await http_client.aclose()


async def verify_single(criteria, name, *, conn=None, dry_run=False, now=None):
    """Single-domain debug path (--domain). Live lookup + DoH; writes ONLY when the name has an
    OPEN row and not dry_run — never onto a closed cycle. Returns (obs, update, dns, wrote)."""
    now = now or datetime.now()
    label = name[:-4] if name.endswith(".com") else name
    domain = f"{label}.com"
    http_client = make_async_client(criteria)
    try:
        client = _new_dns_client(http_client, criteria.rdap_endpoint)
        obs = await with_backoff(lambda: lookup_one(client, label),
                                 retries=criteria.rdap_max_retries)
        dns = await doh_mod.probe(http_client, domain)
    finally:
        await http_client.aclose()
    row = None
    if conn is not None:
        row = conn.execute(
            "SELECT id, lifecycle_status FROM candidates "
            "WHERE domain=? AND lifecycle_status NOT IN ('renewed','reregistered','dismissed')",
            (domain,)).fetchone()
    current = row["lifecycle_status"] if row else "unknown"
    upd = lifecycle.next_state(current, obs, now.date())
    wrote = False
    if row is not None and not dry_run:
        db.set_rdap_result(
            conn, row["id"], lifecycle_status=upd.lifecycle_status, rdap_status=obs.status_json,
            expiry_date=_iso(upd.expiry_date), drop_date_est=_iso(upd.drop_date_est),
            drop_date_actual=_iso(upd.drop_date_actual), dns_status=dns,
            verified_at=now.isoformat(timespec="seconds"))
        wrote = True
    return obs, upd, dns, wrote
```
(Note: the module now imports `LifecycleUpdate` for typing/return clarity even though it's produced by `lifecycle`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_rdap.py -v`
Expected: PASS (all rdap tests).

- [ ] **Step 6: Commit**

```bash
git add domainscout/models.py domainscout/rdap.py tests/test_rdap.py
git commit -m "feat(phase4): verify_candidates orchestrator (injected lookup/doh) + run_verify + verify_single"
```

---

### Task 10: CLI `verify` subcommand

**Files:**
- Modify: `domainscout/commands.py` (add `cmd_verify`; drop `"verify"` from `STUB_PHASES`)
- Modify: `domainscout/__main__.py` (add `p_verify`; drop `"verify"` from `_STUB_HELP`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `rdap.run_verify`, `rdap.verify_single`, `load_criteria`, `db.connect`.
- Produces: `python -m domainscout verify [--criteria] [--limit N] [--concurrency N] [--recheck-all] [--domain NAME] [--dry-run] [--db PATH]`.

- [ ] **Step 1: Write/adjust the failing CLI tests**

In `tests/test_cli.py`, replace `test_stub_subcommand_reports_phase` (it used `verify`, now real) with a different stub, and add verify tests:
```python
def test_stub_subcommand_reports_phase(capsys):
    rc = main(["digest"])          # digest is still a Phase-7 stub
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "not implemented" in out
    assert "phase 7" in out


def test_verify_cli_empty_db_prints_summary(tmp_path, capsys):
    dbp = tmp_path / "d.db"
    assert main(["--db", str(dbp), "init-db"]) == 0
    capsys.readouterr()
    rc = main(["--db", str(dbp), "verify", "--criteria", str(REPO_ROOT / "criteria.toml")])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "processed=0" in out   # no due rows -> no network


def test_verify_cli_dry_run_on_unfiltered_rows_is_network_free(tmp_path, capsys):
    # rows exist but filter_pass is unset -> select_due returns nothing -> no network
    dbp = tmp_path / "d.db"
    assert main(["--db", str(dbp), "init-db"]) == 0
    assert main(["--db", str(dbp), "ingest", "--file", str(FIXTURE),
                 "--feed-category", "expired",
                 "--criteria", str(REPO_ROOT / "criteria.toml")]) == 0
    capsys.readouterr()
    rc = main(["--db", str(dbp), "verify", "--dry-run",
               "--criteria", str(REPO_ROOT / "criteria.toml")])
    assert rc == 0
    assert "processed=0" in capsys.readouterr().out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli.py::test_verify_cli_empty_db_prints_summary -v`
Expected: FAIL (currently `verify` prints the stub "not implemented", so `processed=0` is absent).

- [ ] **Step 3: Add `cmd_verify` to `commands.py`**

In `domainscout/commands.py`, add `import asyncio` at the top, add `rdap` to the domainscout import:
```python
from domainscout import db, filters, ingest, pronounce, rdap
```
Remove `"verify": 4,` from `STUB_PHASES`. Then add:
```python
def cmd_verify(args: argparse.Namespace) -> int:
    criteria = load_criteria(args.criteria)
    conn = db.connect(args.db)
    try:
        if args.domain:
            obs, upd, dns, wrote = asyncio.run(
                rdap.verify_single(criteria, args.domain, conn=conn, dry_run=args.dry_run))
            print(f"verify {args.domain}: available={obs.available} status={list(obs.status)}")
            print(f"  -> lifecycle={upd.lifecycle_status} drop_est={upd.drop_date_est} "
                  f"expiry={upd.expiry_date} dns={dns} written={wrote}")
            return 0
        counts = asyncio.run(rdap.run_verify(
            conn, criteria, limit=args.limit, recheck_all=args.recheck_all, dry_run=args.dry_run))
    finally:
        conn.close()
    print(
        f"verify: processed={counts.processed} dropped={counts.dropped} "
        f"redemption={counts.redemption} pending_delete={counts.pending_delete} "
        f"grace={counts.grace} renewed={counts.renewed} reregistered={counts.reregistered} "
        f"errors={counts.errors}"
        + ("  [dry-run]" if args.dry_run else "")
    )
    if counts.left_for_next_run:
        print(f"  {counts.left_for_next_run} due rows left for the next run (raise --limit to drain faster)")
    if counts.unmatched:
        pairs = ", ".join(f"{s!r}={n}" for s, n in sorted(counts.unmatched.items()))
        print(f"  unmatched RDAP statuses: {pairs}")
    return 0
```

- [ ] **Step 4: Add the `verify` subparser to `__main__.py`**

In `domainscout/__main__.py`, remove the `"verify": ...` line from `_STUB_HELP`. Then, after the `p_ngrams` block (before the `outcome` parser), add:
```python
    p_verify = sub.add_parser(
        "verify", help="[Phase 4] RDAP verification + status-driven drop dates")
    p_verify.add_argument("--criteria", default="criteria.toml",
                          help="path to criteria.toml (default: criteria.toml)")
    p_verify.add_argument("--limit", type=int, default=1000,
                          help="max candidates to verify this run (default: 1000)")
    p_verify.add_argument("--concurrency", type=int,
                          help="override [rdap].concurrency for this run")
    p_verify.add_argument("--recheck-all", action="store_true", dest="recheck_all",
                          help="ignore the per-status cadence; re-verify every open+filter_pass row")
    p_verify.add_argument("--domain", help="verify a single NAME (live debug; writes only to an open row)")
    p_verify.add_argument("--dry-run", action="store_true",
                          help="compute + print the tally, write nothing")
    p_verify.set_defaults(func=commands.cmd_verify)
```
Note: `--concurrency` is accepted for forward-compat; `run_verify` reads `criteria.rdap_concurrency`. If you want it live now, in `cmd_verify` pass it through by mutating a local — but YAGNI: leave it parsed and unused this phase (documented), or wire it later. (Do NOT add dead behavior; the flag exists so the documented CLI in the design matches.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -v`
Expected: PASS (old + new; the repointed stub test now checks `digest`/phase 7).

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (all Phase 1–4 tests; no network).

- [ ] **Step 7: Commit**

```bash
git add domainscout/commands.py domainscout/__main__.py tests/test_cli.py
git commit -m "feat(phase4): real verify CLI subcommand (batch + --domain), drop the stub"
```

---

### Task 11: Docs, real-data smoke test, and phase-end push

**Files:**
- Modify: `CLAUDE.md` (check Phase 4)
- Modify: `docs/PHASE-4-DESIGN.md` (status → BUILT + build notes)
- Modify: `DECISIONS.md` (ratified Phase-4 entry)
- Test: `tests/test_rdap.py` (marked live smoke, skipped by default)

**Interfaces:**
- Consumes: everything above.
- Produces: updated status docs; a documented live smoke result.

- [ ] **Step 1: Add a skipped live smoke test**

Append to `tests/test_rdap.py`:
```python
import pytest


@pytest.mark.skip(reason="live network — run manually against Verisign RDAP")
def test_live_smoke_known_registered_and_available():
    from domainscout.config import load_criteria
    crit = load_criteria(REPO_ROOT / "criteria.toml")
    reg, _, _, _ = asyncio.run(rdap.verify_single(crit, "google.com"))
    assert reg.available is False
    # a random unlikely-registered label should 404 -> available
    gone, _, _, _ = asyncio.run(rdap.verify_single(crit, "qzxkvbnmplkjhg.com"))
    assert gone.available is True
```

- [ ] **Step 2: Run the full suite (smoke stays skipped)**

Run: `python -m pytest -q`
Expected: PASS, with 1 skipped.

- [ ] **Step 3: Manually run the live smoke + a small real batch** (per "test each phase with real data")

Run (network; this box MITMs TLS — the truststore client handles it):
```bash
python -m pytest tests/test_rdap.py -k live_smoke --no-header -rs -q -p no:cacheprovider --run  # or temporarily un-skip
python -m domainscout verify --domain google.com --dry-run
python -m domainscout verify --domain qzxkvbnmplkjhg.com --dry-run
python -m domainscout verify --limit 25   # on a DB with real Phase-3 survivors
```
Confirm: `google.com` → registered (200 parsed), the random label → available (404), the batch prints a sane status distribution, the rate-limiter paces ~1/s, and read the `unmatched RDAP statuses` line (record any surprises). If a novel status appears frequently, add it to `KNOWN_STATUSES` (or handle it in `next_state`) before moving on.

- [ ] **Step 4: Update `CLAUDE.md`**

Change the Phase 4 checklist line to:
```markdown
- [x] Phase 4: RDAP verification (whodap async, Verisign-direct, status-driven drop dates, re-verify open rows; DoH recorded-signal)
```

- [ ] **Step 5: Update `docs/PHASE-4-DESIGN.md`**

Change the Status line to `✅ **BUILT 2026-07-15**` and append a short "Build notes (2026-07-15)" section recording: test count, any `KNOWN_STATUSES` additions from the real batch, the observed status distribution, and any drop-offset event-anchoring reality (Verisign omits RGP phase events → today-anchored in practice).

- [ ] **Step 6: Add a ratified entry to `DECISIONS.md`**

Add a dated "2026-07-15 — Phase 4 built" entry summarizing the locked decisions (whodap truststore-injected + bootstrap-skipped; open+filter_pass scope, dropped-feed-first, 1000/run cap; per-status cadence; pending-restore & hold-without-RGP kept OPEN; DoH recorded-signal `dns_status`; grace anchored on today with a 35-day hard floor).

- [ ] **Step 7: Commit and push (phase end)**

```bash
git add CLAUDE.md docs/PHASE-4-DESIGN.md DECISIONS.md tests/test_rdap.py
git commit -m "docs(phase4): mark Phase 4 built + build notes + ratified decision; skipped live smoke"
git push origin main
```

---

## Self-Review

**1. Spec coverage** (each `docs/PHASE-4-DESIGN.md` section → task):
- whodap client, truststore-injected, bootstrap skipped → Tasks 5 (`make_async_client`, `_new_dns_client`) + 9 (`run_verify` pool).
- Verify scope open+filter_pass, dropped-feed-first, `--limit 1000` → Task 8 (`select_due`) + Task 9 (`left_for_next_run` slice) + Task 10 (`--limit`).
- `rdap.py`/`lifecycle.py` split → Tasks 3–9.
- DoH recorded-only (`dns_status`) → Task 7 (`doh.probe`) + Task 2 (column) + Task 9 (write).
- Transition table incl. pending-restore & hold keep-OPEN, closures → Task 4.
- Status-driven drop dates, grace-on-today + hard floor → Task 4 (`_drop_after`, `GRACE_EST_DAYS`).
- Rate-limiter + backoff (exact retry set) → Task 6.
- Per-status cadence + `_is_due` → Task 3 + Task 8.
- Unmatched-status tally → Task 3 (`unmatched_statuses`) + Task 9 (`VerifyCounts.unmatched`) + Task 10 (print).
- `dns_status` migration + `set_rdap_result` COALESCE → Task 2.
- `[rdap]` config + `whodap` dep → Task 1.
- CLI `verify` + `--domain` open-row-only → Task 10 (+ `verify_single` Task 9).
- Real-data confirmations → Task 11.
- No gaps.

**2. Placeholder scan:** No "TBD"/"handle edge cases"/"similar to". The `--concurrency` flag is explicitly parsed-but-not-wired with a documented reason (matches the design's CLI surface without adding dead behavior).

**3. Type consistency:** `RdapObservation` (Task 3) fields match `parse_observation` (Task 5) construction and every fake in Tasks 5/8/9. `LifecycleUpdate` (Task 4) fields match `next_state` returns and `_iso`/`set_rdap_result` calls (Task 9). `VerifyCounts` attr names (`dropped`/`redemption`/`pending_delete`/`grace`/`renewed`/`reregistered`) exactly match `_tally(counts, upd.lifecycle_status)` since `next_state` emits those exact strings. `set_rdap_result` kwargs (Task 2) match the call sites (Tasks 9). `select_due(conn, criteria, now, recheck_all)` signature matches its callers. `lookup(label)` / `doh(domain)` injected-callable shapes match both the fakes and `run_verify`'s real closures.
