import json
from datetime import datetime, timedelta
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


def test_max_gap_years_and_distinct_years_multi_year_gap():
    """max_gap_years silently fails if not asserted - it measures abandonment periods.
    A 6-year gap between 2012 and 2018 must be correctly computed and distinct_years
    must count the actual calendar years spanned."""
    # 2010, 2011, 2012 (one per year), then gap to 2018, then 2019
    caps = _caps([("20100115120000", "A"), ("20110115120000", "B"),
                  ("20120115120000", "C"), ("20180115120000", "D"),
                  ("20190115120000", "E")])
    shape = toxicity.compute_shape(caps, tail_window_months=24, tail_min_captures=3)
    lt = shape.lifetime
    # max_gap: 2012-01-15 to 2018-01-15 is 2191 days = 2191/365.25 = 6.001 after rounding to 3 decimals
    assert lt.max_gap_years == 6.001
    assert lt.distinct_years == 5


def test_status_mix_and_mime_mix_with_diverse_codes_and_types():
    """status_mix and mime_mix exercise _status_bucket's bucketing and diversity.
    If status codes are not bucketed correctly or mimetypes are miscounted, the
    status_mix and mime_mix dicts silently carry wrong counts to downstream scoring."""
    caps = [
        models.Capture("20200101120000", "200", "text/html", "A"),
        models.Capture("20200201120000", "301", "application/octet-stream", "B"),
        models.Capture("20200301120000", "404", "warc/revisit", "C"),
        models.Capture("20200401120000", "-", "text/plain", "D"),
    ]
    shape = toxicity.compute_shape(caps, tail_window_months=24, tail_min_captures=3)
    lt = shape.lifetime
    # status_bucket: "200" -> "2xx", "301" -> "3xx", "404" -> "4xx", "-" -> "other"
    assert lt.status_mix == {"2xx": 1, "3xx": 1, "4xx": 1, "other": 1}
    # mimetypes are counted as-is
    assert lt.mime_mix == {
        "text/html": 1,
        "application/octet-stream": 1,
        "warc/revisit": 1,
        "text/plain": 1,
    }


def test_mime_shift_and_captures_per_year_ratio_in_divergence():
    """mime_shift and captures_per_year_ratio are computed but never validated.
    A text/html-to-other flip in the tail must produce negative mime_shift.
    captures_per_year_ratio must reflect the tail's sampling density vs lifetime."""
    # Long HTML run (2010-2020, every other year), then tail of mostly NOT-HTML
    html_stable = [
        (f"{y}0115120000", "HTML") for y in range(2010, 2021, 2)
    ]  # 2010, 2012, 2014, 2016, 2018, 2020 = 6 HTML captures
    # Tail window (24 months from last capture) covers ~2019-07 onwards
    # Add 2021-01 and 2021-07 with non-HTML mimetypes
    tail_flip = [
        ("20210115120000", "FLIP1"),
        ("20210715120000", "FLIP2"),
    ]
    caps_data = html_stable + tail_flip
    caps = [
        models.Capture(ts, "200", "text/html" if "HTML" in dg else "application/json", dg)
        for ts, dg in caps_data
    ]
    shape = toxicity.compute_shape(caps, tail_window_months=24, tail_min_captures=3)
    assert shape.divergence is not None
    lt, tail, div = shape.lifetime, shape.tail, shape.divergence
    # Lifetime: 8 captures (6 HTML + 2 JSON from tail_flip) -> html_prop = 6/8 = 0.75
    assert lt.capture_count == 8
    # Tail (24 months from 2021-07, i.e., from ~2019-07): 2020-01, 2021-01, 2021-07 = 3 captures
    # 1 text/html (2020-01), 2 application/json -> html_prop = 1/3 ≈ 0.3333
    assert tail.capture_count == 3
    # mime_shift = 0.3333 - 0.75 = -0.4167, rounded to 4 decimals
    assert div.mime_shift == -0.4167
    # captures_per_year: lifetime ≈ 0.696, tail ≈ 2.003
    # ratio ≈ 2.003 / 0.696 ≈ 2.8779, rounded to 4 decimals
    assert div.captures_per_year_ratio == 2.8779


_LISTED = models.GsbResult(True, ("MALWARE",), "2026-07-18")
_NOT_LISTED = models.GsbResult(False, (), "2026-07-18")
_SHAPE = models.HistoryShape(
    lifetime=models.ShapeBlock("20100101000000", "20200101000000", 10.0, 20, 10,
                               1.0, 0.5, 2.0, {"2xx": 20}, {"text/html": 20}),
    tail=None, divergence=None)


