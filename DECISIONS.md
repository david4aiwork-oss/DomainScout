# DomainScout — Decision Log & Research Notes

Canonical record of owner decisions, the research behind them, and proposals still awaiting a call.
CLAUDE.md stays the lean build spec; this file holds the *why* and the numbers.

> **Technical design** (architecture, schema, prior-art survey, cited research): see [`docs/TECHNICAL-DESIGN.md`](docs/TECHNICAL-DESIGN.md).

---

## Decisions (ratified)

### 2026-07-13 — Data source: free-first
Start with **Dynadot drop lists** + **WhoisFreaks free GitHub feed** ([10k domains/day subset](https://github.com/WhoisFreaks/daily-expired-and-dropped-domains)). $0. Upgrade to paid WhoisFreaks only after the pipeline proves it can rank well on free data.
*Rejected for now:* WhoisFreaks paid feeds ($59–70/mo expiring list; see pricing snapshot below); Verisign zone files / CZDS (overkill at this scale).
*Revisit trigger:* free subset too thin to surface enough candidates matching owner criteria.

### 2026-07-13 — AI scoring: provider-agnostic, Anthropic default
Scorer built behind a thin interface — `score(domain, context) → JSON` — with provider/model as config values. Default: **Anthropic** (Haiku 4.5 triage → Sonnet deep scoring), submitted via **Batch API** (50% discount; nightly cron is the textbook batch case). ≈ $8/mo at full volume.
*Considered:* OpenAI (GPT-5.5 family). Verdict: quality adequate either way for this task; cost swing is a few $/mo = noise. If curious later, A/B both providers on the same domains (~$10/mo total) and let Phase 6 auction outcomes pick the winner.
*Billing note:* Claude Pro subscription does **not** include API credits — Phase 5 requires a separate API key (min ~$5 credit purchase).

### 2026-07-13 — Infrastructure: local-first
Run locally on Windows (Task Scheduler, not cron) until stable; then migrate to a cheap VPS (~$5/mo Hetzner CX22 / DO $6). Keep run scripts portable (`python -m ...` entry points) so the move is trivial.

### 2026-07-14 — TDD review: sources reclassified, comps $0 path, schema revised
Ratified after the technical-design survey + a second research pass (see docs/TECHNICAL-DESIGN.md).
- **Data sources** (refines 2026-07-13): **WhoisFreaks free feed** = domain-*names-only* firehose (no dates/category; ~50% .com — filter to .com; lifecycle from RDAP) → hand-register/backorder branch. **Dynadot** contributes its public **expired-*auction* CSV** (`/help/question/download-expired-list`) → bid-at-auction branch. The prior "Dynadot drop lists" phrasing was wrong: Dynadot's public data is auction inventory, not a registry drop list (its "Inactive Domains" page lists only the account's own domains).
- **Comps: $0 path now; $10/mo NameBio Basic kept as a planned upgrade.** Use **NameBio's free API** (RetailStats/TLDStats — real comparable-sale stats, cached CSV, attribution required) + **HumbleWorth** open-source valuation model self-hosted (per-domain value range). Resolves proposal #3 for now; revisit NameBio Basic only if the free tier proves thin. (NameBio's *paid* API forbids pipeline use per its ToS.)
- **Schema: open-cycle identity model** replaces proposal #7's `UNIQUE(domain, drop_date)`. Surrogate `id` PK + partial unique index `UNIQUE(domain) WHERE lifecycle_status NOT IN ('dropped','renewed','reregistered')` (one open cycle per domain) + `lifecycle_status` + `drop_date_est`/`drop_date_actual`. Fixes duplicate rows when a *calculated* drop date is refined, and the renewed/never-dropped case. Detail in TDD §5.
- **RDAP specifics:** query `rdap.verisign.com/com/v1/` directly (not the rdap.org aggregator — 10 req/10s cap); drop date computed status-driven from the fixed 30d RGP + 5d pendingDelete tail (auto-renew grace 0–45d is registrar-variable); async client `whodap` (MIT); build our own rate-limiter/backoff/cache.
- **UI + retention:** add **Phase 8** — a local **Flask/FastAPI** app to view/filter the DB + Phase 6/7 results with write-back. Keep raw feed files + digests **360 days** then auto-prune; the SQLite DB is permanent.
- **Licenses verified:** whoisit (BSD-3), whodap (MIT), domainhunter (BSD-3), spidy (MIT) clean; domainsearcher-app & Williams-Media claim MIT but ship no LICENSE file (patterns-only); domain-watchdog is AGPL (study only); the WhoisFreaks free feed has no stated data-use terms (fine for personal use; clarify before commercial).

