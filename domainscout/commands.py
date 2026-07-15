"""Subcommand handlers. Only init-db does real work in Phase 1; the rest are
friendly stubs that name the phase that will implement them."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from domainscout import db, filters, ingest, pronounce, rdap
from domainscout.config import load_criteria

# Subcommand -> the phase number that will implement it.
STUB_PHASES: dict[str, int] = {
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
            client = ingest.make_client()
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


def cmd_filter(args: argparse.Namespace) -> int:
    criteria = load_criteria(args.criteria)
    conn = db.connect(args.db)
    try:
        counts = filters.filter_candidates(
            conn, criteria, recompute=args.recompute, limit=args.limit,
            dry_run=args.dry_run,
        )
    finally:
        conn.close()
    print(
        f"filter: processed={counts.processed} passed={counts.passed} "
        f"(primary={counts.primary} secondary={counts.secondary}) "
        f"rejected={counts.rejected}"
        + ("  [dry-run]" if args.dry_run else "")
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    criteria = load_criteria(args.criteria)
    if args.concurrency:
        criteria = replace(criteria, rdap_concurrency=args.concurrency)  # --concurrency override
    conn = db.connect(args.db)
    try:
        if args.domain:
            obs, upd, dns, wrote = asyncio.run(
                rdap.verify_single(criteria, args.domain, conn=conn, dry_run=args.dry_run))
            print(f"verify {args.domain}: available={obs.available} status={list(obs.status)}")
            print(f"  -> lifecycle={upd.lifecycle_status} drop_est={upd.drop_date_est} "
                  f"expiry={upd.expiry_date} dns={dns} written={wrote}")
            return 0
        counts = asyncio.run(rdap.run_verify(
            conn, criteria, limit=args.limit, recheck_all=args.recheck_all, dry_run=args.dry_run))
    finally:
        conn.close()
    print(
        f"verify: processed={counts.processed} dropped={counts.dropped} "
        f"redemption={counts.redemption} pending_delete={counts.pending_delete} "
        f"grace={counts.grace} renewed={counts.renewed} reregistered={counts.reregistered} "
        f"errors={counts.errors}"
        + ("  [dry-run]" if args.dry_run else "")
    )
    if counts.left_for_next_run:
        print(f"  {counts.left_for_next_run} due rows left for the next run (raise --limit to drain faster)")
    if counts.unmatched:
        pairs = ", ".join(f"{s!r}={n}" for s, n in sorted(counts.unmatched.items()))
        print(f"  unmatched RDAP statuses: {pairs}")
    return 0


def cmd_build_ngrams(args: argparse.Namespace) -> int:
    out = Path(args.out) if args.out else pronounce.DEFAULT_TABLES_PATH
    tables = pronounce.build_tables(top_n=args.top_n)
    pronounce.save_tables(tables, out)
    size_kb = out.stat().st_size / 1024
    print(f"build-ngrams: wrote {out} ({size_kb:.0f} KB, {tables['_meta']['words_kept']} words)")
    return 0