def test_decide_gsb_listing_rejects_and_outranks_errors():
    """A blocklist hit is a fact, not a judgement. It wins even when the other leg
    failed - we already know enough to reject."""
    verdict, reason = toxicity.decide(_LISTED, None, ["cdx: timeout"])
    assert verdict == models.VERDICT_REJECT
    assert "MALWARE" in reason


def test_decide_error_never_becomes_pass():
    """Invariant 2. A timeout must never be indistinguishable from 'we checked, it's fine'."""
    verdict, _ = toxicity.decide(_NOT_LISTED, _SHAPE, ["cdx: timeout"])
    assert verdict == models.VERDICT_UNKNOWN_ERROR


def test_decide_no_captures_is_unknown_no_history_not_pass_and_not_reject():
    """Invariant 1. Invented secondary-track brandables routinely have zero captures;
    folding that into either pass or reject mis-scores exactly the names we hunt for."""
    verdict, reason = toxicity.decide(_NOT_LISTED, None, [])
    assert verdict == models.VERDICT_UNKNOWN_NO_HISTORY
    assert "absence" in reason.lower()


def test_decide_clean_and_archived_is_pass():
    verdict, _ = toxicity.decide(_NOT_LISTED, _SHAPE, [])
    assert verdict == models.VERDICT_PASS


def test_decide_rung_2_beats_rung_3_errors_outrank_no_history():
    """Rung 2 must precede rung 3: transient errors (never cached) must not be recorded
    as stable absence (cached for 30 days). Swapping these rungs is a realistic refactoring
    accident with severe consequences."""
    verdict, reason = toxicity.decide(_NOT_LISTED, None, ["cdx: timeout"])
    assert verdict == models.VERDICT_UNKNOWN_ERROR
    assert "cdx: timeout" in reason


def test_decide_unknown_error_reason_carries_all_diagnostics():
    """Multiple errors must all appear in the reason, so downstream can pinpoint which
    leg failed. Returning only the first error would silently swallow diagnostic info."""
    verdict, reason = toxicity.decide(_NOT_LISTED, _SHAPE, ["cdx: timeout", "safe-browsing: HTTP 500"])
    assert verdict == models.VERDICT_UNKNOWN_ERROR
    assert "cdx: timeout" in reason
    assert "safe-browsing: HTTP 500" in reason
    assert reason == "cdx: timeout; safe-browsing: HTTP 500"


def test_decide_gsb_none_guards_against_null_dereference():
    """The signature permits gsb=None (Safe Browsing leg failed entirely). The 'gsb is not
    None and ...' guard must be present to prevent AttributeError on a real GSB outage."""
    verdict, reason = toxicity.decide(None, _SHAPE, ["safe-browsing: boom"])
    assert verdict == models.VERDICT_UNKNOWN_ERROR
    assert "safe-browsing: boom" in reason


_DAYS = {"reject": 30, "pass": 14, "unknown_no_history": 30}


def _verdict(domain, verdict, collapse="timestamp:6", screened_at="2026-07-18T00:00:00"):
    return models.ToxicityVerdict(domain=domain, verdict=verdict, reason="r",
                                  gsb=_NOT_LISTED, history=None,
                                  screened_at=screened_at, collapse=collapse)


def test_cache_roundtrip_within_ttl(tmp_path):
    now = datetime(2026, 7, 18)
    cache = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                  collapse="timestamp:6", now=now)
    cache.put(_verdict("a.com", models.VERDICT_PASS, screened_at=now.isoformat()))
    cache.save()
    reopened = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                     collapse="timestamp:6", now=now + timedelta(days=13))
    assert reopened.get("a.com").verdict == models.VERDICT_PASS


def test_cache_expires_past_ttl(tmp_path):
    now = datetime(2026, 7, 18)
    cache = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                  collapse="timestamp:6", now=now)
    cache.put(_verdict("a.com", models.VERDICT_PASS, screened_at=now.isoformat()))
    cache.save()
    stale = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                  collapse="timestamp:6", now=now + timedelta(days=15))
    assert stale.get("a.com") is None


def test_cache_never_persists_unknown_error(tmp_path):
    """NOT a TTL of 0 - never written at all. A transient failure then CANNOT be
    misconfigured into stickiness, which any numeric TTL eventually can."""
    now = datetime(2026, 7, 18)
    cache = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                  collapse="timestamp:6", now=now)
    cache.put(_verdict("a.com", models.VERDICT_UNKNOWN_ERROR, screened_at=now.isoformat()))
    cache.save()
    assert cache.get("a.com") is None
    assert "a.com" not in json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))