### 2026-07-14 — Review round 2: ratifications, schema amendment, FastAPI
Owner reviewed the TDD a second time, ratified the three open items, and raised three concerns + smaller notes — all folded into the TDD (see its v2→v2.1 changelog).

**Ratified:**
- **3.1 Data-source model** — WhoisFreaks free feed (name firehose → hand-register/backorder) + Dynadot public expired-*auction* CSV (→ bid branch). Personal use only pending WhoisFreaks license clarification; revisit before any commercial use.
- **3.2 Comps $0 path** — NameBio free RetailStats/TLDStats (cached CSV, attribution in digest) + HumbleWorth open-source model (hosted endpoint on Windows-local; self-host via Docker on VPS later). Retires proposal #3 (paid NameBio API ToS forbids pipeline use). **CONDITION:** a ~30-min empirical spike against the NameBio free endpoint (~20 keywords, confirm limits + CSV path) precedes Phase-5 prompt design; if it underdelivers, promote the own-comps-table hedge (TDD §9) to a Phase-5 component.
- **3.3 Open-cycle schema — amended** (fixes a real bug the owner caught): `'dropped'` is an **OPEN** state — a dropped-and-available domain is the live hand-register opportunity, so treating it as closed both (a) births every dropped-feed row outside the unique index → daily duplicate rows (`ON CONFLICT` never fires), and (b) models the most actionable candidates as history the moment they arrive. Cycles close only on `reregistered` (RDAP-confirmed 200 after a real drop), `renewed`, or owner `dismissed`. Index predicate → `NOT IN ('renewed','reregistered','dismissed')`; `lifecycle_status NOT NULL DEFAULT 'unknown'` (a NULL escapes the partial index). Ingestion sets `feed_category` from the filename, **not** `lifecycle_status`. Implies Phase 4 **re-verifies** open `dropped` rows (not verify-once) — the RDAP 200 is what flips `dropped → reregistered`.
- **Phase 8 UI = FastAPI + uvicorn** — async-native (matches the aiohttp/httpx stack) + auto API docs. (Was "Flask/FastAPI".)
- **HumbleWorth on Windows-local:** use the hosted endpoint; defer self-hosting to the VPS phase.

