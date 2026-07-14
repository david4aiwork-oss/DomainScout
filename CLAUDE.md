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
2. **Ingestion** — daily pull of expiring .com domains from data source into `candidates` table, dedup, idempotent daily runs
3. **Rules filter** — deterministic, cheap, runs first: length, charset (regex: `^[a-z]+$`), dictionary matching via `wordfreq`, pronounceability heuristic. Must log pass/fail reason per domain. Should cut candidates to ~50–200/day
4. **RDAP verification** — async, rate-limited. Tag status (pendingDelete, redemptionPeriod, dropped) and drop date. Use RDAP, NOT port-43 WHOIS (deprecated). Verisign RDAP endpoint for .com
5. **Two-tier AI scoring** (Anthropic API):
   - Tier 1: cheap model (Haiku) coarse triage on rules-filter survivors
   - Tier 2: strong model (Sonnet) deep scoring on top 20–30 only, with context injected (see Scoring Rubric)
   - Structured JSON output: per-dimension scores + one-line rationale
6. **Outcomes tracker** — log every scored domain's real-world result (backordered by others? auction price? unsold?) for rubric calibration
7. **Daily digest** — ranked report: top candidates, scores, rationale, drop date, recommended action (register/backorder/bid/skip)

## Scoring Rubric (Tier 2)

Dimensions: **brandability, memorability, commercial potential, linguistic clarity**.
Plus checks: radio test (spellable after hearing it), plural/typo confusability, quick trademark screen.

**Ground scores in comps, not vibes:** include recent comparable sales from NameBio (same pattern/niche) in the scoring prompt. Output should reference a realistic value range, not abstract scores alone.

**Toxicity screen before scoring:** Wayback history *shape* (long-lived real business gone dark = good; content flip to gambling/pharma = reject), Google Safe Browsing check, backlink anchor sanity if API available.

## Key Decisions (do not relitigate without owner)

> Full decision log with rationale, pricing research, and pending proposals: see **DECISIONS.md**. Read it at session start alongside this file.

- RDAP-first verification; DNS only as optional cheap pre-filter
- Data source (decided 2026-07-13): start FREE — Dynadot drop lists + WhoisFreaks free GitHub feed (10k/day subset, github.com/WhoisFreaks/daily-expired-and-dropped-domains). Upgrade to paid WhoisFreaks ($59–70/mo) only after the pipeline proves itself on free data. NOT Verisign zone files (overkill at this scale)
- AI scoring: build the scorer behind a provider-agnostic interface (`score(domain, context) → JSON`); provider/model are config values. Default Anthropic (Haiku triage + Sonnet deep, Batch API for the nightly run ≈ $8/mo); OpenAI minis are a viable swap — cost difference is noise, let Phase 6 outcomes settle it if curious
- No CZDS (com-only makes it unnecessary)
- Actual drop-catching is outsourced to backorder services — our edge is selection, not speed
- Feedback loop (Phase 6) is first-class, not an afterthought: rubric gets tuned against real auction outcomes
- Local strategy limited to generic geo+service names; no defunct-business domain targeting

## Data Schema (starting point)

`candidates`: domain, source, first_seen, drop_date, rdap_status, filter_pass (bool), filter_reason, tier1_score, tier2_scores (JSON), rationale, recommended_action, outcome, outcome_price, outcome_date

## Conventions

- Python 3.11+, async where I/O-bound (`aiohttp`, `aiodns`)
- All API keys in `.env`, never committed
- Every module runnable standalone via CLI for testing
- Log liberally — filter/scoring decisions must be auditable so the rubric can be tuned
- Daily run via cron; each phase must be idempotent (safe to re-run)

## Current Status

- [ ] Phase 1: skeleton
- [ ] Phase 2: ingestion (free sources: Dynadot drop lists + WhoisFreaks free GitHub feed)
- [ ] Phase 3: rules filter
- [ ] Phase 4: RDAP verification
- [ ] Phase 5: AI scoring
- [ ] Phase 6: outcomes tracker
- [ ] Phase 7: daily digest

Update this checklist as phases complete.
