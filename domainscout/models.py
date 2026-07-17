"""Dataclasses and lifecycle-status constants shared across phases."""

from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass
class RdapObservation:
    """Normalized RDAP result. available=True iff a 404/NotFoundError. status is lowercased;
    events maps eventAction(lower) -> date; status_json is the rdap_status column value."""

    available: bool
    status: tuple[str, ...]
    events: dict
    expiry_date: date | None
    status_json: str


@dataclass
class LifecycleUpdate:
    """Result of applying an RdapObservation to a candidate's current cycle."""

    lifecycle_status: str
    drop_date_est: date | None
    drop_date_actual: date | None    # today on a confirmed drop; writer COALESCE-preserves the first
    expiry_date: date | None


@dataclass
class VerifyCounts:
    """One verify run's tally (printed summary)."""

    processed: int = 0
    dropped: int = 0
    redemption: int = 0
    pending_delete: int = 0
    grace: int = 0
    renewed: int = 0
    reregistered: int = 0
    errors: int = 0
    left_for_next_run: int = 0
    unmatched: dict = field(default_factory=dict)


# NameBio's free-data permission is CONDITIONED on attribution, so this is a licence
# obligation, not a courtesy. CompsContext carries it so the Phase 7 digest cannot forget.
NAMEBIO_ATTRIBUTION = "Comparable sales data from NameBio (https://namebio.com)"


@dataclass
class KeywordComps:
    """One NameBio keyword's stats at ONE placement. price_* are None-free: a keyword
    absent from the index yields no KeywordComps at all (see comps.lookup)."""

    keyword: str
    placement: str          # 'exact' | 'start' | 'end' | 'middle'
    sale_count: int
    price_avg: float
    price_max: float
    price_stddev: float


@dataclass
class CompsContext:
    """The Tier-2 comps payload for one domain; serialized into candidates.value_range by 5c."""

    domain: str
    segmentation: str                       # from filters.dict_score, e.g. 'cloud+vault'
    keywords: tuple[KeywordComps, ...]
    exact: KeywordComps | None              # whole-label exact lookup (often absent)
    tld_baseline: dict
    retrieved: str | None                   # namebio_meta.json retailstats date; None if no sidecar
    modeled: dict | None = None             # RESERVED ValuationProvider slot (HumbleWorth).
                                            # MUST serialize as "modeled": null - keeps a later
                                            # HumbleWorth a data change, never a schema migration.
    attribution: str = NAMEBIO_ATTRIBUTION


@dataclass
class FileRefreshResult:
    """One cache file's independent outcome. action: 'swapped'|'skipped_fresh'|'refused'."""

    name: str               # 'retailstats' | 'tldstats'
    action: str
    reason: str = ""
    rows: int | None = None
    bytes: int | None = None


@dataclass
class RefreshResult:
    """Per-file results. Mixed outcomes are normal, not an error (design doc: per-file independence)."""

    files: tuple[FileRefreshResult, ...] = ()

    @property
    def any_swapped(self) -> bool:
        return any(f.action == "swapped" for f in self.files)

    @property
    def any_refused(self) -> bool:
        return any(f.action == "refused" for f in self.files)
