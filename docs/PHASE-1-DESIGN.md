# Phase 1 — Skeleton: design

**Status:** ✅ **BUILT 2026-07-14** (owner final approval, review round 2). Three trivia items folded in below
(`[ingestion]` config table, `dismissed` reachability via `outcome --dismiss`, `.env.example` Dynadot placeholder).
Implemented per docs/superpowers/plans/2026-07-14-phase-1-skeleton.md.
Next: writing-plans → Phase-1 implementation plan → build.
**Parent:** [`docs/TECHNICAL-DESIGN.md`](TECHNICAL-DESIGN.md) (the TDD is the design spec; this is the Phase-1 slice).

## Goal / definition of done
A runnable, tested scaffold with **no pipeline logic yet**: create the DB, load+validate criteria, and
invoke every phase as a CLI subcommand (most print "not implemented"). Foundation for later phases.

## Decisions folded in
- **CLI framework: stdlib `argparse`** with sub-parsers (zero-dep, portable `python -m domainscout <cmd>`,
  matches budget/minimal-dep leaning). *Typer was the alternative — rejected to avoid a dependency; revisit if the CLI grows.*
- **Config format: TOML** (`tomllib`, stdlib 3.11+) — ratified.
- **Schema: open-cycle identity model** (TDD §5) — ratified **and amended (review round 2)**: `dropped` stays OPEN;
  index predicate `NOT IN ('renewed','reregistered','dismissed')`; `lifecycle_status NOT NULL DEFAULT 'unknown'`;
  plus an `ingest_log` counts table. `init-db` creates both tables.
- **CLI reflects the batch split:** `score-submit` + `score-collect` (not one `score`); Phase-8 `web` stub is **FastAPI**.
- **Packaging:** `pyproject.toml` (metadata + deps), Python 3.11+.

## Layout (Phase 1: ✅ = real, ▫️ = stub)
```
domainscout/
  __main__.py     ✅ argparse dispatch: init-db + stubs (ingest/filter/verify/score-submit/score-collect/digest/outcome[--dismiss intent noted]/prune/web)
  config.py       ✅ load + validate criteria.toml
  db.py           ✅ schema DDL (open-cycle candidates table + ingest_log) + init-db + connection/upsert helpers
  models.py       ✅ dataclasses (Candidate, …)
  ingest.py …     ▫️ stub modules (NotImplementedError / "phase N not built")
  sources/ scoring/ web/   ▫️ package dirs, __init__ only (web/ → FastAPI in Phase 8)
criteria.toml     ✅ owner criteria as tunable config
pyproject.toml    ✅ metadata + deps
.env.example      ✅ keys: ANTHROPIC_API_KEY, GOOGLE_SAFE_BROWSING_API_KEY, # DYNADOT_API_KEY (commented, optional) — no secrets
data/             ✅ created at runtime (gitignored)
tests/            ✅ tests for config + db (written first — TDD)
README.md         ✅ short "how to run"
```

## What works after Phase 1
- `python -m domainscout init-db` → creates `data/domainscout.db` with the full open-cycle schema (`candidates`
  with the amended partial unique index + `ingest_log`), idempotent.
- `python -m domainscout <phase>` → dispatches, prints a "not implemented" notice (incl. `score-submit`/`score-collect`).
- `config.py` loads `criteria.toml`, validates it, surfaces clear errors.

## `criteria.toml` sketch (real values, all tunable)
```toml
[ingestion]    # the hard-invariant gate (TDD §4.2) — applied on the way in
tld = "com"                 # .com only, ever
charset = "^[a-z]+$"        # no hyphens/numbers; shared by both tracks (lifted out of [primary])
sources = ["whoisfreaks", "dynadot"]
schedule_hint = "late-morning"   # WhoisFreaks feed has ~1-day lag; don't race the upload
# length ceiling is DERIVED in code = max(primary.max_length, secondary.max_length) — no duplicated value
[primary]      # ≤8-char dictionary .com
max_length = 8
max_words = 2
[secondary]    # 9–12-char invented / geo+service
min_length = 9
max_length = 12         # widest target → also the derived ingestion length ceiling
[dictionary]
zipf_min = 3.0          # wordfreq threshold (tunable)
[pronounceability]
min_score = 0.02        # n-gram floor (tunable, calibrate later)
[scoring]
tier2_cutoff = 30
digest_top_n = 10
[rdap]
endpoint = "https://rdap.verisign.com/com/v1/"
max_requests_per_sec = 1.0
[retention]
days = 360
```

## Testing
TDD flow: write tests **first** for the two units with real logic — config load/validate and DB schema
creation/upsert — then implement to green. Phase stubs need no tests yet.

## Process
TDD serves as the design spec → skip a redundant spec file → on approval, write a focused Phase-1
**implementation plan** (writing-plans skill), then build.

## Notes recorded now for later phases (no Phase-1 build)
- **`dismissed` reachability:** the `dismissed` lifecycle state is set via the UI in Phase 8, but to keep it
  reachable before then, Phase 6's `outcome` subcommand doubles as the manual dismissal path
  (`outcome <domain> --dismiss` → `lifecycle_status='dismissed'`, closing the cycle). The Phase-1 `outcome`
  **stub's help text records this intent now** so the state is never unreachable-by-design.
- **`.env.example`** includes a commented-out `# DYNADOT_API_KEY=` with a note: *Phase 2, optional — the public
  expired-auction CSV needs no key; this is only for the account-keyed aftermarket API.* Documents the
  keyless-public vs account-keyed distinction up front.

## Approval
- ✅ Owner final approval 2026-07-14 (review round 2), incl. the `[ingestion]` config table + the two trivia notes above.
- Next step: **writing-plans** skill → focused Phase-1 implementation plan → build.
