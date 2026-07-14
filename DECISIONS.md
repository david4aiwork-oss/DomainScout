# DomainScout — Decision Log & Research Notes

Canonical record of owner decisions, the research behind them, and proposals still awaiting a call.
CLAUDE.md stays the lean build spec; this file holds the *why* and the numbers.

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

### Accepted defaults (owner didn't object; cheap to change)
- Daily digest: **local markdown file**, top **10** candidates, Tier-2 scoring cutoff ~**30** domains.

---

## Pending proposals (raised 2026-07-13, not yet ratified — decide at the relevant phase)

| # | Phase | Proposal |
|---|---|---|
| 1 | 5/7 | Treat digest as an **opportunity-ranker**, not a gem-finder: skew Tier-2 spend toward secondary target + auction candidates. Primary target (≤8-char dictionary .coms) essentially never drops uncaught. |
| 2 | 4 | **Compute** drop dates from Verisign RDAP status + events (expiry → 0–45d auto-renew grace → 30d redemptionPeriod → 5d pendingDelete → drop) rather than only observing them. |
| 3 | 5 | Comps grounding: NameBio **Basic $10/mo** + export → build a **local comps cache** per pattern, refreshed monthly, injected into Tier-2 prompts. (NameBio's API tiers are too credit-starved for live lookups — see snapshot.) |
| 4 | 5 | Trademark screen via **USPTO trademark search API** (free) — never LLM recall. Highest stakes for geo+service names (UDRP risk). |
| 5 | 3 | Pronounceability: char-level **n-gram model** trained on English words (or CVC-pattern scorer) — CMUdict can't cover invented words. |
| 6 | 5 | Toxicity screen: Wayback CDX API (free) + Google Safe Browsing (free w/ key) now; backlink-anchor check **deferred** (no good free API). |
| 7 | 1 | Schema: unique key **(domain, drop_date)** — not `domain` alone (re-registration cycles) — plus `verified_at` / `scored_at` timestamps for idempotent re-runs. |

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
