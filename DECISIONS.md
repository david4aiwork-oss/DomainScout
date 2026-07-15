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

**NameBio** ([memberships](https://namebio.com/memberships)):
| Tier | Price | Notes |
|---|---|---|
| Free | $0 | 5 results/search, web only |
| Basic | $10/mo ($100/yr) | 100 results/search **+ export** ← the budget play |
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
