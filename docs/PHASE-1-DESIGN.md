# Phase 1 — Skeleton: design

**Status:** DRAFT — presented 2026-07-14; owner's review-round-2 points **received and folded in** (schema amendment,
ingestion gate, `score-submit`/`score-collect`, FastAPI). **Awaiting final owner approval before build.**
Not yet approved; do not start implementation until the owner confirms.
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
  __main__.py     ✅ argparse dispatch: init-db + stubs (ingest/filter/verify/score-submit/score-collect/digest/outcome/prune/web)
  config.py       ✅ load + validate criteria.toml
  db.py           ✅ schema DDL (open-cycle candidates table + ingest_log) + init-db + connection/upsert helpers
  models.py       ✅ dataclasses (Candidate, …)
  ingest.py …     ▫️ stub modules (NotImplementedError / "phase N not built")
  sources/ scoring/ web/   ▫️ package dirs, __init__ only (web/ → FastAPI in Phase 8)
criteria.toml     ✅ owner criteria as tunable config
pyproject.toml    ✅ metadata + deps
.env.example      ✅ documents required keys: ANTHROPIC_API_KEY, GOOGLE_SAFE_BROWSING_API_KEY (no secrets)
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
[primary]      # ≤8-char dictionary .com
max_length = 8
charset = "^[a-z]+$"
max_words = 2
[secondary]    # 9–12-char invented / geo+service
min_length = 9
max_length = 12         # also the ingestion charset+length ceiling (TDD §4.2) — not primary's 8
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

## Open before build
- Owner's review-round-2 points — **received and folded in** (2026-07-14).
- **Final approval** of argparse + this layout + `init-db` (creates `candidates` + `ingest_log`) as the working deliverable.
