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


def _caps(pairs):
    """pairs: [(timestamp, digest)] -> captures, all 200/text/html."""
    return [models.Capture(ts, "200", "text/html", dg) for ts, dg in pairs]


def test_compute_shape_returns_none_for_no_captures():
    """Absence is NOT a zero-valued shape. Zero captures must reach decide() as
    unknown_no_history, and a ShapeBlock full of 0.0s would read as 'measured and bad'."""
    assert toxicity.compute_shape([], tail_window_months=24, tail_min_captures=3) is None


def test_compute_shape_lifetime_metrics():
    caps = _caps([("20100115120000", "A"), ("20110115120000", "A"),
                  ("20120115120000", "B"), ("20130115120000", "B")])
    shape = toxicity.compute_shape(caps, tail_window_months=24, tail_min_captures=3)
    lt = shape.lifetime
    assert lt.first_capture == "20100115120000" and lt.last_capture == "20130115120000"
    assert lt.capture_count == 4 and lt.distinct_years == 4
    assert lt.digest_churn == 0.5            # 2 distinct digests / 4 captures
    assert 2.9 < lt.span_years < 3.1
    assert lt.status_mix["2xx"] == 4


def test_compute_shape_tail_is_anchored_on_last_capture_not_today():
    """A domain that died in 2015 has a 2013-2015 tail. 'Late-life' means late in the
    DOMAIN's life - anchoring on today would make every dead domain's tail empty."""
    caps = _caps([(f"{y}0115120000", f"D{y}") for y in range(2005, 2016)])
    shape = toxicity.compute_shape(caps, tail_window_months=24, tail_min_captures=3)
    assert shape.tail is not None
    assert shape.tail.first_capture >= "20130115"
    assert shape.tail.last_capture == "20150115120000"


def test_compute_shape_detects_tail_flip_that_lifetime_aggregates_hide():
    """THE point of the tail window. 10 stable years then 18 months of churn: the
    lifetime digest_churn stays low and respectable, so only the divergence shows it."""
    stable = [(f"{y}{m:02d}15120000", "SAME") for y in range(2010, 2020) for m in (1, 7)]
    churny = [(f"2020{m:02d}15120000", f"FLIP{m}") for m in range(1, 13)]
    shape = toxicity.compute_shape(_caps(stable + churny),
                                   tail_window_months=24, tail_min_captures=3)
    assert shape.lifetime.digest_churn < 0.5          # lifetime looks fine
    assert shape.tail.digest_churn > 0.9              # tail is wild
    assert shape.divergence.churn_ratio > 2.0         # the signal


def test_compute_shape_divergence_is_none_below_tail_min_captures():
    """Two data points cannot support a ratio. None beats a fabricated number."""
    caps = _caps([("20100115120000", "A"), ("20110115120000", "B"),
                  ("20200115120000", "C")])
    shape = toxicity.compute_shape(caps, tail_window_months=24, tail_min_captures=3)
    assert shape.tail is None and shape.divergence is None


def test_compute_shape_divergence_is_none_when_tail_covers_whole_life():
    """If the domain is younger than the tail window, tail == lifetime and every
    ratio is 1.0 by construction - a meaningless 'no divergence' that reads as
    'checked and fine'."""
    caps = _caps([(f"2025{m:02d}15120000", f"D{m}") for m in range(1, 7)])
    shape = toxicity.compute_shape(caps, tail_window_months=24, tail_min_captures=3)
    assert shape.divergence is None


def test_compute_shape_divergence_values_are_exact():
    """Pin the arithmetic, not just the direction. 6 lifetime captures with 3 distinct
    digests (churn 0.5); the 3 tail captures are all distinct (churn 1.0) -> ratio 2.0."""
    caps = _caps([("20200115120000", "A"), ("20200715120000", "A"),
                  ("20210115120000", "B"), ("20230115120000", "C"),
                  ("20230715120000", "D"), ("20240115120000", "E")])
    shape = toxicity.compute_shape(caps, tail_window_months=24, tail_min_captures=3)
    assert shape.lifetime.digest_churn == 0.8333          # 5 distinct / 6
    assert shape.tail.capture_count == 3                  # 2023-01, 2023-07, 2024-01
    assert shape.tail.digest_churn == 1.0                 # C, D, E all distinct
    assert shape.divergence.churn_ratio == 1.2            # 1.0 / 0.8333
    assert shape.divergence.status_shift == 0.0           # all 2xx in both windows
