# DomainScout — Technical Design Document (TDD)

**Status:** Draft v2 for owner review · **Date:** 2026-07-14 · **Scope:** full 7-phase pipeline + Phase 8 UI
**Companion docs:** [`CLAUDE.md`](../CLAUDE.md) (build spec) · [`DECISIONS.md`](../DECISIONS.md) (decision log & pricing)

> **What changed v1 → v2** (after owner review + a second research pass): schema identity model
> revised (drop-date estimate removed from the key — see [§5](#5-data-schema)); drop-date computation
> made **status-driven** ([§4.4](#44-phase-4--rdap-verification)); the free feed confirmed to be
> **domain-names-only and not .com-only** ([§4.2](#42-phase-2--ingestion)); **Dynadot reclassified** as a
> public *auction* source rather than dropped ([§4.2](#42-phase-2--ingestion), [§7](#7-decisions--open-items));
> **comps design resolved to a $0 path** (NameBio free + HumbleWorth self-host,
> [§4.5](#45-phase-5--two-tier-ai-scoring)); **Phase 8 UI** added (Flask/FastAPI,
> [§4.7](#47-phase-8--local-review-ui)); **360-day artifact retention**; licenses verified ([§3](#3-surveyed-tool-teardown)).

---

## 1. Purpose & method

**Purpose.** Establish the architecture, module boundaries, library choices, and anti-patterns for
DomainScout before Phase 1 code, grounded in prior art that already solved the sub-problems.

**Method.** A structured multi-source survey (24 sources, 119 candidate claims, 25 verified by 3-vote
adversarial checking; **23 passed unanimous 3-0**), plus a second targeted pass answering the open
decisions and the comps question against primary sources (ICANN, Verisign, IANA, and the actual repo
files). Confidence is high; qualifications are marked ⚠️.

---

## 2. Executive summary

The survey **validated** DomainScout's core decisions (RDAP-first, free feeds, two-tier scoring) and the
second pass turned the open questions into concrete, mostly **$0** design choices.

1. **RDAP client:** use **`whodap`** (async, MIT) as primary; **`whoisit`** (sync, BSD-3) as reference/fallback.
2. **The free feed is a name firehose, nothing more.** Domain names only — no dates, no category columns —
   and only ~50% `.com`. All lifecycle data comes from our own RDAP step; ingestion filters to `.com`.
3. **Drop-date is status-driven.** The ICANN registry tail is a fixed **30-day redemption + 5-day
   pendingDelete = 35-day deterministic countdown**; the pre-drop auto-renew grace is registrar-variable
   (0–45 days), so we pin the exact drop date from RDAP `redemptionPeriod`/`pendingDelete`, not from a
   fixed expiry offset.
4. **Two complementary free data sources:** WhoisFreaks free feed (registry-drop name firehose, for the
   hand-register/backorder branch) + Dynadot's public **expired-auction CSV** (for the bid-at-auction branch).
5. **RDAP hygiene:** query `rdap.verisign.com/com/v1/` **directly** (the rdap.org aggregator hard-caps at
   10 req/10 s); build our own async rate-limiter + backoff + cache (no client ships one).
6. **Comps at $0:** NameBio **free** RetailStats/TLDStats (real comparable-sale stats) + **HumbleWorth**
   open-source model self-hosted (per-domain modeled value range). Both injected into Tier-2.
7. **Filtering/scoring:** `wordfreq.zipf_frequency` graded threshold; **n-gram phonotactics**
   pronounceability; local-deterministic-vs-AI split with data-calibrated weights.
8. **Phase 8:** a local **Flask/FastAPI** app to view/filter the DB and Phase 6/7 results, with write-back
   to mark outcomes and tune criteria.

---

## 3. Surveyed-tool teardown

### 3.1 The six named projects

| # | Project | Stack / License | Problem it solves | Borrow | Avoid |
|---|---------|-----------------|-------------------|--------|-------|
| 1 | **maelgangloff/domain-watchdog** | PHP/Symfony, Redis · **AGPL-3.0** ⚠️ *(copyleft — study only)* | RDAP monitoring + auto-acquisition | **RDAP event-lifecycle model** (Event=action+immutable date; status JSON array; `deleted` bool; `isPendingDelete()`/`isRedemptionPeriod()`/`getExpiresInDays()`); **rate-limit + cache to minimize RDAP calls** | Redis/Symfony machinery (overkill for a daily SQLite batch). **AGPL → don't copy code.** |
| 2 | **threatexpress/domainhunter** | Python · **BSD-3-Clause** ✅ *(verified)* | Red-team aged-domain finder | **Multi-source reputation/toxicity pattern** (`checkBluecoat`/`checkIBMXForce`/`checkTalos` + Umbrella/McAfee/malwaredomains/Archive.org) for the Phase-5 toxicity gate | **Scrapes authenticated `member.expireddomains.net`** (login + OCR CAPTCHAs). Reputation *endpoints* also brittle now (BlueCoat CAPTCHA) — borrow the pattern, re-pick sources. |
| 3 | **Williams-Media/Exipred-Domain-Finder** *(sic)* | Python · MIT *claimed, no LICENSE file* ⚠️ | Crawl for expired domains | Nothing structural | **Port-43 WHOIS** (`whois.whois().expiration_date`). No formal license → don't copy verbatim. |
| 4 | **twiny/spidy** | Go · **MIT** ✅ *(verified)* | Concurrent availability checker | Concurrency concept only | **Port-43 WHOIS** (`twiny/whois`, TCP :43). Wrong protocol & language. |
| 5 | **thejacedev/Expireddomains-Fast-Checker** | Python (Selenium) · license unverified | Bulk-check ExpiredDomains.net | Nothing structural | **Selenium + real Chrome + manual login**; CAPTCHA-prone. Max-fragility ingestion. |
| 6 | **Hosteroid/domain-monitor** | PHP · license unverified | Multi-TLD expiry monitor (1,400+ TLDs) | **IANA RDAP bootstrap done right** (parses `data.iana.org/rdap/dns.json` `services` array; comment *"DO NOT guess RDAP URLs"*); RDAP-first + WHOIS fallback | ⚠️ README does **not** document rate-limiting (claim refuted 0-3) — not a throttling reference. |

### 3.2 Libraries & references adopted

| Source | Stack / License | Role |
|--------|-----------------|------|
| **pogzyb/whodap** | Python (`httpx`) · **MIT** ✅ | **Primary async RDAP client** (`aio_lookup_domain`, `new_aio_client`). |
| **meeb/whoisit** | Python · **BSD-3** ✅ | Sync RDAP reference/fallback; IANA bootstrap; datetime-typed fields; status list. ⚠️ **No throttling, no retry** (`QueryError`); sync only. |
| **pogzyb/asyncwhois** | Python · license unverified | Optional WHOIS cross-check; dual WHOIS+RDAP, paired sync/async; RDAP transport via whodap→httpx. |
| **rspeer/wordfreq** | Python · (ratified dep) | Graded dictionary match: `zipf_frequency` (Zipf 6 ≈ once/1k words, Zipf 3 ≈ once/1M); `word_frequency`→0–1; `top_n_list`/`get_frequency_dict` for a curated list. ⚠️ Corpus commonness, not membership. |
| **arXiv 1706.09335** — *Generating Appealing Brand Names* | Paper | Name-quality math: appeal = weighted(readability, pronounceability, memorability, uniqueness); pronounceability via char n-grams; weights via Rank-SVM. ⚠️ Small sample; borrow the *decompose-and-calibrate* principle, not the numbers. |
| **lukem512/pronounceable** | JS · license unverified | Reference n-gram pronounceability (bigram+trigram; `score()`=Σtrigram-prob/len; bigram fallback <3 chars). Port the *idea* to Python. |
| **vasilytrofimchuk/domainsearcher-app** | app · MIT *claimed, no LICENSE file* ⚠️ | **Patterns-only** (no verbatim copy). Validates the **local/AI scoring split** (LEN/ZON local; PRO/MEM/BRD/FIT AI in one call) and a **two-stage availability check** (RDAP → DNS-over-HTTPS vs Cloudflare `1.1.1.1`). |
| **HumbleWorth** (`humbleworth/price-predict-v1`) | Python model · **open-source (Cog/Docker)** ✅ | **Self-hosted per-domain value range** — see [§4.5](#45-phase-5--two-tier-ai-scoring). |
| **NameBio free API** | data · free w/ **attribution** | **Real comparable-sale stats** (RetailStats/TLDStats) — see [§4.5](#45-phase-5--two-tier-ai-scoring). |

---

## 4. Architecture

### 4.1 Module boundaries & data flow

Every phase is a standalone, idempotent module runnable via `python -m domainscout.<module>`. State passes
**through the SQLite DB**, never in-memory between phases — this is what makes each phase independently
re-runnable.

```
   WhoisFreaks feed ─┐
   Dynadot auctions ─┴─►│2. ingest │ filter to .com, upsert (partial unique index on open cycle)
                        └────┬─────┘
                             ▼
                        │3. filter │ deterministic: length/charset/dict/pronounceability → filter_pass/reason
                             ▼ (survivors)
                        │4. verify │ async RDAP: status, drop_date (status-driven), lifecycle_status, verified_at
                             ▼
                        │5. score  │ toxicity gate → Tier-1 triage → Tier-2 deep (+NameBio comps +HumbleWorth range)
                             ▼
                        │7. digest │ ranked markdown, top ~10, action = register/backorder/bid/skip
   6. outcomes ───────────────────────► writes real results back for calibration
   8. web UI (Flask/FastAPI) ─────────► view/filter DB + Phase 6/7 results + write-back
```

**Package layout** (Phase 1 creates the skeleton):

```
domainscout/
  __main__.py          # argparse dispatch → subcommands (init-db, ingest, filter, verify, score, digest, outcome, prune, web)
  config.py            # load + validate criteria.toml (tomllib)
  db.py                # connection, schema DDL, migrations, upsert helpers
  models.py            # dataclasses: Candidate, RdapResult, Scores
  ingest.py            # Phase 2 (WhoisFreaks + Dynadot adapters, .com filter)
  sources/             #   feed adapters: whoisfreaks.py, dynadot.py
  filters.py           # Phase 3
  pronounce.py         # n-gram pronounceability scorer (+ trained tables)
  rdap.py              # Phase 4: async client wrapper, rate-limit, status-driven drop-date
  scoring/
    base.py            # score(domain, context) -> JSON  (provider-agnostic)
    anthropic.py       # default provider (Haiku triage + Sonnet deep, Batch API)
  comps.py             # NameBio free stats cache + HumbleWorth value range
  toxicity.py          # Phase 5 pre-score gate (Wayback + Safe Browsing)
  outcomes.py          # Phase 6
  digest.py            # Phase 7
  web/                 # Phase 8 Flask/FastAPI app (read + write-back)
  retention.py         # prune raw feeds/digests older than N days
criteria.toml
data/  domainscout.db · feeds/ · digests/ · ngram_tables.json · namebio_comps.csv
docs/  TECHNICAL-DESIGN.md
tests/
```

### 4.2 Phase 2 — Ingestion

**Two complementary sources**, matching the two branches of the decision output:

| Source | What it is | Feeds which branch | Format (verified) |
|--------|-----------|--------------------|-------------------|
| **WhoisFreaks free feed** (GitHub) | Registry expired/dropped **name firehose** | hand-register / backorder | Newline-delimited **domain names only** — *no header, no dates, no category column*. `~10k/day`, **~50% `.com`**, includes other TLDs/hyphens/digits. Files: `YYYY-MM-DD-free-expired-domains.csv` & `-dropped-`, plus `0-latest-*` snapshots. **~1-day lag.** |
| **Dynadot expired auctions** | Public **auction/closeout inventory** (bid on it) | bid at auction | Public CSV export (`/help/question/download-expired-list`, marketplace `/market/auction`) + account-keyed aftermarket API (`get_open_auctions`, `get_expired_closeout_domains`, `download_all_listings`). ⚠️ *Not* a registry drop list — it's inventory to bid on. |

**Consequences for the design:**
- **Ingestion filters to `.com`** on the way in (hard invariant per `CLAUDE.md`) — ~5k of 10k/day survive from WhoisFreaks.
- The feed carries **no lifecycle data**; the `-expired-` vs `-dropped-` filename is the only signal (initial `lifecycle_status`: `expiring` vs `dropped`). Everything else is backfilled by RDAP (Phase 4).
- **Idempotency:** re-downloading a given date's file + `INSERT ... ON CONFLICT DO UPDATE` on the open-cycle index → repeated runs converge. `first_seen` set on insert only.
- ⚠️ **The WhoisFreaks free feed has no stated license / data-use terms** — fine for personal use; get written clarification before any commercial use ([§7](#7-decisions--open-items)).

### 4.3 Phase 3 — Rules filter

Cheapest-gate-first, deterministic, fully logged (`filter_pass` + `filter_reason` per domain):
1. **Charset/shape:** `^[a-z]+$`, no hyphens/numbers (needed — the feed does no pre-filtering).
2. **Length:** ≤8 (primary) / 9–12 (secondary), per `criteria.toml`.
3. **Dictionary (graded, `wordfreq`):** score stem/word-split by `zipf_frequency`; tunable threshold, not binary. Two-word combos scored by min/mean of parts.
4. **Pronounceability (n-gram, `pronounce.py`):** bigram/trigram frequency model; **whole-word average** score (⚠️ *not* lukem512's hard per-trigram floor, which nukes a good coined word for one odd trigram). Not CVC, not CMUdict (can't score invented-but-pronounceable secondary targets).

Target: ~50–200 survivors/day → Phase 4.

### 4.4 Phase 4 — RDAP verification

- **Client:** `whodap` (async, MIT) primary; `whoisit` (sync, BSD-3) fallback.
- **Endpoint:** resolve via **IANA bootstrap** (RFC 9224), then **cache** the `.com`→`rdap.verisign.com/com/v1/` mapping (stable for a `.com`-only tool). **Query Verisign directly — not rdap.org** (which hard-caps at **10 req/10 s** and tells you to go direct for volume).
- **Status:** RDAP signals registration by HTTP code (200=registered+JSON, 404=available). Tag `rdap_status` from the returned **status list**.
- **Drop-date (status-driven — the key correction):** the registry tail is **fixed by ICANN: redemptionPeriod 30 d + pendingDelete 5 d = 35 d deterministic**. The pre-drop auto-renew grace is **registrar-variable (0–45 d)**, so:
  - `redemptionPeriod` present → `drop_date_est = redemption_start + 35 d` (high confidence).
  - `pendingDelete` present → drop within ~5 d (highest confidence).
  - only `autoRenewPeriod`/`expiration_date` known → low-confidence estimate; re-check as it advances.
  Prefer exact phase-start dates from the RDAP `events` array when present.
- **Rate-limiting (build it ourselves):** async semaphore + token-bucket + exponential backoff on `429`; per-run response cache; descriptive `User-Agent` (good practice, not required). Verisign publishes no numeric limit — a modest daily batch is within its ToS; pace politely.
- **Optional DNS pre-filter:** DNS-over-HTTPS A/NS check vs Cloudflare `1.1.1.1` (NXDOMAIN ⇒ likely available) before spending an RDAP call (per `DECISIONS.md` "DNS only as optional pre-filter"; domainsearcher-app's two-stage pattern).

### 4.5 Phase 5 — Two-tier AI scoring

- **Provider-agnostic** `score(domain, context) → JSON`; default Anthropic (Haiku triage → Sonnet deep, Batch API).
- **Hybrid local/AI split:** deterministic dims (length, lifecycle, dict/pronounceability from Phase 3) computed locally and passed as **context**; only subjective dims (brandability, memorability, commercial potential, linguistic clarity) are AI-scored.
- **Comps grounding ($0 path):**
  - **NameBio free API** — real aggregated comparable-sale stats: `RetailStats` (count/avg/max/σ per keyword & placement) + `TLDStats`, both downloadable as **CSV → local cache** (`namebio_comps.csv`), refreshed periodically. **Free, attribution required** (cite NameBio in the digest). *(This likely retires `DECISIONS.md` pending proposal #3's $10/mo plan — see [§7](#7-decisions--open-items).)* ⚠️ Do **not** use NameBio's *paid* API — its ToS forbids use in any product/service without written permission.
  - **HumbleWorth** — open-source valuation model **self-hosted via Docker/Cog** (free, CPU, ~2 GB RAM), returning an `auction/marketplace/brokerage` triple = a modeled low/mid/high range. (Hosted Replicate API ~$0.10/1k is the fallback.) ⚠️ A *model estimate*, trained through early-2024 — pair with NameBio's real stats, don't rely on it alone.
  - **Injection:** NameBio real stats (reality band) + HumbleWorth modeled triple (point anchor) → the LLM reconciles into a value range + rationale.
- **Weights are data-calibrated**, not hand-set — tune against Phase 6 outcomes (arXiv Rank-SVM is the template).
- **Toxicity gate before scoring** (`toxicity.py`): Wayback CDX history *shape* + Google Safe Browsing (both free), following domainhunter's *multi-source reputation* pattern (re-pick live sources).

### 4.6 Phases 6–7 — Outcomes & digest

- **Outcomes** (`outcomes.py`): writes `outcome`/`outcome_price`/`outcome_date` back → calibration input.
- **Digest** (`digest.py`): local markdown, top ~10, ranked with score + rationale + drop date + action (register/backorder/bid/skip). Digests retained per [§4.7](#47-phase-8--local-review-ui) retention.

### 4.7 Phase 8 — Local review UI

- **Stack:** **Flask/FastAPI** local web app (owner's choice), read path first, write-back second.
  - *Read:* browse/filter/sort the `candidates` table + Phase 6/7 results; view a candidate's full scores/rationale/lifecycle; browse retained digests.
  - *Write-back:* mark outcomes (backordered/auction price/unsold), edit `criteria.toml` thresholds, flag/dismiss candidates — closing the calibration loop.
- **Artifact retention:** raw feed files + generated digests kept **360 days**, then `prune` removes older ones (`python -m domainscout prune`). The **SQLite DB (all derived data) is permanent**. Disk ≈ a few hundred MB/year (bounded). Retained artifacts are reviewable through the UI to improve the rubric.

---

## 5. Data schema

Revised from proposal #7 after the owner flagged that a **calculated** drop-date in the unique key would
(a) spawn duplicate rows when the estimate is refined and (b) be meaningless if the domain is renewed and
never drops. Since the feed provides **no date at ingestion**, identity keys on the domain's *open cycle*,
not on any date.

```sql
CREATE TABLE candidates (
  id                INTEGER PRIMARY KEY,        -- surrogate; FK target for future tables
  domain            TEXT NOT NULL,
  source            TEXT,                       -- 'whoisfreaks' | 'dynadot' (+ file)
  feed_category     TEXT,                       -- 'expired' | 'dropped' (from the feed filename)
  first_seen        TIMESTAMP NOT NULL,         -- set on insert only
  -- lifecycle (backfilled by RDAP, Phase 4)
  expiry_date       DATE,                       -- registry expiration for this cycle (from RDAP)
  drop_date_est     DATE,                       -- MUTABLE estimate; refined by RDAP status
  drop_date_actual  DATE,                       -- set ONLY when confirmed dropped
  lifecycle_status  TEXT,                       -- expiring|grace|redemption|pending_delete|dropped|renewed|reregistered|unknown
  rdap_status       TEXT,                       -- raw RDAP status list (JSON)
  verified_at       TIMESTAMP,                  -- idempotent re-run guard
  -- filter (Phase 3)
  filter_pass       BOOLEAN,
  filter_reason     TEXT,
  -- scoring (Phase 5)
  tier1_score       REAL,
  tier2_scores      TEXT,                       -- JSON per-dimension
  value_range       TEXT,                       -- JSON: NameBio stats + HumbleWorth triple
  rationale         TEXT,
  recommended_action TEXT,
  scored_at         TIMESTAMP,                  -- idempotent re-run guard
  -- outcomes (Phase 6)
  outcome           TEXT,
  outcome_price     REAL,
  outcome_date      DATE
);

-- At most ONE open cycle per domain; closed rows (dropped/renewed/reregistered) retained as history.
-- Re-registration after a real drop closes the old row and opens a new one → distinct opportunity rows.
CREATE UNIQUE INDEX ux_open_cycle ON candidates(domain)
  WHERE lifecycle_status NOT IN ('dropped','renewed','reregistered');

CREATE INDEX idx_drop_est   ON candidates(drop_date_est);
CREATE INDEX idx_filter_pass ON candidates(filter_pass);
CREATE INDEX idx_lifecycle  ON candidates(lifecycle_status);
```

**Renewal handling (owner's Q3):** when RDAP shows the registration was renewed (expiry moved forward /
status back to active), set `lifecycle_status = 'renewed'`, clear `drop_date_est`, **keep the row** as a
calibration signal ("liked it, wasn't available"), and exclude it from active ranking. Ranking uses
`drop_date_actual` when known, else `drop_date_est`. `verified_at`/`scored_at` let each phase skip
already-processed rows while re-runs stay safe (upsert converges).

---

## 6. Anti-patterns we're designing around

Each observed in a surveyed tool, verified:
1. **Port-43 WHOIS** (Williams-Media; spidy) → RDAP-first.
2. **Scraping an authenticated UI** (domainhunter OCR CAPTCHAs; Fast-Checker Selenium) → ingest feed files.
3. **Hardcoding/guessing the RDAP endpoint** → IANA bootstrap (domain-monitor's *"DO NOT guess RDAP URLs"*).
4. **Assuming the client rate-limits for you** (`whoisit` doesn't) → caller-side limiter + backoff + cache.
5. **Routing volume through rdap.org** (10 req/10 s cap) → direct to Verisign.
6. **Fixed expiry-offset drop dates** (ignores registrar-variable auto-renew) → status-driven from RGP/pendingDelete.
7. **Binary wordlist membership** → graded `zipf_frequency`.
8. **Rigid CVC/CMUdict pronounceability** → n-gram phonotactics; **whole-word average** (not per-trigram floor).
9. **Hand-set scoring weights** → calibrate against Phase 6 outcomes.
10. **In-memory state between phases** → state in SQLite; every phase idempotent & standalone.
11. **Relying on a model estimate as "comps"** (HumbleWorth alone) → anchor with NameBio real sales.

> **Refuted / excluded:** Hosteroid rate-limiting (0-3) and `whoisit` native async (1-2) — treat `whoisit` as sync, no throttling.

---

## 7. Decisions & open items

**Resolved by the second research pass (facts, cited):**
- ✅ **.com lifecycle:** RGP 30 d + pendingDelete 5 d = **fixed 35 d** registry tail; auto-renew grace 0–45 d (registrar-variable). *(ICANN ERRP/RGP, EPP status codes.)*
- ✅ **Verisign RDAP:** no published numeric limits/headers; ToS bans "high volume" mass querying; **query direct, not rdap.org**. *(Verisign RDAP help/ToS; about.rdap.org.)*
- ✅ **Free feed:** names-only, ~50% `.com`, no dates/category → filter to `.com` ourselves, lifecycle from RDAP.
- ✅ **Comps:** NameBio **free** RetailStats/TLDStats + HumbleWorth self-host = **$0**.
- ✅ **Licenses:** whoisit BSD-3, whodap MIT, domainhunter BSD-3, spidy MIT are clean; domainsearcher-app & Williams-Media claim MIT with **no LICENSE file** (patterns-only); domain-watchdog AGPL (study only); **WhoisFreaks feed has no stated terms**.

**Needs owner ratification (touch `DECISIONS.md`):**
1. **Data-source model:** keep **WhoisFreaks free feed** (drop firehose) **and reclassify Dynadot** as a public **expired-*auction*** source (bid/backorder branch), *not* a hand-register drop list. Replaces the ambiguous "Dynadot drop lists" line.
2. **Comps:** adopt the **$0 NameBio-free + HumbleWorth-self-host** path; **retire pending proposal #3's** NameBio Basic $10/mo (unless you want the paid comps depth later — but its ToS forbids pipeline use).
3. **Schema:** adopt the revised **open-cycle** identity model ([§5](#5-data-schema)) in place of `UNIQUE(domain, drop_date)`.

**Still open (lower stakes, decide at the phase):**
- Exact `User-Agent` string / pacing constant for the Verisign batch (pick a polite default, e.g. ≤1–2 req/s).
- Whether to self-host HumbleWorth (Docker) from day one or start with the free direct endpoint / Replicate API.

---

## 8. Proposed dependencies

| Dependency | Purpose | License |
|-----------|---------|---------|
| `whodap` | async RDAP client (Phase 4) | MIT ✅ |
| `whoisit` | sync RDAP fallback | BSD-3 ✅ |
| `wordfreq` | graded dictionary match | ratified |
| `aiohttp`/`httpx` | async I/O (RDAP, DoH, feeds) | permissive |
| `flask` **or** `fastapi`+`uvicorn` | Phase 8 UI | BSD/MIT ✅ |
| HumbleWorth model (Docker/Cog) | self-hosted valuation | open-source ✅ |
| `tomllib`, `sqlite3`, `csv` | config, storage, feed parse | **stdlib** (3.11+) |
| Anthropic SDK | Phase 5 scoring | needs API key (Pro ≠ API credits) |

Stdlib-first keeps the Windows-local → VPS move trivial.

---

## 9. Sources & evidence

Survey run `wf_dd773f78-442` (23/23 claims 3-0) + second pass (primary sources below).

**Projects:** [domain-watchdog](https://github.com/maelgangloff/domain-watchdog) ·
[domainhunter](https://github.com/threatexpress/domainhunter) ·
[domain-monitor](https://github.com/Hosteroid/domain-monitor) ·
[Expireddomains-Fast-Checker](https://github.com/thejacedev/Expireddomains-Fast-Checker) ·
[Exipred-Domain-Finder](https://github.com/Williams-Media/Exipred-Domain-Finder) ·
[spidy](https://github.com/twiny/spidy)

**Libraries/models:** [whoisit](https://github.com/meeb/whoisit) · [whodap](https://github.com/pogzyb/whodap) ·
[asyncwhois](https://github.com/pogzyb/asyncwhois) · [wordfreq](https://github.com/rspeer/wordfreq) ·
[pronounceable](https://github.com/lukem512/pronounceable) ·
[domainsearcher-app](https://github.com/vasilytrofimchuk/domainsearcher-app) ·
[HumbleWorth model](https://replicate.com/humbleworth/price-predict-v1) ·
[dnsworth (HumbleWorth ref impl)](https://github.com/dnsworth/dnsworth)

**Authoritative references:** [ICANN ERRP](https://www.icann.org/en/contracted-parties/consensus-policies/expired-registration-recovery-policy/expired-registration-recovery-policy-21-02-2024-en) ·
[ICANN EPP status codes](https://www.icann.org/resources/pages/epp-status-codes-2014-06-16-en) ·
[ICANN RGP](https://www.icann.org/resources/pages/grace-2013-05-03-en) ·
[Verisign RDAP help](https://www.verisign.com/news-insights/registration-data-access-protocol/help/) ·
[Verisign RDAP ToS](https://www.verisign.com/domain-names/registration-data-access-protocol/terms-service/index.xhtml) ·
[rdap.org limits](https://about.rdap.org/) · [IANA RDAP bootstrap](https://data.iana.org/rdap/dns.json) ·
[WhoisFreaks free feed](https://github.com/WhoisFreaks/daily-expired-and-dropped-domains) ·
[NameBio free API](https://api.namebio.com) · [HumbleWorth API](https://humbleworth.com/about/api) ·
[Dynadot expired list export](https://www.dynadot.com/help/question/download-expired-list) ·
[arXiv 1706.09335](https://arxiv.org/pdf/1706.09335)
