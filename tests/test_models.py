from datetime import datetime

from domainscout import models
from domainscout.models import (
    ALL_STATUSES,
    CLOSED_STATUSES,
    DEFAULT_STATUS,
    OPEN_STATUSES,
    Candidate,
    IngestCounts,
)


def test_status_sets_partition_cleanly():
    # open and closed are disjoint, and 'dropped' is OPEN (the live opportunity)
    assert OPEN_STATUSES.isdisjoint(CLOSED_STATUSES)
    assert "dropped" in OPEN_STATUSES
    assert CLOSED_STATUSES == {"renewed", "reregistered", "dismissed"}
    assert ALL_STATUSES == OPEN_STATUSES | CLOSED_STATUSES
    assert DEFAULT_STATUS == "unknown"
    assert DEFAULT_STATUS in OPEN_STATUSES


def test_candidate_defaults_to_unknown_open_status():
    c = Candidate(domain="foo.com", source="whoisfreaks")
    assert c.lifecycle_status == "unknown"
    assert c.feed_category is None
    assert c.id is None
    assert c.first_seen is None


def test_ingest_counts_defaults_zero():
    ic = IngestCounts(source="whoisfreaks", feed_file="2026-07-14-free-dropped-domains.csv")
    assert ic.seen == 0
    assert ic.landed == 0
    assert ic.rejected_charset == 0
    assert ic.run_date is None


def test_keyword_comps_and_context_shape():
    from domainscout.models import CompsContext, KeywordComps
    kw = KeywordComps(keyword="cloud", placement="start", sale_count=2762,
                      price_avg=3133.18, price_max=500000.0, price_stddev=10466.05)
    ctx = CompsContext(domain="cloudvault.com", segmentation="cloud+vault",
                       keywords=(kw,), exact=None, tld_baseline={"extension": ".com"},
                       retrieved="2026-07-16")
    assert ctx.modeled is None            # reserved ValuationProvider slot
    assert ctx.attribution.startswith("Comparable sales data from NameBio")


def test_refresh_result_reports_mixed_outcome():
    from domainscout.models import FileRefreshResult, RefreshResult
    res = RefreshResult(files=(
        FileRefreshResult(name="retailstats", action="swapped", reason="", rows=97568, bytes=6678360),
        FileRefreshResult(name="tldstats", action="refused", reason="429", rows=None, bytes=None),
    ))
    assert res.any_swapped is True
    assert res.any_refused is True


def test_verdict_constants_are_the_exact_spec_strings():
    assert models.VERDICT_REJECT == "reject"
    assert models.VERDICT_UNKNOWN_ERROR == "unknown_error"
    assert models.VERDICT_UNKNOWN_NO_HISTORY == "unknown_no_history"
    assert models.VERDICT_PASS == "pass"


def test_toxicity_verdict_holds_partial_legs():
    """A verdict must be able to carry a successful leg alongside a failed one."""
    v = models.ToxicityVerdict(
        domain="x.com", verdict=models.VERDICT_UNKNOWN_ERROR, reason="cdx: timeout",
        gsb=models.GsbResult(currently_listed=False, threat_types=(), checked_at="2026-07-18"),
        history=None, screened_at="2026-07-18", collapse="timestamp:6")
    assert v.gsb is not None and v.gsb.currently_listed is False
