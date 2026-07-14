"""Subcommand handlers. Only init-db does real work in Phase 1; the rest are
friendly stubs that name the phase that will implement them."""

from __future__ import annotations

import argparse

from domainscout import db

# Subcommand -> the phase number that will implement it.
STUB_PHASES: dict[str, int] = {
    "ingest": 2,
    "filter": 3,
    "verify": 4,
    "score-submit": 5,
    "score-collect": 5,
    "outcome": 6,
    "digest": 7,
    "prune": 8,
    "web": 8,
}


def cmd_init_db(args: argparse.Namespace) -> int:
    db.init_db(args.db)
    print(f"Initialized DomainScout database at {args.db}")
    return 0


def cmd_stub(args: argparse.Namespace) -> int:
    phase = STUB_PHASES[args.command]
    print(f"domainscout: '{args.command}' is not implemented yet (Phase {phase}).")
    return 0
