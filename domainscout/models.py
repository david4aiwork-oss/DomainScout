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


# --- Phase 5b: toxicity gate -------------------------------------------------
# Four verdicts, NOT three. unknown_no_history (CDX succeeded, zero captures) is
# STABLE, informative absence - re-screening will not change it, and for an invented
# secondary-track brandable it is mildly reassuring. unknown_error is TRANSIENT
# ignorance and must be retried. Collapsing them would leave 5c unable to tell a
# young name from a failed lookup.
VERDICT_REJECT = "reject"
VERDICT_UNKNOWN_ERROR = "unknown_error"
VERDICT_UNKNOWN_NO_HISTORY = "unknown_no_history"
VERDICT_PASS = "pass"


@dataclass(frozen=True)
class Capture:
    """One Wayback CDX row. timestamp is 'YYYYMMDDhhmmss'."""

    timestamp: str
    statuscode: str
    mimetype: str
    digest: str


@dataclass(frozen=True)
class ShapeBlock:
    """History metrics over one time range. Computed over the MONTHLY-SAMPLED series,
    never the raw archive - raw counts are dominated by crawl-frequency artifacts."""

    first_capture: str | None
    last_capture: str | None
    span_years: float
    capture_count: int
    distinct_years: int
    max_gap_years: float
    digest_churn: float          # distinct digests / captures
    captures_per_year: float
    status_mix: dict             # '2xx'/'3xx'/'4xx'/'5xx'/'other' -> count
    mime_mix: dict               # mimetype -> count


@dataclass(frozen=True)
class Divergence:
    """Tail-vs-lifetime deltas. This is the content-flip signal: lifetime aggregates
    CANNOT show a late-life flip (12 clean years + 18 months of gambling averages out
    to respectable numbers), so the divergence is where the flip actually lives.
    5b reports these; it does NOT threshold them - interpretation is Tier-2's job."""

    churn_ratio: float | None            # tail.digest_churn / lifetime.digest_churn
    status_shift: float                  # tail 2xx proportion - lifetime 2xx proportion
    mime_shift: float                    # tail text/html proportion - lifetime's
    captures_per_year_ratio: float | None


@dataclass(frozen=True)
class HistoryShape:
    lifetime: ShapeBlock
    tail: ShapeBlock | None              # None if too few tail captures to be meaningful
    divergence: Divergence | None        # None whenever tail is None


@dataclass(frozen=True)
class GsbResult:
    """Safe Browsing is a blocklist of CURRENTLY listed URLs. A False here means
    'not presently listed' - a dropped domain that served malware in 2019 may well
    have aged off. The field is named currently_listed, never 'clean' or 'safe',
    so no downstream prompt can present it as verified-safe."""

    currently_listed: bool
    threat_types: tuple[str, ...]
    checked_at: str


@dataclass(frozen=True)
class ToxicityVerdict:
    """The verdict reflects the WORST leg; the data reflects EVERY leg that succeeded.
    A GSB success rides along even when CDX failed, and vice versa."""

    domain: str
    verdict: str
    reason: str
    gsb: GsbResult | None
    history: HistoryShape | None
    screened_at: str
    collapse: str                        # the sampling this verdict was computed under
