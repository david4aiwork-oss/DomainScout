from pathlib import Path

import pytest

from domainscout import comps

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RETAIL = FIXTURES / "namebio_retailstats_small.csv"
TLD = FIXTURES / "namebio_tldstats_small.csv"


def test_load_index_keys_by_keyword_and_keeps_raw_line():
    idx = comps.load_index(RETAIL)
    assert set(idx) == {"cloud", "vault", "austin", "plumber", "shop"}
    assert idx["cloud"].startswith("cloud,")   # raw line retained, parsed on demand


def test_parse_placement_reads_the_right_columns():
    idx = comps.load_index(RETAIL)
    kc = comps.parse_placement(idx["cloud"], "start")
    assert kc.keyword == "cloud" and kc.placement == "start"
    assert kc.sale_count == 2762
    assert kc.price_avg == 3133.18
    assert kc.price_max == 500000.0
    assert kc.price_stddev == 10466.05


def test_parse_placement_exact_differs_from_start():
    idx = comps.load_index(RETAIL)
    assert comps.parse_placement(idx["cloud"], "exact").sale_count == 120
    assert comps.parse_placement(idx["cloud"], "start").sale_count == 2762


def test_parse_placement_zero_sales_returns_none():
    """0 sales carries no information; treat as absent so lookup reports 'no comps'."""
    line = "zylo," + ",".join(["0"] * 20)
    assert comps.parse_placement(line, "exact") is None


def test_load_tld_stats_by_extension():
    tld = comps.load_tld_stats(TLD)
    assert tld[".com"]["all_retail"]["sale_count"] == 189826
    assert tld[".com"]["all_retail"]["price_avg"] == 8587.02


def test_load_index_missing_file_raises_cache_missing():
    with pytest.raises(comps.CompsCacheMissing):
        comps.load_index(FIXTURES / "does-not-exist.csv")
