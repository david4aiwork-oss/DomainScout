"""python -m domainscout — argparse dispatch across the pipeline phases."""

from __future__ import annotations

import argparse
import sys

from domainscout import __version__, commands
from domainscout.db import DEFAULT_DB_PATH

_STUB_HELP = {
    "score-submit": "[Phase 5] submit the AI scoring batch",
    "score-collect": "[Phase 5] collect AI scoring batch results",
    "digest": "[Phase 7] generate the ranked daily digest",
    "prune": "[Phase 8] prune retained feeds/digests past the retention window",
    "web": "[Phase 8] run the FastAPI review UI",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="domainscout", description="Expired-domain discovery pipeline (.com).")
    parser.add_argument("--version", action="version", version=f"domainscout {__version__}")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help=f"SQLite path (default: {DEFAULT_DB_PATH})")

    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    p_init = sub.add_parser("init-db", help="create the database schema (idempotent)")
    p_init.set_defaults(func=commands.cmd_init_db)

    p_ingest = sub.add_parser(
        "ingest",
        help="[Phase 2] pull daily feeds, apply the .com+charset+length gate, upsert candidates",
    )
    p_ingest.add_argument("--source", action="append",
                          help="source name (repeatable; default: criteria.sources)")
    p_ingest.add_argument("--date", help="feed date YYYY-MM-DD (default: yesterday)")
    p_ingest.add_argument("--file", help="ingest a LOCAL feed file instead of downloading")
    p_ingest.add_argument("--feed-category", choices=["expired", "dropped"],
                          dest="feed_category",
                          help="feed_category for --file when the name is ambiguous")
    p_ingest.add_argument("--criteria", default="criteria.toml",
                          help="path to criteria.toml (default: criteria.toml)")
    p_ingest.add_argument("--dry-run", action="store_true",
                          help="gate + print counts, write nothing")
    p_ingest.set_defaults(func=commands.cmd_ingest)

    p_filter = sub.add_parser(
        "filter", help="[Phase 3] classify + dictionary/pronounceability gates on candidates")
    p_filter.add_argument("--criteria", default="criteria.toml",
                          help="path to criteria.toml (default: criteria.toml)")
    p_filter.add_argument("--recompute", action="store_true",
                          help="re-filter all open rows (after tuning thresholds)")
    p_filter.add_argument("--limit", type=int, help="max candidates to process")
    p_filter.add_argument("--dry-run", action="store_true",
                          help="compute + print summary, write nothing")
    p_filter.set_defaults(func=commands.cmd_filter)

    p_ngrams = sub.add_parser(
        "build-ngrams", help="[Phase 3] (re)build the pronounceability n-gram tables")
    p_ngrams.add_argument("--top-n", type=int, default=50000, dest="top_n",
                          help="number of top English words to train on (default: 50000)")
    p_ngrams.add_argument("--out", help="output path (default: domainscout/pronounce_tables.json)")
    p_ngrams.set_defaults(func=commands.cmd_build_ngrams)

    p_verify = sub.add_parser(
        "verify", help="[Phase 4] RDAP verification + status-driven drop dates")
    p_verify.add_argument("--criteria", default="criteria.toml",
                          help="path to criteria.toml (default: criteria.toml)")
    p_verify.add_argument("--limit", type=int, default=1000,
                          help="max candidates to verify this run (default: 1000)")
    p_verify.add_argument("--concurrency", type=int,
                          help="override [rdap].concurrency for this run")
    p_verify.add_argument("--recheck-all", action="store_true", dest="recheck_all",
                          help="ignore the per-status cadence; re-verify every open+filter_pass row")
    p_verify.add_argument("--domain", help="verify a single NAME (live debug; writes only to an open row)")
    p_verify.add_argument("--dry-run", action="store_true",
                          help="compute + print the tally, write nothing")
    p_verify.set_defaults(func=commands.cmd_verify)

    # outcome carries the dismissal-intent note now; the --dismiss flag lands in Phase 6.
    p_outcome = sub.add_parser(
        "outcome",
        help="[Phase 6] record real-world outcomes",
        description=(
            "Phase 6 (stub). Will also be the manual dismissal path: "
            "`outcome <domain> --dismiss` sets lifecycle_status='dismissed' to close "
            "an open cycle from the CLI before the Phase 8 UI exists."
        ),
    )
    p_outcome.set_defaults(func=commands.cmd_stub)

    for name, help_text in _STUB_HELP.items():
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=commands.cmd_stub)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
