# DomainScout — Technical Design Document (TDD)

**Status:** Draft for owner review · **Date:** 2026-07-14 · **Scope:** full 7-phase pipeline
**Companion docs:** [`CLAUDE.md`](../CLAUDE.md) (build spec) · [`DECISIONS.md`](../DECISIONS.md) (decision log & pricing)

> This TDD translates a survey of the open-source expired-domain tooling landscape into concrete
> engineering decisions for DomainScout. It is prior-art-grounded: every "borrow" and "avoid" below
> traces to a specific project or reference, verified in the survey (see [§8 Sources](#8-sources--evidence)).
> It does **not** relitigate ratified decisions in `DECISIONS.md` — where the survey challenges one
> (the Dynadot source), it is flagged in [§7](#7-open-decisions--corrections-needed) for the owner, not changed here.

---

## 1. Purpose & method

**Purpose.** Establish the technical architecture, module boundaries, library choices, and anti-patterns
for DomainScout before Phase 1 code is written, using evidence from tools that already solved the
sub-problems (RDAP verification, feed ingestion, name scoring).

**Method.** A structured multi-source survey (5 search angles → 24 sources fetched → 119 candidate
claims → 25 verified by 3-vote adversarial checking). **23 claims passed unanimous 3-0** verification
against primary sources (repo code, READMEs, the arXiv PDF); **2 were refuted** and excluded (noted in
[§6](#6-anti-patterns-were-designing-around)). Confidence is high on the factual backbone; qualifications
are called out inline as ⚠️.

**How to read the verdicts.** Each surveyed tool gets: *stack · problem it solves · what to borrow · what
to avoid · license*. Two of the richest exemplars (domain-watchdog, domain-monitor) are PHP/Symfony
long-running services — **their patterns transfer, their code does not.**

---

## 2. Executive summary

The landscape splits cleanly into **patterns to borrow** and **anti-patterns DomainScout already decided against** — the survey largely *validates* the existing spec while sharpening the how.

1. **RDAP-first is correct, and Python has mature libraries for it.** `whoisit` (BSD-3), `whodap` (MIT),
   and `asyncwhois` (dual WHOIS+RDAP) all expose the lifecycle primitives Phase 4 needs. **Pick `whodap`
   as the async RDAP client** (see [§4.4](#44-phase-4--rdap-verification)); `whoisit` is the clean sync
   fallback.
2. **Two surveyed tools are pure anti-patterns** confirming our decisions: Williams-Media and twiny/spidy
   use deprecated **port-43 WHOIS**; Domain Hunter and Expireddomains-Fast-Checker **scrape an
   authenticated ExpiredDomains.net** (Selenium/Chrome, CAPTCHA-OCR). We ingest feed files + RDAP instead.
3. **Drop-date = model the RDAP event lifecycle**, per domain-watchdog: registration → expiration →
   redemption → pendingDelete → deletion. Compute from `expiration_date` + status list + fixed ICANN
   durations (~45d grace + ~30d redemption + 5d pendingDelete ≈ **80 days** expiry→drop).
4. **Discover the Verisign `.com` RDAP endpoint via IANA bootstrap** (RFC 9224), never hardcode it —
   though for a `.com`-only tool the resolved endpoint (`rdap.verisign.com/com/v1/`) is stable enough to
   cache.
5. **Build your own rate-limiter + cache.** `whoisit` ships no throttling and no retry; the maturity in
   domain-watchdog is a *caller-side* rate-limit + cache layer, which we replicate in async Python.
6. **Filtering/scoring:** `wordfreq.zipf_frequency` as a *graded* dictionary threshold (not binary);
   pronounceability as **character n-gram phonotactics** (not CVC/CMUdict); a **local-deterministic-vs-AI
   split** with **data-calibrated weights** (validated by both the arXiv brand-name paper and domainsearcher-app).
7. **One correction to flag:** Dynadot's "Inactive Domains" is an *account dashboard of your own domains*,
   not a public drop feed — see [§7](#7-open-decisions--corrections-needed).

---

## 3. Surveyed-tool teardown

### 3.1 The six named projects

| # | Project | Stack / License | Problem it solves | Borrow | Avoid |
|---|---------|-----------------|-------------------|--------|-------|
| 1 | **maelgangloff/domain-watchdog** | PHP/Symfony, Redis · **AGPL-3.0** ⚠️ *(copyleft — study only)* | RDAP monitoring + auto-acquisition; models a domain "from inception to release" | **RDAP event-lifecycle model** (Event = action+immutable date; status JSON array; `deleted` bool; `isPendingDelete()`/`isRedemptionPeriod()`/`getExpiresInDays()`); **rate-limit + cache to minimize RDAP calls** | Its Redis/Symfony machinery — overkill for a once-daily SQLite batch. **AGPL means don't copy code.** |
| 2 | **threatexpress/domainhunter** | Python · license unverified ⚠️ | Red-team: find aged expired domains with clean categorization | **Multi-source reputation/toxicity pattern** (`checkBluecoat`/`checkIBMXForce`/`checkTalos` + Umbrella/McAfee/malwaredomains/Archive.org) for our Phase-5 toxicity gate | **Scrapes authenticated `member.expireddomains.net`** (mandatory login, BeautifulSoup, pytesseract OCR to beat CAPTCHAs). Do not ingest this way. Reputation *endpoints* are also brittle now (BlueCoat added CAPTCHA) — borrow the pattern, re-pick sources. |
| 3 | **Williams-Media/Exipred-Domain-Finder** *(sic — repo really is spelled "Exipred")* | Python · license unverified | Crawl for expired domains | Nothing structural | **Status solely via `whois.whois().expiration_date`** — port-43 WHOIS, the deprecated approach we reject. No RDAP/DNS path. |
| 4 | **twiny/spidy** | Go · license unverified | Concurrent domain-availability checker | Concurrency model is fine conceptually | **Port-43 WHOIS** (`twiny/whois`, `twiny/domaincheck` dial TCP :43). No RDAP. Wrong protocol for us; wrong language anyway. |
| 5 | **thejacedev/Expireddomains-Fast-Checker** | Python (Selenium) · license unverified | Bulk-check ExpiredDomains.net listings | Nothing structural | **Selenium + real Chrome + manual login + BeautifulSoup**, warns about CAPTCHA/anti-bot. Maximum-fragility ingestion. |
| 6 | **Hosteroid/domain-monitor** | PHP · license unverified | Multi-TLD domain expiry monitor (1,400+ TLDs) | **IANA RDAP bootstrap done right:** parses `https://data.iana.org/rdap/dns.json` `services` array (TLD patterns → RDAP URLs); code comment *"DO NOT guess RDAP URLs — they must be from official sources."* RDAP-first with WHOIS fallback. | ⚠️ Its README does **not** actually document rate-limiting/backoff (a claim to that effect was **refuted 0-3**) — don't assume it as a reference for throttling. |

### 3.2 Libraries & references worth adopting

| Source | Stack / License | Role for DomainScout |
|--------|-----------------|----------------------|
| **meeb/whoisit** | Python (`requests`+`dateutil`) · **BSD-3-Clause** ✅ | Sync RDAP client. IANA bootstrapping; parses to flat dicts with **datetime-typed** `registration_date`/`expiration_date`/`last_changed_date` and **status as a list**. ⚠️ **Synchronous, no built-in throttling, no auto-retry** (raises `QueryError`). *(A claimed native async interface was refuted 1-2 — treat whoisit as sync-only.)* Good clean fallback / reference implementation. |
| **pogzyb/whodap** | Python (`httpx`) · **MIT** ✅ | **Async** RDAP client (`aio_lookup_domain`, `new_aio_client`). **Recommended primary** for Phase 4 (async fits the daily batch; MIT is permissive). |
| **pogzyb/asyncwhois** | Python · license unverified | Dual WHOIS **and** RDAP, paired sync/async (`rdap()`/`aio_rdap()`), returns `(query_string, parsed_dict)` normalizing `created`/`expires`/`updated` as datetime. RDAP transport delegates to `whodap`→`httpx`; WHOIS half uses raw sockets. Viable alternative to whodap if we ever want a WHOIS cross-check. |
| **rspeer/wordfreq** | Python · (already a ratified dependency) | Dictionary matching as a **graded frequency threshold**. `zipf_frequency(w,'en')` on a base-10 log scale (Zipf 6 ≈ once/thousand words, Zipf 3 ≈ once/million); `word_frequency()`→0–1; `top_n_list()`/`get_frequency_dict()` to pull a curated dictionary. ⚠️ Measures *corpus commonness, not dictionary membership* — a heuristic threshold, exactly as intended. |
| **arXiv 1706.09335** — *Generating Appealing Brand Names* (Gangal et al., 2017) | Academic paper | Concrete math for name quality: appeal = weighted sum of **readability, pronounceability, memorability, uniqueness**; pronounceability via **character n-grams** (l∈{2,3,4}, back-off weighted); weights learned by **Rank-SVM** on human pairwise comparisons (readability weighted highest). ⚠️ Small sample (20 people/315 comparisons); dims map only loosely to our rubric — borrow the **decompose-and-calibrate** principle, not the numbers. |
| **lukem512/pronounceable** | JavaScript · license unverified | Reference implementation of n-gram pronounceability: bigram+trigram frequency tables from a wordlist; `score()` sums trigram probs / word length, falls back to bigrams for <3-char strings (e.g. `peonies`≈0.102 vs `sshh`≈0.00086). Port the *idea* to Python; don't ship the JS. |
| **vasilytrofimchuk/domainsearcher-app** | (app) · license unverified | Validates our **hybrid scoring split**: `LEN`+`ZON` computed locally/deterministically; `PRO`/`MEM`/`BRD`/`FIT` **AI-scored in a single call**. Also a clean **two-stage availability check**: RDAP (200=taken / 404=unconfirmed) then **DNS-over-HTTPS** A-record vs Cloudflare `1.1.1.1` (NXDOMAIN=available). |
| **WhoisFreaks free feed** (GitHub) + docs | Data source (ratified) | Feed format spec — see [§4.2](#42-phase-2--ingestion). |
| **IANA RDAP bootstrap** `data.iana.org/rdap/dns.json` (RFC 9224/7484) | Registry data | Authoritative TLD→RDAP-endpoint map; resolves `.com`→`rdap.verisign.com/com/v1/`. |

---

## 4. Architecture

### 4.1 Module boundaries & data flow

Each phase is a standalone, idempotent module with a narrow interface, runnable via `python -m domainscout.<module>`. State passes **through the SQLite `candidates` table**, never in-memory between phases — this is what makes every phase independently re-runnable (the core idempotency requirement).

```
                 ┌─────────────┐
   feed files →  │ 2. ingest   │  upsert rows            (idempotent on UNIQUE(domain,drop_date))
                 └──────┬──────┘
                        ▼
                 ┌─────────────┐
                 │ 3. filter   │  deterministic, cheap   (length/charset/dict/pronounceability)
                 └──────┬──────┘   writes filter_pass + filter_reason
                        ▼  (survivors only)
                 ┌─────────────┐
   RDAP/DNS ←──  │ 4. verify   │  async, rate-limited    (status, drop_date, verified_at)
                 └──────┬──────┘
                        ▼
                 ┌─────────────┐
   AI API   ←──  │ 5. score    │  Tier-1 triage → Tier-2 (scores JSON, rationale, action, scored_at)
                 └──────┬──────┘   + toxicity + comps context
                        ▼
                 ┌─────────────┐
                 │ 7. digest   │  ranked markdown report
                 └─────────────┘
   6. outcomes: separate module, writes back real-world results for rubric calibration
```

**Proposed package layout** (Phase 1 creates the skeleton; later phases fill modules):

```
domainscout/
  __init__.py
  __main__.py          # argparse dispatch → subcommands (init-db, ingest, filter, verify, score, digest, outcome)
  config.py            # load + validate criteria.toml (tomllib)
  db.py                # connection, schema DDL, migrations, upsert helpers
  models.py            # dataclasses: Candidate, RdapResult, Scores
  ingest.py            # Phase 2
  filters.py           # Phase 3 (length, charset, dictionary, pronounceability)
  pronounce.py         # n-gram pronounceability scorer (+ trained tables)
  rdap.py              # Phase 4: async client wrapper, rate-limit, drop-date computation
  scoring/
    __init__.py
    base.py            # score(domain, context) -> JSON  (provider-agnostic interface)
    anthropic.py       # default provider (Haiku triage + Sonnet deep, Batch API)
  toxicity.py          # Phase 5 pre-score gate (Wayback + Safe Browsing)
  outcomes.py          # Phase 6
  digest.py            # Phase 7
criteria.toml          # tunable rules (owner-editable)
data/
  domainscout.db       # SQLite (gitignored)
  ngram_tables.json    # pronounceability model (checked in or generated)
docs/
  TECHNICAL-DESIGN.md  # this file
tests/
```

### 4.2 Phase 2 — Ingestion

**Source (per `DECISIONS.md`): WhoisFreaks free GitHub feed** (+ Dynadot — see [§7](#7-open-decisions--corrections-needed)).

Verified feed facts to design against:
- **~10,000 domains/day cap** (a subset of the ~400k/day commercial set) — daily volume is bounded at ~10k rows *before* filtering.
- **Date-stamped CSV files**, split by lifecycle category: `YYYY-MM-DD-free-expired-domains.csv` and `YYYY-MM-DD-free-dropped-domains.csv`. The parser locates the file by date + category and **labels each row with its `source` and lifecycle category**.
- **Published ~03:00 UTC covering the *previous* day**; free feed lags real-time by ~1 day. → cron runs after 03:00 UTC; drop-date/timing must account for ~24h staleness (acceptable since drop-catching is outsourced).
- **Date-addressable** (fetch a specific day's file) → **re-downloading a given date is idempotent**, which pairs exactly with our `UNIQUE(domain, drop_date)` identity model.

**Idempotency mechanism** (from the idempotent-pipeline reference): `INSERT ... ON CONFLICT(domain, drop_date) DO UPDATE` (SQLite `INSERT OR REPLACE` semantics). Re-running a day converges to identical state. `first_seen` is set on insert only (`ON CONFLICT` preserves it); a `last_seen`/counter can update.

### 4.3 Phase 3 — Rules filter

Runs first, cheap, deterministic, **fully logged** (`filter_pass` + `filter_reason` per domain). Order gates cheapest-first:

1. **Charset / shape:** `^[a-z]+$`, no hyphens/numbers (regex). Reject fast.
2. **Length:** ≤8 (primary) or 9–12 (secondary), per `criteria.toml`.
3. **Dictionary (graded, via `wordfreq`):** score the domain stem / word-split with `zipf_frequency`; a **tunable Zipf threshold** decides "real word" rather than a binary wordlist hit. Two-word combos scored by the min/mean of their parts' frequencies.
4. **Pronounceability (n-gram phonotactics, `pronounce.py`):** bigram/trigram frequency model trained on an English wordlist; score = mean trigram log-prob (bigram fallback for short strings). **Not** CVC rules, **not** CMUdict (can't cover invented-but-pronounceable words — a secondary-target requirement). ⚠️ Use a *whole-word average*, **not** lukem512's hard per-trigram floor (one unusual trigram shouldn't nuke a good coined word).

Target output: ~50–200 survivors/day → Phase 4.

### 4.4 Phase 4 — RDAP verification

- **Client:** `whodap` (async, MIT) as primary; `whoisit` (sync, BSD-3) as reference/fallback.
- **Endpoint discovery:** resolve via **IANA bootstrap** (`data.iana.org/rdap/dns.json`); cache the resolved `.com`→`rdap.verisign.com/com/v1/` mapping to disk with a staleness check. Never hardcode-guess.
- **Status semantics:** RDAP signals registration by **HTTP code** (200 = registered + JSON, 404 = not in registry) — no WHOIS free-text regex. Tag `rdap_status` from the returned **status list** (`autoRenewPeriod`, `redemptionPeriod`, `pendingDelete`, etc.).
- **Drop-date computation (the domain-watchdog model, in Python):**
  `drop_date ≈ expiration_date + grace(≈45d) + redemption(≈30d) + pendingDelete(5d)`, **refined** when the status list reveals the current phase (e.g. `pendingDelete` present → drop within ~5 days). Prefer exact phase-start dates from the RDAP `events` array (raw mode) when present; fall back to fixed ICANN durations. ⚠️ Exact durations + whether Verisign emits phase-start events = [open question](#7-open-decisions--corrections-needed).
- **Rate-limiting & blocks (build it ourselves):** async semaphore + token-bucket + exponential backoff on errors; a per-run response cache so re-runs don't re-hit. Use a dedicated client against the **direct Verisign endpoint** (not the public `rdap.org` aggregator) for a daily batch. Set a descriptive `User-Agent`.
- **Optional cheap pre-filter (per `DECISIONS.md` "DNS only as optional pre-filter"):** DNS-over-HTTPS A/NS check vs Cloudflare `1.1.1.1` (NXDOMAIN ⇒ likely available) *before* spending an RDAP call, following domainsearcher-app's two-stage pattern.

### 4.5 Phase 5 — Two-tier AI scoring

- **Provider-agnostic interface** `score(domain, context) → JSON` (ratified), default Anthropic (Haiku triage → Sonnet deep, Batch API).
- **Hybrid local/AI split** (validated by domainsearcher-app + arXiv): deterministic dimensions (length, availability/lifecycle, dictionary/pronounceability scores from Phase 3) are computed locally and **passed into** the prompt as context; only the subjective dimensions (**brandability, memorability, commercial potential, linguistic clarity**) are AI-scored.
- **Rubric weights are data-calibrated, not hand-set** — start with reasoned defaults, tune against Phase 6 outcomes (the arXiv Rank-SVM approach is the template; its actual weights are illustrative only).
- **Toxicity gate before scoring** (`toxicity.py`): Wayback CDX history *shape* + Google Safe Browsing (both free), following domainhunter's *multi-source reputation* pattern (re-pick live sources; BlueCoat is CAPTCHA-hardened now).
- **Comps grounding:** inject NameBio-style comparable sales into the Tier-2 prompt so output references a realistic value range — integration source is an [open question](#7-open-decisions--corrections-needed).

### 4.6 Phases 6–7 — Outcomes & digest

- **Outcomes** (`outcomes.py`): writes real-world results (`outcome`, `outcome_price`, `outcome_date`) back onto candidate rows → feeds rubric calibration. First-class, not an afterthought.
- **Digest** (`digest.py`): local markdown, top ~10, per ratified defaults; ranked with score + rationale + drop date + recommended action (register/backorder/bid/skip).

---

## 5. Data schema

Per ratified proposal #7 and the owner's chosen identity model:

```sql
CREATE TABLE candidates (
  id            INTEGER PRIMARY KEY,          -- surrogate key; FK target for future tables
  domain        TEXT NOT NULL,
  drop_date     DATE,                         -- separate column (range-queryable), not concatenated
  source        TEXT,                         -- feed + lifecycle category (expired/dropped)
  first_seen    TIMESTAMP NOT NULL,           -- set on insert only
  -- filter (Phase 3)
  filter_pass   BOOLEAN,
  filter_reason TEXT,                         -- auditable pass/fail reason
  -- rdap (Phase 4)
  rdap_status   TEXT,
  verified_at   TIMESTAMP,                    -- idempotent re-run guard
  -- scoring (Phase 5)
  tier1_score   REAL,
  tier2_scores  TEXT,                         -- JSON: per-dimension
  rationale     TEXT,
  recommended_action TEXT,
  scored_at     TIMESTAMP,                    -- idempotent re-run guard
  -- outcomes (Phase 6)
  outcome       TEXT,
  outcome_price REAL,
  outcome_date  DATE,
  UNIQUE(domain, drop_date)                   -- natural/alternate key; re-registration cycles → new row
);
CREATE INDEX idx_candidates_drop_date ON candidates(drop_date);
CREATE INDEX idx_candidates_filter_pass ON candidates(filter_pass);
```

`verified_at` / `scored_at` let each phase skip already-processed rows on re-run (idempotency), while re-running is still *safe* (upsert converges).

---

## 6. Anti-patterns we're designing around

Each is a concrete mistake observed in a surveyed tool — all verified.

1. **Port-43 WHOIS for status** (Williams-Media; twiny/spidy). Deprecated, free-text, brittle. → **RDAP-first**, HTTP-status + JSON.
2. **Scraping ExpiredDomains.net's authenticated UI** (Domain Hunter: OCR CAPTCHAs; Fast-Checker: Selenium/Chrome + manual login). Fragile, login-gated, anti-bot-hostile. → **ingest static feed files** + RDAP.
3. **Hardcoding/guessing the RDAP endpoint.** → **IANA bootstrap** (domain-monitor's explicit *"DO NOT guess RDAP URLs"*).
4. **Assuming the client rate-limits for you.** `whoisit` doesn't throttle or retry. → **caller-side** rate-limit + backoff + cache (the domain-watchdog maturity, reimplemented).
5. **Binary wordlist membership** for "is it a real word." → **graded `zipf_frequency` threshold** (handles brands/rare words gracefully).
6. **Rigid CVC / CMUdict pronounceability.** Can't score invented-but-pronounceable coinages (a secondary-target need). → **character n-gram phonotactics.**
7. **Hard per-trigram rejection floor** (lukem512). One odd trigram fails a whole good word. → **whole-word average** score.
8. **Hand-set scoring weights.** → **calibrate against Phase 6 outcomes.**
9. **In-memory state between phases.** → **state in SQLite**, every phase idempotent & standalone.

> **Refuted / excluded (did not survive verification):** (a) that Hosteroid/domain-monitor documents rate-limiting — **refuted 0-3**, so don't cite it for throttling; (b) that `whoisit` has a native async interface + shared connection pools — **refuted 1-2**, so treat `whoisit` as synchronous.

---

## 7. Open decisions & corrections needed

These need owner input; none are changed unilaterally.

1. **⚠️ Dynadot source correction.** `CLAUDE.md`/`DECISIONS.md` list "Dynadot drop lists" as a free source. The survey found Dynadot's **"Inactive Domains" is an account-level dashboard of your *own* expired/moved domains — not a public downloadable drop feed.** Options: (a) drop Dynadot as an ingestion source and rely on the WhoisFreaks free feed alone for now; (b) find Dynadot's actual public auction/expired-listing export if one exists; (c) verify before Phase 2. **Recommend (a)** until a real Dynadot feed is confirmed.
2. **`.com` lifecycle durations to hardcode.** Confirm exact ICANN/Verisign grace / redemption (≈30d) / pendingDelete (5d) values, and whether Verisign `.com` RDAP emits **precise phase-start events** in its `events` array (exact drop math) or whether we must estimate from `expiration_date` + status.
3. **Verisign RDAP limits.** Does `rdap.verisign.com/com/v1` publish rate limits / require a specific `User-Agent`? Direct endpoint vs `rdap.org` aggregator trade-off for a daily batch.
4. **Comps integration.** No surveyed tool grounds scores in real sales. Which NameBio-style source feeds the Tier-2 "value range" (ties to `DECISIONS.md` pending proposal #3: NameBio Basic $10/mo + local comps cache)?
5. **License gate before any code reuse.** Only `whoisit` (BSD-3) and `whodap` (MIT) licenses are verified-permissive. Domain Hunter, domainsearcher-app, spidy, Williams-Media, and the WhoisFreaks feed licenses are **unverified** — treat all as *patterns-only* until checked. domain-watchdog is **AGPL-3.0** (copyleft — do not copy code).

---

## 8. Proposed dependencies

| Dependency | Purpose | License | Notes |
|-----------|---------|---------|-------|
| `whodap` | async RDAP client (Phase 4 primary) | MIT ✅ | httpx-based |
| `whoisit` | sync RDAP reference/fallback | BSD-3 ✅ | no throttling/retry — wrap it |
| `wordfreq` | graded dictionary matching | (ratified) | commonness, not membership |
| `aiohttp` / `httpx` | async I/O (RDAP, DoH, feed fetch) | permissive | per `CLAUDE.md` conventions |
| `tomllib` | criteria config parsing | **stdlib** (3.11+) | zero-dep, comments allowed |
| `sqlite3` | storage | **stdlib** | — |
| Anthropic SDK | Phase 5 scoring | — | behind provider-agnostic interface; needs API key (Pro plan ≠ API credits) |

Stdlib-first keeps the Windows-local → VPS move trivial (ratified infra plan).

---

## 9. Sources & evidence

All claims 3-0 adversarially verified against primary sources unless marked ⚠️. Full run:
`.../workflows/wf_dd773f78-442`.

**Projects:** [domain-watchdog](https://github.com/maelgangloff/domain-watchdog) ·
[domainhunter](https://github.com/threatexpress/domainhunter) ·
[domain-monitor](https://github.com/Hosteroid/domain-monitor) ·
[Expireddomains-Fast-Checker](https://github.com/thejacedev/Expireddomains-Fast-Checker) ·
[Exipred-Domain-Finder](https://github.com/Williams-Media/Exipred-Domain-Finder) ·
[spidy](https://github.com/twiny/spidy)

**Libraries:** [whoisit](https://github.com/meeb/whoisit) ·
[whodap](https://github.com/pogzyb/whodap) ·
[asyncwhois](https://github.com/pogzyb/asyncwhois) ·
[wordfreq](https://github.com/rspeer/wordfreq) ·
[pronounceable](https://github.com/lukem512/pronounceable) ·
[domainsearcher-app](https://github.com/vasilytrofimchuk/domainsearcher-app)

**References:** [arXiv 1706.09335](https://arxiv.org/pdf/1706.09335) ·
[IANA RDAP bootstrap](https://data.iana.org/rdap/dns.json) ·
[rcode3 RDAP libraries](https://rdap.rcode3.com/client_implementations/libraries.html) ·
[WhoisFreaks free feed](https://github.com/WhoisFreaks/daily-expired-and-dropped-domains) ·
[WhoisFreaks docs](https://whoisfreaks.com/documentation/expiring-dropped-domains) ·
[idempotent-pipeline design](https://fawadhs.dev/blog/idempotent-data-pipeline-design-safe-rerun) ·
[.com lifecycle](https://whoisjson.com/blog/domain-expires-how-to-detect) ·
[RDAP availability](https://rdapapi.io/blog/check-domain-availability-with-rdap)
