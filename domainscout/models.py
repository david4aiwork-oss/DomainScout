"""Dataclasses and lifecycle-status constants shared across phases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

# Lifecycle status is the single source of truth for whether a cycle is OPEN.
# 'dropped' is OPEN: a dropped-and-registerable domain is the live opportunity.
# A cycle closes ONLY on re-registration, renewal, or owner dismissal.
DEFAULT_STATUS = "unknown"
OPEN_STATUSES = frozenset(
    {"unknown", "expiring", "grace", "redemption", "pending_delete", "dropped"}
)
CLOSED_STATUSES = frozenset({"renewed", "reregistered", "dismissed"})
ALL_STATUSES = OPEN_STATUSES | CLOSED_STATUSES


@dataclass
class Candidate:
    """A domain in one open registration cycle. Phase 1 uses the ingestion-time
    fields; later phases fill the rest via UPDATE, not via this dataclass."""

    domain: str
    source: str
    feed_category: str | None = None  # 'expired' | 'dropped' (from feed filename)
    lifecycle_status: str = DEFAULT_STATUS
    id: int | None = None
    first_seen: datetime | None = None


@dataclass
class IngestCounts:
    """One ingestion audit row (see ingest_log). Not every feed row lands —
    the charset+length gate rejects most (TDD §4.2)."""

    source: str
    feed_file: str
    seen: int = 0
    rejected_tld: int = 0
    rejected_charset: int = 0
    rejected_length: int = 0
    landed: int = 0
    run_date: date | None = None


@dataclass
class FilterCounts:
    """One filter run's tally (printed summary; per-domain detail lives in filter_reason)."""

    processed: int = 0
    passed: int = 0
    primary: int = 0      # passed & primary track
    secondary: int = 0    # passed & secondary track
    rejected: int = 0
