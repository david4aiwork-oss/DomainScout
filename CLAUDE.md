# DomainScout

Personal expired-domain discovery pipeline. Finds quality expiring/dropped **.com** domains for personal use, with occasional high-value flips. This is NOT a volume trading operation — quality over quantity, a handful of great candidates per week beats hundreds of mediocre ones.

## Owner's Domain Criteria

**Primary target:**
- .com only (no other TLDs, ever)
- 1–2 dictionary words
- Max 8 characters
- No hyphens, no numbers

**Secondary target (less bot competition, actively scored):**
- Two-word combos, 9–12 characters
- Invented-but-pronounceable words
- Generic geo+service combos (e.g. `austinplumber.com` pattern) — no defunct-business names (trademark/UDRP risk)

**Market reality (calibrate expectations):** short dictionary .coms almost never drop uncaught — they go to backorder/auction. The pipeline's job is to *identify and rank opportunities* (including auction candidates), not to find free gems. The decision output is: hand-register / backorder ($59–79) / bid at auction / skip.

## Architecture (build in phases, test each with real data before proceeding)

1. **Skeleton** — SQLite DB, config file for criteria, CLI structure
2. **Ingestion** — daily pull into `candidates`. Apply the **hard-invariant gate at ingestion** (.com → charset `^[a-z]+$` → length ≤12, the non-tunable criteria) so only survivors land in the permanent DB; log per-run counts to `ingest_log`. Retained feeds (360d) allow re-ingest if criteria loosen. Dedup via open-cycle upsert; idempotent daily runs. Schedule late-morning (feed has ~1-day lag)
3. **Rules filter** — deterministic, cheap (charset+length already gated at ingestion). Handles the **tunable** gates: primary/secondary classification (≤8 vs 9–12), dictionary matching via `wordfreq` (frozen at 2024 — emerging terms are Tier-2's job), pronounceability heuristic. Must log pass/fail reason per domain. Should cut candidates to ~50–200/day
4. **RDAP verification** — async, rate-limited (own backoff + cache; query the Verisign `.com` endpoint directly, NOT the rdap.org aggregator which caps at 10 req/10s). Tag status and compute drop date **status-driven** (fixed 30d redemption + 5d pendingDelete tail; the pre-drop auto-renew grace is registrar-variable). Use RDAP, NOT port-43 WHOIS (deprecated). Client: `whodap` (async, MIT)
5. **Two-tier AI scoring** (Anthropic API):
   - Tier 1: cheap model (Haiku) coarse triage on rules-filter survivors
   - **Toxicity gate on Tier-1 survivors** (between tiers — keeps slow CDX/Safe-Browsing calls off the critical path)
   - Tier 2: strong model (Sonnet) deep scoring on top 20–30 only, with context injected (see Scoring Rubric)
   - Batch API is async (hours) → split into `score-submit` / `score-collect` so the cron stays idempotent
   - Structured JSON output: per-dimension scores + one-line rationale
6. **Outcomes tracker** — log every scored domain's real-world result (backordered by others? auction price? unsold?) for rubric calibration
7. **Daily digest** — ranked report: top candidates, scores, rationale, drop date, recommended action (register/backorder/bid/skip)
8. **Local review UI** — FastAPI + uvicorn app to view/filter the DB and Phase 6/7 results, with write-back to mark outcomes and tune criteria (feeds the calibration loop)

## Scoring Rubric (Tier 2)

Dimensions: **brandability, memorability, commercial potential, linguistic clarity**.
Plus checks: radio test (spellable after hearing it), plural/typo confusability, quick trademark screen.

**Ground scores in comps, not vibes:** inject real comparable-sale stats from **NameBio's free API** (RetailStats/TLDStats, cached CSV, attribution required) plus a modeled value range from **HumbleWorth** (open-source; hosted endpoint on Windows-local, self-host on VPS later) into the scoring prompt. Output should reference a realistic value range, not abstract scores alone. (NameBio Basic $10/mo kept as a future upgrade; verify the free tier with a ~30-min spike before Phase-5 prompt design. See docs/TECHNICAL-DESIGN.md §4.5.)

**Toxicity screen between Tier-1 and Tier-2** (on Tier-1 survivors only): Wayback history *shape* (long-lived real business gone dark = good; content flip to gambling/pharma = reject), Google Safe Browsing check (needs a free-tier Google Cloud API key), backlink anchor sanity if API available.

## Key Decisions (do not relitigate without owner)

> Full decision log with rationale, pricing research, and pending proposals: see **DECISIONS.md**; technical design (architecture, schema, prior-art survey): see **docs/TECHNICAL-DESIGN.md**. Read both at session start alongside this file.

- RDAP-first verification; DNS only as optional cheap pre-filter
- Data source (decided 2026-07-13; refined 2026-07-14): start FREE — **WhoisFreaks free GitHub feed** (10k/day subset, github.com/WhoisFreaks/daily-expired-and-dropped-domains) = domain-**names-only** firehose (~50% .com; filter to .com ourselves; lifecycle comes from RDAP) → hand-register/backorder branch; **Dynadot public expired-*auction* CSV** → bid-at-auction branch. (Correction: "Dynadot drop lists" was inaccurate — Dynadot's public data is auction inventory, not a registry drop list; its "Inactive Domains" page is account-only.) Upgrade to paid WhoisFreaks ($59–70/mo) only after the pipeline proves itself. NOT Verisign zone files (overkill). See docs/TECHNICAL-DESIGN.md §4.2
- AI scoring: build the scorer behind a provider-agnostic interface (`score(domain, context) → JSON`); provider/model are config values. Default Anthropic (Haiku triage + Sonnet deep, Batch API for the nightly run ≈ $8/mo); OpenAI minis are a viable swap — cost difference is noise, let Phase 6 outcomes settle it if curious
- No CZDS (com-only makes it unnecessary)
- Actual drop-catching is outsourced to backorder services — our edge is selection, not speed
- Feedback loop (Phase 6) is first-class, not an afterthought: rubric gets tuned against real auction outcomes
- Local strategy limited to generic geo+service names; no defunct-business domain targeting

## Data Schema (starting point)

`candidates` (open-cycle identity model — full detail in docs/TECHNICAL-DESIGN.md §5): `id` (PK), domain, source, feed_category (expired/dropped), first_seen, expiry_date, drop_date_est, drop_date_actual, lifecycle_status, rdap_status, verified_at, filter_pass (bool), filter_reason, tier1_score, tier2_scores (JSON), value_range (JSON), rationale, recommended_action, scored_at, outcome, outcome_price, outcome_date.

Identity = surrogate `id` PK + partial unique index `UNIQUE(domain) WHERE lifecycle_status NOT IN ('renewed','reregistered','dismissed')` — one *open* cycle per domain, closed rows retained as history (supersedes proposal #7's `UNIQUE(domain, drop_date)`; the estimated drop date must not be in the key). **`dropped` stays OPEN** — a dropped-and-available domain is the live hand-register opportunity, so cycles close only on `reregistered`/`renewed`/`dismissed`; `lifecycle_status` is `NOT NULL DEFAULT 'unknown'` (a NULL would silently escape the partial index). Ingestion sets `feed_category` from the filename but leaves `lifecycle_status='unknown'` for RDAP to fill. `drop_date_est` is a mutable estimate; `drop_date_actual` is set only on confirmed drop.

## Conventions

- Python 3.11+, async where I/O-bound (`aiohttp`, `aiodns`)
- All API keys in `.env`, never committed
- Every module runnable standalone via CLI for testing
- Log liberally — filter/scoring decisions must be auditable so the rubric can be tuned
- Daily run via cron; each phase must be idempotent (safe to re-run)

## Current Status

- [x] Phase 1: skeleton
- [ ] Phase 2: ingestion (free sources: WhoisFreaks free feed + Dynadot expired-auction CSV)
- [ ] Phase 3: rules filter
- [ ] Phase 4: RDAP verification
- [ ] Phase 5: AI scoring
- [ ] Phase 6: outcomes tracker
- [ ] Phase 7: daily digest
- [ ] Phase 8: local review UI (FastAPI + uvicorn — view/filter DB + Phase 6/7 results, write-back)

Update this checklist as phases complete.
