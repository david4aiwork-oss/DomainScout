# Phase 4 — RDAP verification: design

**Status:** 📝 **DRAFT — design approved in brainstorm 2026-07-14, pending written-spec review.**
Brainstormed via superpowers; four decisions locked (see *Locked decisions*). Parent design:
`docs/TECHNICAL-DESIGN.md` §4.4 (RDAP verification), §5 (open-cycle schema). Builds on the Phase-2
truststore `httpx` client pattern and the Phase-3 `filter_pass` gate.

**Goal:** For each open, rules-filter-surviving candidate, ask Verisign RDAP what state the domain is in,
and translate that into our open-cycle model: set `lifecycle_status`, a **status-driven** `drop_date_est`,
`expiry_date`, the raw `rdap_status`, and `verified_at`. Re-verify open rows on a per-status cadence so a
confirmed drop (404) flips a row to `dropped` (available now, still OPEN) and a later re-registration (200)
closes the cycle as `reregistered`. Async, self-rate-limited, idempotent, TLS-through-the-OS-trust-store.

---

## Scope

**In scope:**
- Async RDAP lookup against **`rdap.verisign.com/com/v1/` directly** (never the rdap.org aggregator) via
  `whodap` (async, MIT), injecting our own truststore `httpx.AsyncClient`.
- **Status-driven drop-date** computation (redemption +35 d, pendingDelete +5 d; auto-renew grace low-confidence).
- **Open-cycle lifecycle transitions** — the §5 closure logic (`dropped`→`reregistered`, lapsing→`renewed`,
  404→`dropped`), as a **pure** function.
- Our own **rate-limiter** (token bucket at `rdap_max_rps`) + **concurrency** semaphore + **backoff** on 429/5xx.
- **Re-verify cadence** keyed on `verified_at` and current status; per-run politeness cap (`--limit`, default 1000).
- **DoH resolution probe** (Cloudflare `1.1.1.1` JSON API) recorded to a new `dns_status` column — **signal only**.
- Persist `lifecycle_status`, `rdap_status`, `expiry_date`, `drop_date_est`, `drop_date_actual`, `dns_status`,
  `verified_at`. Add `dns_status` via the single-authority migration.
- `verify` CLI (idempotent; `--limit`, `--concurrency`, `--recheck-all`, `--domain`, `--dry-run`).
- `whodap` as the 4th runtime dependency.

**Out of scope (deferred):**
- AI scoring / toxicity / comps → Phase 5. Verify never touches `tier1_score`/`tier2_scores`/…
- **DoH as a gate.** NXDOMAIN can't safely mean "available" for .com (redemption/pendingDelete domains are
  removed from the zone yet still registered), so DoH never short-circuits an RDAP call or sets
  `lifecycle_status` this phase — it is recorded for later analysis only.
- IANA bootstrap discovery across TLDs — we're .com-only with one known endpoint, so we **preset** the
  `.com`→Verisign mapping and skip the bootstrap network call entirely.
- Backorder/auction actions on the computed drop dates → later phases.

---

## Locked decisions (from this brainstorm)

1. **`whodap` client, truststore-injected, bootstrap skipped.** `whodap` (async, MIT) as documented in §4.4.
   Its `DNSClient`/`aio_lookup_domain` accept a pre-built `httpx.AsyncClient`, so we pass our
   `truststore.SSLContext` client (the AV/proxy MITM handling proven in Phase 2). We **preset**
   `DNSClient.iana_dns_server_map = {"com": <endpoint>}` from `criteria.rdap_endpoint` and never call the IANA
   bootstrap (`new_aio_client` would fetch `data.iana.org/rdap/dns.json` on every construction).
2. **Verify scope = open **AND** `filter_pass = 1`, cap `--limit 1000`/run.** RDAP calls are only spent on rows
   that survived Phase 3, ordered by drop-date proximity then staleness. The first run's ~3.5 k backlog drains
   over a few daily runs; the truncation is logged, not silent.
3. **Two modules: `rdap.py` (async I/O) + `lifecycle.py` (pure).** The auditable domain logic — the transition
   table and the drop-date math — lives in a network-free module tested entirely on fixtures.
4. **DoH = recorded signal only.** A cheap Cloudflare DoH A-record probe runs alongside RDAP and stores
   `noerror`/`nxdomain`/`servfail`/`error` in `dns_status`. It never gates an RDAP call and never influences
   `lifecycle_status` — RDAP is the sole source of lifecycle truth.

### whodap gotchas (verified against whodap 0.1.16 — captured so the plan doesn't rediscover them)