**Design refinements folded in (owner's smaller notes):**
- **Charset+length gate moves to ingestion** — permanent invariants, not tunable thresholds; only survivors land in the permanent DB (avoids ~1.8M junk rows/yr). Length ceiling = **secondary max (12), not primary (8)** (else all secondary candidates would be discarded). Per-run counts logged to a new `ingest_log` table. Dictionary/pronounceability stay in Phase 3 (tunable). Re-ingest from the 360-day retained feeds if criteria loosen.
- **`score` split into `score-submit` / `score-collect`** — Batch API is async (hours); a submit-and-wait cron step would hang and break idempotency.
- **Toxicity gate runs between Tier-1 and Tier-2** (on Tier-1 survivors), not on all filter survivors — keeps slow CDX/Safe-Browsing calls off the critical path.
- **Cron timing:** late-morning run (feed has ~1-day lag) so ingestion never races the WhoisFreaks upload.
- **`wordfreq` is frozen at 2024** (author stopped updating; LLM-corpus pollution) — emerging vocab won't register; that's the Tier-2 Google-Trends context's job, not the Phase-3 dictionary gate's.
- **Google Safe Browsing** needs a free-tier Google Cloud API key → added to the credentials/`.env` signup list.

### 2026-07-14 — Phase 2 built: ingestion + `truststore` TLS
Phase 2 (ingestion) built and pushed (plan: `docs/superpowers/plans/2026-07-14-phase-2-ingestion.md`; design/build notes: `docs/PHASE-2-DESIGN.md`). WhoisFreaks free feed is real end-to-end; Dynadot is an interface stub deferred to a "Phase 2b" spec (its public data is an auction CSV with a different schema). Two decisions landed during the build:
- **`truststore` added as a 2nd runtime dependency** (owner-approved). This Windows machine intercepts HTTPS with a private root CA (AV/proxy) trusted by the OS store but absent from `certifi`, so httpx's default verification failed (`CERTIFICATE_VERIFY_FAILED`). `ingest.make_client()` now verifies against the **OS trust store** via `truststore.SSLContext`. Secure (not `verify=False`), portable (Windows store now, Linux system CA store on the VPS), zero cost. Tests unaffected (mock transport, no real TLS).
- **Feed lag is ~3 days, not ~1** (empirical): on 2026-07-14 the newest dated file at the repo root was `2026-07-11` (older dates rotate into `archive/`; `0-latest-*` always present). The `ingest` "yesterday" default will often 404 during the window — handled as warning + skip (exit 0). Cron `--date` should target a few days back (or add a `--latest` mode later). Supersedes the "~1-day lag" note under review-round-2 cron timing for scheduling purposes.
- **Real-data smoke (2026-07-11, 10k names/file):** expired landed 1718, dropped landed 1999; `tld` reject bucket ~50–57% (consistent with ~40–45% .com); idempotent re-run stable (candidates 3717→3717, ingest_log 2 rows).

### 2026-07-14 — Phase 3 built: rules filter + pronounceability calibration
Phase 3 (rules filter) built and pushed (plan: `docs/superpowers/plans/2026-07-14-phase-3-rules-filter.md`; design + build notes: `docs/PHASE-3-DESIGN.md`). `wordfreq` added as the 3rd runtime dep (powers both the dictionary gate and the n-gram training corpus). Log-space trigram pronounceability model shipped as 74 KB package data (`domainscout/pronounce_tables.json`, 47,973 word types). 91 tests pass, no network in the suite. Two decisions landed during the build:
- **Dictionary split-part floor raised ≥2 → ≥3 chars.** `wordfreq` gives 2-letter fragments substantial zipf (`th`=4.2, `ng`=3.9, `aa`=4.01), so a ≥2 floor let consonant-mash clear the dict gate via a bogus split (`thng`→`th`+`ng`, min 3.9 ≥ 3.0). ≥3 kills the noise and loses no genuine multi-word target (real combos use ≥3-char words). Regression-tested.
- **`pronounce_min_score` = −4.0, a MASH-ONLY gate; "~50–200/day" reclassified as a post-Tier-1 target** (owner decision, revises CLAUDE.md/PHASE-3-DESIGN.md). Calibrated on the 2026-07-11 feed (3,717 candidates): the **dict gate alone passes 472** (>200 already); real expired .coms are overwhelmingly pronounceable (score median −2.94, p5 −4.04), so the pronounceability OR-gate is a wide net; and the trigram model **can't separate borderline invented from borderline mash** (good `zylo` −3.88 = mash `vgkxq` −3.88). Hitting ~200 (floor ≈ −2.2) would reject exactly the invented names the secondary track exists to catch. So −4.0 removes only unambiguous keyboard-mash (`xqzk` −4.23, `qwrtz` −4.11; ~6%), keeps all legitimate invented/geo names, and leaves volume control to the downstream **Tier-1 (Haiku) triage**. Survivors at −4.0: 3,498/day (primary 1,162 / secondary 2,336; 219 rejected). One-line `criteria.toml` tunable; Phase-6 outcome loop can retune. *Revisit trigger:* if Tier-1 cost at ~3.5k/day proves too high, consider a tighter secondary-track floor or lower `zipf_min` rather than one global floor.

### 2026-07-15 — Phase 4 built: RDAP verification
Phase 4 (RDAP verification) built (plan: `docs/superpowers/plans/2026-07-15-phase-4-rdap-verification.md`;
design + build notes: `docs/PHASE-4-DESIGN.md`). `whodap` added as the 4th runtime dependency. 141 tests pass, zero
network in the suite, plus 1 skipped live smoke (`test_live_smoke_known_registered_and_available`,
`tests/test_rdap.py`) confirmed manually against real Verisign RDAP. Decisions locked during the brainstorm/build:
- **`whodap` client, truststore-injected, IANA bootstrap skipped.** Our own truststore `httpx.AsyncClient` (the
  Phase-2 MITM-handling pattern) is passed into `whodap`; the `.com`→`rdap.verisign.com/com/v1/` mapping is
  **preset** from `criteria.rdap_endpoint`, so the per-call IANA bootstrap network fetch is never made.
- **Verify scope = open AND `filter_pass = 1`, dropped-feed-first, capped `--limit 1000`/run.** RDAP calls are
  spent only on rows that survived Phase 3, ordered dropped-feed-category first (may be hand-registerable *now*),
  then soonest-drop / stalest-verified; the ~3.5k backlog drains over a few daily runs, truncation logged not silent.
- **Per-status re-verify cadence** (`pending_delete`=1d, `redemption`=2d, `grace`=7d, `dropped`=7d; `unknown`/missing
  ⇒ always due) keeps a confirmed drop (404) or a later re-registration (200) from going stale between runs.
- **`pending restore` and hold-without-RGP kept OPEN** (owner decisions): a filed restore watches one more cadence
  as `redemption` rather than closing prematurely as `renewed`; a `client hold`/`server hold` with no RGP status
  classifies as `grace` (OPEN), not a closure — some registrars park expired-in-grace domains on hold before RGP
  statuses appear, and closing those would forfeit the opportunity window.
- **DoH is a recorded signal only** (`dns_status` column: `noerror`/`nxdomain`/`servfail`/`error`). NXDOMAIN can't
  safely mean "available" for .com (redemption/pendingDelete domains leave the zone while still registered), so DoH
  never gates an RDAP call and never sets `lifecycle_status` — RDAP alone is the source of lifecycle truth.
- **Drop-date grace anchored on today, with a 35-day hard floor.** `GRACE_EST_DAYS = 45` (low-confidence auto-renew
  estimate) anchors on the observation date, not the RDAP `expiration` event (which Verisign has already pushed out
  +1 year during grace); it may never be tuned below **35 days**, the fixed ICANN redemption(30d)+pendingDelete(5d)
  tail a domain cannot drop faster than. Confirmed empirically at build time: Verisign's `events` array omits RGP
  phase-start dates on observed responses, so `redemption`/`pending_delete`/`grace` estimates fall back to the
  `today` anchor in practice (the event-anchored branch exists for forward-compat but wasn't exercised live).
- **Real-data smoke (2026-07-15):** `google.com` → registered, `renewed`, expiry 2028-09-13; `example.com` →
  registered, `renewed`, expiry 2026-08-13; `qzxkvbnmplkjhgfd.com` → 404, `dropped`, `drop_date_actual` set to
  today. Seeded 5-row batch: `processed=2 dropped=1 renewed=1 errors=0`, writeback correct on both rows. Zero
  entries in the unmatched-status tally — every observed RDAP status string was already in `KNOWN_STATUSES`, so no
  additions were needed.

### 2026-07-16 — Phase 5a spike: NameBio free path CONFIRMED; HumbleWorth hosted endpoint DEAD (✅ RESOLVED 2026-07-17 → Replicate at 5c)
Phase 5 split into three sub-phases (owner-delegated scope call): **5a** comps grounding (NameBio; no API key),
**5b** toxicity gate (free Google key), **5c** two-tier scoring core (Anthropic key). Each gets its own
spec → plan → build → real-data test → push. Design: [`docs/PHASE-5A-DESIGN.md`](docs/PHASE-5A-DESIGN.md).

**✅ The CONDITION on ratification #3.2 is DISCHARGED — the NameBio empirical spike ran (2026-07-16) and the free
path over-delivers**, so the own-comps-table hedge is **NOT** promoted for NameBio:
- Free, no-auth, no-signup **bulk CSVs confirmed live**: `GET /retailstats-download` = **6.7 MB, 97,568 keywords,
  1.8 s**; `GET /tldstats-download` = 161 KB, 741 TLDs. Exactly the `namebio_comps.csv` the TDD envisioned.
- **ToS permits it:** *"You may incorporate this data into other products and services, but attribution is
  required."* The "no product/service" prohibition is scoped to the **Paid API only**. Attribution is therefore a
  **licence condition**, not a courtesy → must land in the Phase 7 digest.
- **Measured limits** (not from the docs): stats endpoints **4 req / 60 s rolling, per-endpoint** (5th → 429;
  recovery ~64 s); **no `Retry-After` / `X-RateLimit-*` headers** (Cloudflare) so backoff must be our own; the
  `*-download` endpoints have an **independent, much longer window** (>30 min; exact length uncharacterized)
  ⇒ **a download 429 is NOT retryable in-run — the next daily cron run is the retry.** This deliberately inverts
  Phase 4's policy, where RDAP 429s recover in seconds.
- **The pricing snapshot below is stale:** "Basic $10/mo + export ← the budget play" no longer exists (tiers are now
  Domainer/Business/Enterprise), and "Free = 5 results/search, web only" conflates the **website** cap with the
  **API**, which has no such cap. **No paid tier is needed for comps.**

**⚠️ Ratification #3.2's HumbleWorth leg is BROKEN — owner decision required, not a doc fix.** #3.2 ratified
*"HumbleWorth ... (hosted endpoint on Windows-local; self-host via Docker on VPS later)"*. The free hosted endpoint
is **gone**:
- `valuation.humbleworth.com` fails the TLS handshake from **our** network (it CNAMEs to `ghs.googlehosted.com`,
  which closes connections for unmapped SNI) **and** from **Firecrawl's cloud** (`ERR_CONNECTION_CLOSED`) — two
  independent vantage points — while `humbleworth.com` itself serves fine **through the same proxy**, so this is not
  our MITM.
- HumbleWorth's own `/about/api` now documents **only** the Replicate route; the free endpoint is not mentioned at all.

| # | Option | Cost | Friction |
|---|---|---|---|
| 1 | **Replicate `humbleworth/price-predict-v1`, wired at 5c** | $0.10/1k ≈ **$0.09/mo** at 30/day | needs `REPLICATE_API_TOKEN` in `.env`; 2nd credential alongside the Anthropic key | ← ✅ **RATIFIED 2026-07-17 (owner)** |
| 2 | Docker self-host now (`r8.im/...`) | free, offline | Docker Desktop/WSL2 on Windows **now** — the friction deliberately deferred to the VPS; model SPDX license UNCONFIRMED |
| 3 | Ship 5a NameBio-only; add HumbleWorth at VPS migration | $0 | none | *(was the interim selection; superseded by option 1)* |

**✅ RE-RATIFIED 2026-07-17 (owner): option 1 — implement the `ValuationProvider` against Replicate during Phase 5c.**
(Supersedes the interim option-3 selection.) The modeled-valuation leg lands at 5c via Replicate's hosted
`humbleworth/price-predict-v1` (~$0.09/mo at 30 Tier-2 domains/day; batch ≤2,560 ⇒ 1 call/day), filling the
`"modeled"` slot that `value_range` reserved (`null`) from 5a — so it is a **data change, not a schema migration**,
exactly as designed. NameBio real sales remain the **anchor** (TDD anti-pattern #11: *"relying on a model estimate as
comps → anchor with NameBio real sales"*); HumbleWorth's channels are P50/P97.5/P99.25 of three **sale channels**
(NOT a low/mid/high band — never present them as one range in the Tier-2 prompt). **New 5c credential:**
`REPLICATE_API_TOKEN` joins the Anthropic key on the `.env` signup list. Docker self-host (option 2) remains the
free VPS-phase path if we later want offline/unlimited valuations. This retires ratified #3.2's dead-hosted-endpoint
leg entirely.

**Also corrected:** TDD §4.5 calls HumbleWorth's `auction`/`marketplace`/`brokerage` triple a *"modeled low/mid/high
range"* — it is **not**. They are **three distinct sale channels** at the **P50 / P97.5 / P99.25** percentiles.
Carried to 5c: presenting them as one distribution's range would corrupt the model's reconciliation.

**Bonus — Phase-4 follow-up proposal (logged, not chased):** NameBio exposes a free, no-auth `POST /verisign`
(+ `/verisign-download`) returning the **exact Verisign pending-delete drop order** for .com (5 × ≤100 domains /
24 h / IP). Phase 4's build notes record that Verisign RDAP **omits RGP phase-start dates**, forcing our
`today`-anchored estimates — this endpoint could pin real drop dates and sharpen the backorder decision.

### 2026-07-17 — Phase 5a built: comps grounding
Phase 5a (NameBio comps grounding) built (plan: `docs/superpowers/plans/2026-07-16-phase-5a-comps-grounding.md`;
design + build notes: `docs/PHASE-5A-DESIGN.md`). **Zero new dependencies** (stdlib `csv`/`json`/`hashlib` +
the existing `httpx`/`truststore` client). 187 tests pass, zero network in the suite, + 2 skipped live smokes
(RDAP + comps). Built subagent-driven (implementer + reviewer per task, opus final whole-branch review). Live
real-data confirmation **passed** 2026-07-17: real 97,576-row / 6.7 MB download; `cloudvault`→cloud@start +
vault@end, `austinplumber`→austin@start + plumber@end, `vault`→exact, `zylo`→absence-not-$0; idempotent
no-op; live 429→refused-cache-intact; corrupt-header→refused. Decisions locked during the build:
- **A library, not a pipeline stage** — `comps.py` is a cache + lookup + CLI; it does **not** write
  `candidates`. 5c calls `lookup()` and writes `value_range` at scoring time (comps are global context keyed
  by freshness, needed only for the ~30 domains that reach Tier-2).
- **Per-file independent refresh** — the two caches download/validate/swap independently (each with its own
  `.prev` + sidecar entry), retailstats first; one file's 429/failure never discards the other's validated
  download. A validated 6.7 MB download is scarce (gotcha #3's uncharacterized >30-min window).
- **429 refused, never retried in-run** (inverts Phase 4): NameBio download 429s recover in hours, so the
  next daily cron run is the retry. `ratelimit.py` (async, whodap-specific) is deliberately NOT reused; comps
  has a sync `_get_with_retry` retrying `httpx.TransportError` only.
- **Validate-before-swap + `.prev` + sidecar** (owner review round 2): an HTTP-200 error-page / truncated /
  empty download can neither replace nor seed a cache (parse + exact-header + 80%-shrink / first-run min-rows
  floor); `--force` bypasses freshness + shrink but never the header check; `namebio_meta.json` is the source
  of truth for freshness and the shrink baseline; a crash between the two swap renames recovers via `.prev`.
- **`modeled: null` reserved** in `value_range` from day one, so adding HumbleWorth later is a data change,
  not a schema migration. **NameBio attribution** carried on every `CompsContext` (licence condition → Phase 7).

### 2026-07-20 — Phase 5b built: toxicity gate; GSB confirmed live; host-level scope accepted
Phase 5b (Wayback CDX history shape + Google Safe Browsing) built and confirmed against real APIs
(plan: `docs/superpowers/plans/2026-07-18-phase-5b-toxicity-gate.md`; design + build notes:
`docs/PHASE-5B-DESIGN.md`; live findings: `docs/PHASE-5B-SPIKE.md` Parts 1–3). **Zero new dependencies.**
282 tests pass + 3 skipped, zero network in the suite. Built subagent-driven (implementer + reviewer per
task, opus final whole-branch review). The Safe Browsing leg was blocked through 2026-07-19 by a Google
Cloud misconfiguration — **both** causes had to be fixed: the API needed enabling on the project *and* the
key's API-restriction allowlist needed Safe Browsing added (the two 403 bodies seen were two separate
problems, not one error reported inconsistently). Resolved 2026-07-20. Decisions and findings:
- **A library, not a pipeline stage** (5a precedent) — Tier-1 decides who gets screened, and Tier-1 does not
  exist until 5c.
- **Asymmetric legs** — GSB is a hard reject (batched ≤500 URLs, so a day's screen is one call); CDX is a
  graded signal for Tier-2. The legs are deliberately **independent**.
- **A3 (top residual false-negative risk) closed favourably** — GSB echoes `threat.url` byte-identical to
  what was sent, scheme included, so hit attribution is correct. This could not have been settled by any
  unit test: the fixtures encode the same assumption as the code, so only live observation discharges it.
- **⚠️ GSB is a HOST-LEVEL check — owner ruling: accept and document.** v4 expands a lookup URL into
  host-suffix/path-prefix combinations, so the bare `scheme://domain/` probe cannot match a blocklist entry
  stored at a path; a host with an active path-scoped MALWARE listing returns `currently_listed=False`.
  `threatMatches:find` takes URLs, not hosts, so there is no "anything under this host?" query — an API
  boundary, not a probe bug. Wholly-malicious (host-listed) domains are still caught; **path-scoped**
  listings — the usual shape for *compromised legitimate sites*, a real slice of the target population —
  are not. The real-world rate is **unmeasured**. Rejected alternative: injecting top-N CDX-observed paths
  as extra `threatEntries` (couples the independent legs, re-budgets the 500 cap) — revisit if Phase-6
  outcomes show path-scoped misses mattering. `gsb_currently_listed: false` therefore means "this host is
  not itself listed right now", never "nothing under this host is listed"; 5c's prompt must not round up.
- **Empty-list guard premise measured, not assumed** — against a URL known to be listed, empty `threatTypes`
  and empty `platformTypes` each return 0 matches (silent false-cleans), while empty `threatEntryTypes` still
  matched (v4 appears to default it). The guard refuses all three regardless: refusing the harmless case is
  free, and the defaulting is undocumented.
- **Invariants confirmed on the live path** — never-archived → `unknown_no_history` (distinct from `pass`
  and from `reject`); and the earlier GSB outage incidentally live-validated invariant 2, pulling every
  verdict to `unknown_error` rather than `pass`.
- **Carried into 5c:** `screen()`'s injected `now` never reaches `VerdictCache.now`; 5c is the first caller
  that will pass `now`, and a **future** `now` makes the TTL delta negative — a permanent cache hit.

### Accepted defaults (owner didn't object; cheap to change)
- Daily digest: **local markdown file**, top **10** candidates, Tier-2 scoring cutoff ~**30** domains.

---

## Pending proposals (raised 2026-07-13, not yet ratified — decide at the relevant phase)

| # | Phase | Proposal |
|---|---|---|
| 1 | 5/7 | Treat digest as an **opportunity-ranker**, not a gem-finder: skew Tier-2 spend toward secondary target + auction candidates. Primary target (≤8-char dictionary .coms) essentially never drops uncaught. |
| 2 | 4 | **Compute** drop dates from Verisign RDAP status + events (expiry → 0–45d auto-renew grace → 30d redemptionPeriod → 5d pendingDelete → drop) rather than only observing them. |
| 3 | 5 | ✅ **Resolved 2026-07-14** (see 2026-07-14 entry): adopt NameBio **free** API + HumbleWorth self-host ($0); NameBio **Basic $10/mo** + export kept as a *planned upgrade* if the free tier is too thin. The local comps-cache-per-pattern idea still applies (built from the free RetailStats CSV). |
| 4 | 5 | Trademark screen via **USPTO trademark search API** (free) — never LLM recall. Highest stakes for geo+service names (UDRP risk). |
| 5 | 3 | Pronounceability: char-level **n-gram model** trained on English words (or CVC-pattern scorer) — CMUdict can't cover invented words. |
| 6 | 5 | Toxicity screen: Wayback CDX API (free) + Google Safe Browsing (free w/ key) now; backlink-anchor check **deferred** (no good free API). |
| 7 | 1 | ✅ **Resolved 2026-07-14** — superseded by the **open-cycle model** (see 2026-07-14 entries & TDD §5): the *calculated* drop date can't be in the key (it moves as estimates refine, and is meaningless on renewal), so identity = `id` PK + a partial unique index on the open cycle per domain, plus `verified_at`/`scored_at`. **Amended in review round 2:** `'dropped'` stays OPEN; predicate `NOT IN ('renewed','reregistered','dismissed')`; `lifecycle_status NOT NULL DEFAULT 'unknown'`. |

---

## Pricing snapshot (verified live 2026-07-13 — recheck if stale)

**WhoisFreaks** ([pricing](https://whoisfreaks.com/pricing/expiring-dropped-domains)):
| Product | Monthly | Billed yearly |
|---|---|---|
| Expiring domains, list only | $70 | $59/mo |
| Expiring + WHOIS | $100 | $84/mo |
| Dropped domains, list only | $100 | $84/mo |
| Dropped + WHOIS | $150 | $125/mo |
| Backlink tiers | $200–250 | $167–209/mo |

> Before ever paying: compare their [credit-based API plans](https://whoisfreaks.com/pricing/api-plans) — metered access to the expiring feed may beat $70 flat for a .com-only daily pull.

**NameBio** ([memberships](https://namebio.com/memberships)) — ⚠️ **STALE as of 2026-07-16, and superseded for
comps purposes; see the 2026-07-16 Phase-5a entry above.** The "Basic $10/mo" tier no longer exists (tiers are now
Domainer/Business/Enterprise, prices UNCONFIRMED), and the "Free" row below conflates the **website** results cap
with the **free API**, which has no such cap and needs no membership. **No paid tier is required for comps** — the
free `/retailstats-download` + `/tldstats-download` bulk CSVs are the whole comps dataset, and the free-data ToS
explicitly permits use with attribution. Kept below only as the historical record of the 2026-07-13 research.
| Tier | Price | Notes |
|---|---|---|
| Free | $0 | 5 results/search, web only ← ⚠️ website only; the free **API** has no such cap |
| Basic | $10/mo ($100/yr) | 100 results/search **+ export** ← ⚠️ tier no longer exists; export ≠ the free stats CSVs |
| Pro | $25/mo ($250/yr) | API, but only 100 credits/mo (~3 lookups/day) |
| Business | $50/mo ($500/yr) | API, 500 credits/mo |

**AI scoring @ full volume** (200 triage + 30 deep/day, nightly batch = 50% off):
| Provider | Models | ~$/mo |
|---|---|---|
| Anthropic | Haiku 4.5 ($1/$5 per MTok) + Sonnet ($3/$15; intro $2/$10 thru 2026-08-31) | ~$8 |
| OpenAI | 5.5-mini/nano triage + GPT-5.5 ($5/$30) deep | ~$12 |
| OpenAI (all-mini) | 5.5-mini both tiers | ~$2–4 |

**Other:** VPS $4–6/mo (Hetzner CX22 ~€4.50, DO $6). Backorders $59–79/attempt (DropCatch/SnapNames), typically charged only on successful catch.

**Total burn:** ~$8–18/mo lean path; $0 until Phase 5 needs an API key.
