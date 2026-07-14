"""python -m domainscout — argparse dispatch across the pipeline phases."""

from __future__ import annotations

import argparse
import sys

from domainscout import __version__, commands
from domainscout.db import DEFAULT_DB_PATH

_STUB_HELP = {
    "ingest": "[Phase 2] pull daily feeds, apply the .com+charset+length gate, upsert candidates",
    "filter": "[Phase 3] deterministic rules filter (dictionary + pronounceability)",
    "verify": "[Phase 4] RDAP verification and status-driven drop dates",
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
