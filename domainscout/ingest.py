"""Ingestion: pure gate + network download + orchestration.

Only survivors of the hard-invariant gate land in the permanent DB. Network
lives solely in download(); the httpx.Client is injected so tests never hit it."""

from __future__ import annotations

import re
from pathlib import Path

import httpx

from domainscout.config import Criteria
from domainscout.sources.base import FeedFile

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
