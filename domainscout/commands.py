"""Subcommand handlers. Only init-db does real work in Phase 1; the rest are
friendly stubs that name the phase that will implement them."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from domainscout import comps, db, filters, ingest, pronounce, rdap
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


COMPS_DATA_DIR = "data"


def cmd_comps_refresh(args: argparse.Namespace) -> int:
    criteria = load_criteria(args.criteria)
    data_dir = Path(getattr(args, "data_dir", None) or COMPS_DATA_DIR)
    if args.dry_run:
        print("comps-refresh: [dry-run] would refresh "
              + ", ".join(s.name for s in comps.FILE_SPECS)
              + f" into {data_dir} (nothing written)")
        return 0
    client = ingest.make_client()
    try:
        result = comps.refresh_cache(client, criteria, data_dir, force=args.force)
    finally:
        client.close()
    print(comps.summary_line(result))
    _warn_if_stale(criteria, data_dir)
    return 0


def _warn_if_stale(criteria, data_dir) -> None:
    """An exact-header match means a NameBio column ADD bricks refresh until a code change.
    That is the right conservative failure, but from cron it fails silently-forever (exit 0),
    so surface age where we actually look."""
    meta = comps.load_meta(data_dir)
    limit = criteria.comps_refresh_days * criteria.comps_stale_warn_factor
    now = datetime.now()
    for spec in comps.FILE_SPECS:
        age = comps.cache_age_days(meta, spec.name, now)
        if age is not None and age > limit:
            print(f"  ⚠️  STALE - {spec.name} is {age:.0f}d old (> {criteria.comps_stale_warn_factor}x "
                  f"refresh_days={criteria.comps_refresh_days}); refresh has been failing")


def cmd_comps(args: argparse.Namespace) -> int:
    """LOCAL ONLY: reads the cache, never touches the network."""
    criteria = load_criteria(args.criteria)
    data_dir = Path(getattr(args, "data_dir", None) or COMPS_DATA_DIR)
    retail, used_prev_r = comps.resolve_cache_path(
        data_dir / "namebio_retailstats.csv", data_dir / "namebio_retailstats.csv.prev")
    tldp, _ = comps.resolve_cache_path(
        data_dir / "namebio_tldstats.csv", data_dir / "namebio_tldstats.csv.prev")
    meta = comps.load_meta(data_dir)
    now = datetime.now()

    ages = []
    for spec in comps.FILE_SPECS:
        age = comps.cache_age_days(meta, spec.name, now)
        rows = (meta.get(spec.name) or {}).get("rows")
        ages.append(f"{spec.name} " + ("age unknown" if age is None else f"{age:.0f}d")
                    + (f" ({rows:,} rows)" if rows else ""))
    print("cache: " + " | ".join(ages) + ("  [using .prev!]" if used_prev_r else ""))
    _warn_if_stale(criteria, data_dir)

    ctx = comps.lookup(args.domain, comps.load_index(retail), comps.load_tld_stats(tldp),
                       criteria, retrieved=(meta.get("retailstats") or {}).get("retrieved"))
    print(f"{ctx.domain}  segmentation={ctx.segmentation}")
    if not ctx.keywords and ctx.exact is None:
        print("  no comparable sales for this keyword pattern "
              "(absence of evidence - invented names are underrepresented, NOT worthless)")
    for k in ctx.keywords:
        print(f"  {k.keyword:12s} {k.placement:6s} n={k.sale_count:<7,} "
              f"avg=${k.price_avg:,.0f}  max=${k.price_max:,.0f}  sd=${k.price_stddev:,.0f}")
    base = (ctx.tld_baseline.get("all_retail") or {})
    if base:
        print(f"  .com retail baseline: n={base.get('sale_count', 0):,} "
              f"avg=${base.get('price_avg', 0):,.0f}")
    print(comps.context_to_json(ctx))
    return 0
