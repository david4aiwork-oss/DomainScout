from datetime import datetime

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