def test_cache_misses_when_collapse_changed(tmp_path):
    """Self-enforcing calibration: every metric is relative to the sampling, so an
    entry computed under a different collapse is not comparable data."""
    now = datetime(2026, 7, 18)
    cache = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                  collapse="timestamp:6", now=now)
    cache.put(_verdict("a.com", models.VERDICT_PASS, collapse="timestamp:6",
                       screened_at=now.isoformat()))
    cache.save()
    changed = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                    collapse="timestamp:4", now=now)
    assert changed.get("a.com") is None


def test_cache_tolerates_a_corrupt_file(tmp_path):
    """A half-written cache must degrade to a cold cache, never crash the run."""
    (tmp_path / "c.json").write_text("{not json", encoding="utf-8")
    cache = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                  collapse="timestamp:6", now=datetime(2026, 7, 18))
    assert cache.get("a.com") is None


def test_cache_ttl_boundary_is_miss(tmp_path):
    """Entry exactly ttl days old is a MISS to prevent >= vs > refactor silent extension."""
    now = datetime(2026, 7, 18)
    cache = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                  collapse="timestamp:6", now=now)
    # Pass verdict has TTL of 14 days
    cache.put(_verdict("a.com", models.VERDICT_PASS, screened_at=now.isoformat()))
    cache.save()
    # Reopen at exactly 14 days later - should be a miss
    stale = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                  collapse="timestamp:6", now=now + timedelta(days=14))
    assert stale.get("a.com") is None


def test_cache_unknown_verdict_string_is_miss(tmp_path):
    """Future version with unknown verdict string must not crash, only miss cache."""
    cache_file = tmp_path / "c.json"
    # Hand-write a cache with a verdict not in cache_days
    cache_file.write_text(json.dumps({
        "a.com": {
            "verdict": "quarantined",
            "reason": "test",
            "screened_at": "2026-07-18T00:00:00",
            "collapse": "timestamp:6"
        }
    }), encoding="utf-8")
    cache = toxicity.VerdictCache(cache_file, cache_days=_DAYS,
                                  collapse="timestamp:6", now=datetime(2026, 7, 18))
    assert cache.get("a.com") is None


def test_cache_non_dict_entry_is_miss(tmp_path):
    """Hand-edited cache with bare string instead of dict entry must not crash."""
    cache_file = tmp_path / "c.json"
    # Hand-write corrupted cache with bare string entry
    cache_file.write_text(json.dumps({
        "a.com": "corrupted",
        "b.com": {"verdict": "pass", "reason": "", "screened_at": "2026-07-18T00:00:00", "collapse": "timestamp:6"}
    }), encoding="utf-8")
    cache = toxicity.VerdictCache(cache_file, cache_days=_DAYS,
                                  collapse="timestamp:6", now=datetime(2026, 7, 18))
    assert cache.get("a.com") is None
    # Good entry still works
    assert cache.get("b.com").verdict == models.VERDICT_PASS


def test_cache_save_osrc_does_not_raise(tmp_path, monkeypatch, capsys):
    """Windows AV file-lock during rename is NOT theoretical - 5a hit it.
    OSError must be caught and warned, not propagated."""
    now = datetime(2026, 7, 18)
    cache = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                  collapse="timestamp:6", now=now)
    cache.put(_verdict("a.com", models.VERDICT_PASS, screened_at=now.isoformat()))

    # Monkeypatch os.replace to raise OSError
    def mock_replace(src, dst):
        raise OSError("locked")
    monkeypatch.setattr("os.replace", mock_replace)

    # save() must NOT raise
    cache.save()

    # Verify warning was printed
    captured = capsys.readouterr()
    assert "toxicity: WARNING" in captured.out

    # Verify the output encodes to cp1252 (Windows console safety)
    captured.out.encode("cp1252")  # must not raise


def test_cache_reason_survives_roundtrip(tmp_path):
    """Reason field must survive save/reload cycle, not just verdict."""
    now = datetime(2026, 7, 18)
    cache = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                  collapse="timestamp:6", now=now)
    distinctive_reason = "site flipped to gambling then defunct"
    verdict = models.ToxicityVerdict(
        domain="a.com", verdict=models.VERDICT_PASS, reason=distinctive_reason,
        gsb=_NOT_LISTED, history=None,
        screened_at=now.isoformat(), collapse="timestamp:6")
    cache.put(verdict)
    cache.save()

    reopened = toxicity.VerdictCache(tmp_path / "c.json", cache_days=_DAYS,
                                     collapse="timestamp:6", now=now)
    retrieved = reopened.get("a.com")
    assert retrieved is not None
    assert retrieved.reason == distinctive_reason
