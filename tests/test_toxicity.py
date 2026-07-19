import json
from pathlib import Path

import pytest

from domainscout import models, toxicity

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parse_cdx_reads_columns_by_name_not_index():
    """CDX column ORDER depends on the fl= parameter. Reading by index would break
    silently the moment anyone reorders fl."""
    payload = [["digest", "timestamp", "mimetype", "statuscode"],
               ["ABC", "20200115120000", "text/html", "200"]]
    caps = toxicity.parse_cdx(payload)
    assert caps == [models.Capture(timestamp="20200115120000", statuscode="200",
                                   mimetype="text/html", digest="ABC")]


def test_parse_cdx_empty_and_header_only_both_mean_no_captures():
    """MEASURED in Task 1: a never-archived domain returns the literal bytes `[]`, a bare
    empty array - NOT a header-only response. Both are handled anyway, because a
    never-archived domain must never be mistaken for a parse failure."""
    assert toxicity.parse_cdx([]) == []
    assert toxicity.parse_cdx([["timestamp", "statuscode", "mimetype", "digest"]]) == []


def test_parse_cdx_never_archived_fixture_yields_nothing():
    assert toxicity.parse_cdx(_fixture("cdx_never_archived.json")) == []


def test_bucket_monthly_keeps_one_capture_per_calendar_month():
    caps = [models.Capture(f"2020{m:02d}{d:02d}120000", "200", "text/html", f"D{m}{d}")
            for m in (1, 1, 2) for d in (1, 15)]
    kept = toxicity.bucket_monthly(caps)
    assert [c.timestamp[:6] for c in kept] == ["202001", "202002"]


def test_bucket_monthly_sorts_by_time_first():
    """CdxClient merges two independently-collapsed host queries (apex + www.), so the
    merged list is NOT time-ordered and can hold two rows for the same month. Bucketing
    without sorting would sample by merge order rather than by time."""
    caps = [models.Capture("20220301120000", "200", "text/html", "B"),
            models.Capture("20200115120000", "200", "text/html", "A")]
    assert [c.timestamp for c in toxicity.bucket_monthly(caps)] == \
           ["20200115120000", "20220301120000"]


def test_bucket_monthly_keeps_the_earliest_capture_in_a_month():
    """When two captures fall in the SAME month but arrive out of chronological order,
    the sort ensures the earliest one survives the dedup, not whichever came first in
    the (unordered) input - critical because merged queries can have same-month captures."""
    caps = [models.Capture("20200120120000", "200", "text/html", "LATE"),
            models.Capture("20200105120000", "200", "text/html", "EARLY")]
    kept = toxicity.bucket_monthly(caps)
    assert len(kept) == 1
    assert kept[0].digest == "EARLY"


def test_bucket_monthly_year_boundary_keeps_decembers_distinct():
    """Year boundaries matter: Dec 2020, Jan 2021, and Dec 2021 are three distinct
    calendar months. Bucketing on timestamp[:6] (YYYYMM) correctly keeps all three;
    a future edit to month-only slice like timestamp[4:6] would conflate Decembers."""
    caps = [models.Capture("20201215120000", "200", "text/html", "DEC20"),
            models.Capture("20210105120000", "200", "text/html", "JAN21"),
            models.Capture("20211205120000", "200", "text/html", "DEC21")]
    kept = toxicity.bucket_monthly(caps)
    assert len(kept) == 3
    assert [c.digest for c in kept] == ["DEC20", "JAN21", "DEC21"]


def test_parse_cdx_skips_short_rows_and_parses_valid_ones():
    """Malformed or truncated CDX rows (too few columns) must be skipped cleanly
    without raising an exception or adding incomplete Capture objects to the output."""
    payload = [["timestamp", "statuscode", "mimetype", "digest"],
               ["20200115120000", "200", "text/html"],  # too short - missing digest
               ["20200120120000", "200", "text/html", "ABC"]]  # valid
    caps = toxicity.parse_cdx(payload)
    assert len(caps) == 1
    assert caps[0].timestamp == "20200120120000"
    assert caps[0].digest == "ABC"