- **`DNSClient` is stateful (`self._target`)** — it is mutated per call and read while following related
  hrefs, so **one `DNSClient` instance cannot serve concurrent `aio_lookup` calls.** We use **one client per
  concurrent worker**, all sharing the single `httpx.AsyncClient` (which *is* concurrency-safe).
- **`aio_lookup_domain(...)` bootstraps IANA per call** (via `new_aio_client`) → never call it in the loop.
- **404 → `whodap.errors.NotFoundError`** (raised at recursion depth 0) — this is our "available" signal.
  **429 → `whodap.errors.RateLimitError`** — our backoff trigger.
- **`DomainResponse`** exposes `.status` (`list[str]`) and `.events` (objects with `.eventAction: str` and
  `.eventDate: datetime`, already parsed). Constructible from fixture JSON via `DomainResponse.from_json(b)`
  → the parse path is fully unit-testable with **no network**.

---

## Architecture

New / changed modules (TDD §4.1 layout):

```
domainscout/
  rdap.py                   # Phase 4: RdapObservation · parse_observation · async lookup + verify_candidates (DB loop)
  lifecycle.py              # PURE: next_state (transition table) · drop-date math · cadence (_is_due)
  doh.py                    # small: async DoH A-probe -> dns_status string (Cloudflare 1.1.1.1 JSON API)
  ratelimit.py              # small: async TokenBucket(rate) + retry/backoff helper
  db.py                     # +dns_status column (migration) + set_rdap_result() helper        (modified)
  models.py                 # +RdapObservation, +LifecycleUpdate, +VerifyCounts                (modified)
  commands.py, __main__.py  # real `verify` subcommand (replaces the Phase-4 stub)             (modified)
  config.py, criteria.toml  # +[rdap] concurrency/max_retries/timeout/user_agent/[rdap.recheck_days] (modified)
  pyproject.toml            # dependencies += "whodap"                                          (modified)
```

### Pure core — `lifecycle.py` (no I/O; the auditable heart)

```python
@dataclass
class LifecycleUpdate:
    lifecycle_status: str
    drop_date_est: date | None
    drop_date_actual: date | None    # today on a confirmed drop; writer COALESCE-preserves the first one
    expiry_date: date | None

REDEMPTION_TAIL_DAYS = 35            # ICANN RGP redemption 30 d + pendingDelete 5 d
PENDING_DELETE_DAYS  = 5
GRACE_EST_DAYS       = 45            # low-confidence, anchored on TODAY (see note): rough days-to-drop
                                     # while in the registrar-variable auto-renew grace; refined on recheck

def next_state(current: str, obs: "RdapObservation", today: date) -> LifecycleUpdate: ...
```

