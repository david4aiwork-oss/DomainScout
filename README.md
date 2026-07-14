# DomainScout

Personal expired-domain discovery pipeline for quality **.com** domains.
See [`CLAUDE.md`](CLAUDE.md), [`DECISIONS.md`](DECISIONS.md), and
[`docs/TECHNICAL-DESIGN.md`](docs/TECHNICAL-DESIGN.md) for the design.

## Requirements
- Python 3.11+ (no third-party runtime dependencies in Phase 1).

## Install (editable, with dev tools)
```bash
python -m pip install -e ".[dev]"
```

## Run
```bash
python -m domainscout init-db        # create data/domainscout.db (idempotent)
python -m domainscout --help         # list subcommands
```
Later-phase subcommands (`ingest`, `filter`, `verify`, `score-submit`, …) are
stubs until their phase is built.

## Test
```bash
python -m pytest
```

## Config & secrets
- Criteria live in [`criteria.toml`](criteria.toml).
- Copy [`.env.example`](.env.example) to `.env` and fill in API keys (Phase 5+).
