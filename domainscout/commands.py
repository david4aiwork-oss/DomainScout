"""Subcommand handlers. Only init-db does real work in Phase 1; the rest are
friendly stubs that name the phase that will implement them."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path

import httpx

from domainscout import db, ingest
from domainscout.config import load_criteria

# Subcommand -> the phase number that will implement it.
STUB_PHASES: dict[str, int] = {
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


def cmd_ingest(args: argparse.Namespace) -> int:
    criteria = load_criteria(args.criteria)
    run_date = date.fromisoformat(args.date) if args.date else date.today() - timedelta(days=1)
    conn = db.connect(args.db)
    try:
        if args.file:
            results = [
                ingest.ingest_local_file(
                    conn, path=Path(args.file), criteria=criteria, run_date=run_date,
                    feed_category=args.feed_category, dry_run=args.dry_run,
                )
            ]
        else:
            source_names = args.source or list(criteria.sources)
            client = httpx.Client(timeout=30.0, follow_redirects=True)
            try:
                results = ingest.run_ingest(
                    conn, criteria=criteria, run_date=run_date,
                    source_names=source_names, feeds_dir=ingest.DEFAULT_FEEDS_DIR,
                    client=client, dry_run=args.dry_run,
                )
            finally:
                client.close()
        for counts in results:
            print(ingest.summary_line(counts))
    finally:
        conn.close()
    return 0