**Transition table** (`current` = the row's existing `lifecycle_status`; keys checked top-to-bottom):

| condition | → `lifecycle_status` | `drop_date_est` | cycle |
|---|---|---|---|
| `obs.available` (RDAP 404) | `dropped` | cleared | **stays OPEN** — the live hand-register opportunity |
| registered, `pending delete` in status | `pending_delete` | `pd_event or today` **+ 5 d** | open (highest confidence) |
| registered, `redemption period` in status | `redemption` | `rp_event or today` **+ 35 d** | open (high confidence) |
| registered, `auto renew period` in status | `grace` | `today + 45 d` (low conf.) | open |
| registered, plain, **`current == 'dropped'`** | `reregistered` | cleared | **CLOSES** — someone re-registered |
| registered, plain (any other open `current`) | `renewed` | cleared | **CLOSES** — recovered/renewed |

Notes:
- **Event dates preferred when present** (§4.4): if the RDAP `events` array carries a `redemption period` /
  `pending delete` action date, anchor the offset on it; Verisign usually omits these, so we fall back to
  `today` (observation date) — a conservative upper bound on the true drop.
- `expiry_date` is taken from the `expiration` event on every registered response (stored as-is).
- **`grace` anchors its estimate on `today`, not `expiry`.** During the auto-renew grace, Verisign has
  already auto-renewed the registry `expiration` **+1 year**, so `expiry` is ~13 months out and useless for the
  drop date. The real drop path from grace is: grace (0–45 d, registrar-variable) → redemption (30 d) →
  pendingDelete (5 d). We can't see remaining grace, so `today + 45 d` is a deliberately rough low-confidence
  placeholder that gets **refined** the moment the row is re-verified into `redemption`/`pending_delete`
  (which then anchor precisely). Same reason `pending_delete`/`redemption` anchor on `today`, not `expiry`.
- **`drop_date_actual`** is returned as `today` only on a 404. The DB writer sets it with
  `COALESCE(drop_date_actual, :actual)`, so the **first** confirmed-drop date sticks and is retained even after
  the cycle later closes as `reregistered` (this non-null history is §5's prior-drop-count quality signal).
- **Why "plain registered from a fresh `unknown`" closes as `renewed`:** the feed is a list of *expired/dropped*
  names; a name RDAP reports as plainly registered with no RGP tail has recovered, so it is not a live
  opportunity. Closing retains it as history and (via the partial unique index) frees a future feed appearance
  to open a fresh cycle. `expiring` remains a valid schema state but Phase 4 does not emit it — RDAP always
  gives us the more specific `grace`/`redemption`/`pending_delete`.

**Cadence** (pure): `_is_due(status, verified_at, now, recheck_days) -> bool` — `True` if `verified_at is None`
or `now - verified_at >= recheck_days[status]` (an `unknown`/missing entry ⇒ 0 days ⇒ always due).

### RDAP I/O — `rdap.py`

```python
@dataclass
class RdapObservation:
    available: bool                  # True iff RDAP 404 / NotFoundError
    status: tuple[str, ...]          # normalized (lowercased) RDAP status list
    events: dict[str, date]          # eventAction(lower) -> date
    expiry_date: date | None
    status_json: str                 # json.dumps(list(status)) -> the rdap_status column

def parse_observation(resp: "DomainResponse | None") -> RdapObservation:
    # resp is None for a 404 (NotFoundError caught in lookup_one)
    ...

def make_async_client(criteria) -> httpx.AsyncClient:
    # async twin of ingest.make_client: truststore.SSLContext, follow_redirects=True,
    # timeout=criteria.rdap_timeout, headers={"User-Agent": criteria.rdap_user_agent}

async def lookup_one(dns_client, label: str) -> RdapObservation:
    # dns_client.aio_lookup(label, "com"); on NotFoundError -> available; RateLimitError bubbles to backoff

async def verify_candidates(conn, criteria, *, limit=1000, concurrency=None,
                            recheck_all=False, dry_run=False, now=None,
                            lookup=lookup_one, doh=doh.probe) -> VerifyCounts:
    # 1) select_due(conn, criteria, now, recheck_all, limit) -> rows
    # 2) shared httpx.AsyncClient + pool of `concurrency` DNSClients (map preset, no bootstrap)
    # 3) per row under semaphore + TokenBucket(rdap_max_rps):
    #       obs   = await backoff(lambda: lookup(client, label))   # retries RateLimitError/5xx
    #       dns   = await doh(http_client, domain)                 # recorded only; never gates
    #       upd   = lifecycle.next_state(row.lifecycle_status, obs, now)
    #       if not dry_run: db.set_rdap_result(conn, row.id, ...upd..., rdap_status=obs.status_json,
    #                                           dns_status=dns, verified_at=now)
    # 4) tally VerifyCounts; log() when the backlog exceeds `limit`
```

- **`lookup`/`doh` are injected** (defaults are the real network fns) so the orchestrator is tested with fakes,
  **zero network** in the suite.
- **DB writes are synchronous** `sqlite3` calls interleaved between `await`s — safe because the whole phase runs
  single-threaded under one `asyncio` event loop (the CLI handler calls `asyncio.run(verify_candidates(...))`).
- **Per-row failures** (unexpected RDAP/DoH errors after retries) are logged and counted as `errors`, then
  skipped — one bad domain never aborts the batch.

`select_due(conn, criteria, now, recheck_all, limit)` runs:
```sql
SELECT id, domain, lifecycle_status, drop_date_actual, verified_at
FROM candidates
WHERE lifecycle_status NOT IN ('renewed','reregistered','dismissed')   -- open
  AND filter_pass = 1                                                  -- Phase-3 survivors only
ORDER BY (drop_date_est IS NULL), drop_date_est ASC, (verified_at IS NULL) DESC, verified_at ASC
```
then keeps rows where `recheck_all or lifecycle._is_due(status, verified_at, now, criteria.rdap_recheck_days)`,
taking the first `limit`. (Cadence is applied in Python for clarity/testability; the ordering pushes
soonest-dropping and never-verified rows first.)

### Rate-limit / backoff — `ratelimit.py`

- `class TokenBucket: def __init__(self, rate: float); async def acquire(self) -> None` — refills at `rate`
  tokens/sec (capacity 1); `rdap_max_rps` (default **1.0**) is the real throughput throttle.
- `async def with_backoff(coro_factory, *, retries, base=2.0, cap=60.0, sleep=asyncio.sleep)` — retries on
  `RateLimitError`/5xx with exponential delay (`min(cap, base * 2**n)`); `sleep` is injected so tests use a
  fake clock. The concurrency **semaphore** (default 5) bounds simultaneous sockets; with 1 rps it mostly
  overlaps latency — raising `rdap_max_rps` is what actually speeds a run.

### DoH probe — `doh.py`

```python
DOH_URL = "https://cloudflare-dns.com/dns-query"     # JSON API; Accept: application/dns-json
async def probe(http_client, domain: str) -> str:    # -> "noerror" | "nxdomain" | "servfail" | "error"
    # GET DOH_URL?name={domain}&type=A ; map response "Status": 0->noerror, 3->nxdomain, 2->servfail; any exc->error
```
Reuses the shared truststore `httpx.AsyncClient`. Errors are swallowed to `"error"` — DoH never aborts a row.

---

## Schema changes (`db.py`) — single-authority migration

Every RDAP column already exists from Phase 1 (`expiry_date`, `drop_date_est`, `drop_date_actual`,
`lifecycle_status`, `rdap_status`, `verified_at`). Phase 4 adds **one** new column:

| column | type | note |
|--------|------|------|
| `dns_status` | TEXT | DoH A-probe result: `noerror`/`nxdomain`/`servfail`/`error` — recorded signal, never gates |

Both paths converge (identical to the Phase-3 mechanism): `dns_status` is in the `CREATE TABLE` DDL for fresh
DBs and appended to `_MIGRATION_COLUMNS` for existing DBs, so `init_db`'s idempotent `_migrate` (PRAGMA-guarded
`ALTER TABLE`) adds it. `init_db` stays the single schema authority; `verify` assumes `init-db` was run.

```python
def set_rdap_result(conn, candidate_id, *, lifecycle_status, rdap_status, expiry_date,
                    drop_date_est, drop_date_actual, dns_status, verified_at) -> None:
    # UPDATE candidates SET lifecycle_status=?, rdap_status=?, expiry_date=?, drop_date_est=?,
    #        drop_date_actual = COALESCE(drop_date_actual, ?),   -- first confirmed drop sticks
    #        dns_status=?, verified_at=?  WHERE id=?
```
Touches only the RDAP/DoH columns — never `filter_*`, `source`, `first_seen`, or scoring columns.

---

## CLI

```
python -m domainscout verify [--criteria criteria.toml] [--limit 1000] [--concurrency 5]
                             [--recheck-all] [--domain NAME] [--dry-run] [--db data/domainscout.db]
```
- Default: verify due open+`filter_pass` rows, newest-drop / stalest first, up to `--limit`.
- `--recheck-all` ignores the cadence (re-verify every open+filter_pass row up to `--limit`).
- `--domain NAME` — one live lookup, print the parsed observation + computed `LifecycleUpdate` (debug path;
  writes only if the domain already exists as a row and `--dry-run` is absent).
- `--dry-run` — compute + print the tally, write nothing.
- Prints a per-run summary: `verify: processed=N dropped=a redemption=b pending_delete=c grace=d
  renewed=e reregistered=f errors=g` and, when the due backlog exceeded `--limit`, a `log()` line naming how
  many rows were left for the next run.

---

## Config & dependency changes

- **`pyproject.toml`:** `dependencies = ["httpx", "truststore", "wordfreq", "whodap"]` (4th runtime dep).
- **`criteria.toml`** — extend `[rdap]` (the `endpoint` + `max_requests_per_sec` keys already exist):
  ```toml
  [rdap]
  endpoint = "https://rdap.verisign.com/com/v1/"
  max_requests_per_sec = 1.0
  concurrency = 5
  max_retries = 4
  timeout = 15.0
  user_agent = "DomainScout/0.1 (personal expired-domain research)"

  [rdap.recheck_days]        # per-status re-verify cadence (verified_at staleness)
  pending_delete = 1
  redemption = 2
  grace = 7
  dropped = 7
  expiring = 7
  # 'unknown' is intentionally absent -> always due (0 days)
  ```
- **`config.py`:** add to `Criteria` (all with sane defaults when keys are absent, for backward-compat):
  `rdap_concurrency: int = 5`, `rdap_max_retries: int = 4`, `rdap_timeout: float = 15.0`,
  `rdap_user_agent: str = "DomainScout/0.1 (personal expired-domain research)"`,
  `rdap_recheck_days: dict[str, int]` (parsed from `[rdap.recheck_days]`; default the table above).
  Existing `rdap_endpoint` / `rdap_max_rps` are unchanged.

---

## Testing strategy (TDD: red → green → commit per task)

- **`parse_observation`** — fixtures built via `DomainResponse.from_json`: redemption, pendingDelete,
  auto-renew, plain-active; plus `None` (404) → `available=True`. Assert normalized status, events dict,
  `expiry_date`, and `status_json`.
- **`next_state` transition matrix** (the auditable core) — every table row, with special attention to the two
  closures: `dropped` + 200 → `reregistered`; `redemption`/`grace`/`unknown` + plain-200-future-expiry →
  `renewed`; any-open + 404 → `dropped` with `drop_date_actual = today`.
- **Drop-date math** — redemption → `today + 35 d` (and event-anchored when an RGP event date is present);
  pendingDelete → `today + 5 d`; grace → `today + 45 d` (anchored on today, **not** the auto-renewed expiry);
  active/renewed → est cleared. A regression test pins that grace does **not** use `expiry_date`.
- **`_is_due` cadence** — `verified_at is None` ⇒ due; within cadence ⇒ not due; past cadence ⇒ due;
  `unknown`/missing status ⇒ always due.
- **`TokenBucket` / `with_backoff`** — injected fake `sleep`/clock: bucket paces to `rate`; backoff retries
  `RateLimitError` with exponential delays and gives up after `max_retries`.
- **`select_due`** — temp DB: excludes closed rows, excludes `filter_pass=0`, applies cadence, honors `--limit`,
  orders soonest-drop / never-verified first.
- **`verify_candidates` orchestrator** — injected fake `lookup` + fake `doh` (**zero network**): writes the
  seven fields per row via `set_rdap_result`; a fake 404 sets `drop_date_actual`; re-run within cadence no-ops;
  `--recheck-all` reprocesses; `--dry-run` writes nothing; a per-row exception counts as `error` and does not
  abort the batch.
- **DB migration** — `init_db` on a pre-Phase-4 schema adds `dns_status` idempotently (PRAGMA path).
- **`set_rdap_result`** — writes the RDAP/DoH columns; `drop_date_actual` COALESCE preserves the first
  non-null; leaves `filter_*` / scoring columns untouched.
- **CLI** — `verify --db <tmp> --dry-run` on a seeded DB prints the summary and writes nothing;
  a live `--domain` smoke test is **marked and skipped by default** (the only network-touching test).
- **No network** in the default suite — every RDAP/DoH path is exercised through injected fakes or fixtures.

---

## Build-time real-data confirmations (per "test each phase with real data")

1. **Live single-domain smoke** — `verify --domain <a-known-registered .com>` and `--domain <a-known-available
   .com>` against Verisign through the truststore client: confirm 200-parse and 404→`available`, and that the
   MITM TLS path works async exactly as it did for Phase-2 ingest.
2. **Small real batch** — run `verify --limit 25` on real Phase-3 survivors; eyeball the status distribution
   (how many redemption / pendingDelete / grace / renewed / already-dropped), spot-check a few computed
   `drop_date_est` values against the RDAP `events`/`status`, and confirm the rate-limiter paces politely.
3. **Idempotency + cadence** — re-run immediately: within-cadence rows are skipped (no RDAP calls); `dns_status`
   recorded alongside; `--recheck-all` re-verifies. Confirm a re-registered example (if any) closes correctly.

---

## Self-review

- **Placeholders:** none — every default (concurrency 5, limit 1000, cadence days, grace-est 30 d) is a stated,
  tunable config value; the RGP offsets (35 d / 5 d) are the fixed ICANN registry tail from §4.4.
- **Consistency:** reuses the Phase-2 truststore client pattern (async twin) and the Phase-3 single-authority
  `init_db` migration + UPDATE-only writer pattern; the transition table matches §5's open-cycle closure rules
  (`dropped` OPEN; close only on `reregistered`/`renewed`/`dismissed`); DoH is recorded-only per the locked
  decision; no scoring-column writes (Phase 5 boundary intact).
- **Scope:** one phase — RDAP verify + lifecycle/drop-date + cadence + DoH-signal + persistence + CLI; no AI /
  toxicity / comps bleed-in.
- **Isolation:** pure logic (`next_state`, drop-date, `_is_due`, `parse_observation`) is network-free and fully
  fixture-tested; async I/O (`lookup_one`, `verify_candidates`, `doh.probe`, `TokenBucket`) is injected into the
  orchestrator so the whole phase tests with zero network; whodap's statefulness is contained by the
  one-client-per-worker pool.
