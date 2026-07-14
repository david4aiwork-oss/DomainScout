"""Ingestion: pure gate + network download + orchestration.

Only survivors of the hard-invariant gate land in the permanent DB. Network
lives solely in download(); the httpx.Client is injected so tests never hit it."""

from __future__ import annotations

import re

from domainscout.config import Criteria

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
