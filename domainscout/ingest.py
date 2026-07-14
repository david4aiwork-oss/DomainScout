"""Ingestion: pure gate + network download + orchestration.

Only survivors of the hard-invariant gate land in the permanent DB. Network
lives solely in download(); the httpx.Client is injected so tests never hit it."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import httpx

from domainscout import db
from domainscout.config import Criteria
from domainscout.models import Candidate, IngestCounts
from domainscout.sources.base import FeedFile, FeedSource

DEFAULT_FEEDS_DIR = "data/feeds"


def gate(domain: str, criteria: Criteria) -> tuple[bool, str | None]:
    """Apply the hard invariant. First failure wins; buckets are mutually
    exclusive. Length is measured on the label (name without the .com suffix)."""
    name = domain.strip().lower()
    if not name.endswith(".com"):
        return (False, "rejected_tld")
    label = name[:-4]  # strip ".com"
    if not re.match(criteria.charset, label):
        return (False, "rejected_charset")
    if len(label) > criteria.ingest_max_length:
        return (False, "rejected_length")
    return (True, None)


def download(feed_file: FeedFile, feeds_dir: str | Path, client: httpx.Client) -> Path:
    """GET the feed file to feeds_dir/local_name; skip if it already exists."""
    dest = Path(feeds_dir) / feed_file.local_name
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = client.get(feed_file.remote_url)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest


def ingest_file(
    conn,
    source: FeedSource,
    *,
    path: Path,
    feed_category: str,
    feed_file_name: str,
    run_date: date,
    criteria: Criteria,
    dry_run: bool = False,
) -> IngestCounts:
    """Gate every name in one local feed file; upsert survivors and log counts."""
    counts = IngestCounts(source=source.name, feed_file=feed_file_name, run_date=run_date)
    for raw in source.iter_domains(Path(path)):
        counts.seen += 1
        ok, reason = gate(raw, criteria)
        if ok:
            counts.landed += 1
            if not dry_run:
                db.upsert_candidate(
                    conn,
                    Candidate(
                        domain=raw.strip().lower(),
                        source=source.name,
                        feed_category=feed_category,
                    ),
                )
        elif reason == "rejected_tld":
            counts.rejected_tld += 1
        elif reason == "rejected_charset":
            counts.rejected_charset += 1
        else:  # "rejected_length"
            counts.rejected_length += 1
    if not dry_run:
        db.record_ingest(conn, counts)
    return counts
