import json
from pathlib import Path

import pytest

from domainscout import comps
from domainscout.config import load_criteria

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RETAIL = FIXTURES / "namebio_retailstats_small.csv"
TLD = FIXTURES / "namebio_tldstats_small.csv"

REPO_ROOT = Path(__file__).resolve().parents[1]
CRIT = load_criteria(REPO_ROOT / "criteria.toml")


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


def _ctx(domain):
    return comps.lookup(domain, comps.load_index(RETAIL), comps.load_tld_stats(TLD),
                        CRIT, retrieved="2026-07-16")


def test_lookup_two_words_uses_start_then_end():
    ctx = _ctx("cloudvault.com")
    assert ctx.segmentation == "cloud+vault"
    got = [(k.keyword, k.placement, k.sale_count) for k in ctx.keywords]
    assert got == [("cloud", "start", 2762), ("vault", "end", 41)]


def test_lookup_single_word_uses_exact():
    ctx = _ctx("vault.com")
    assert ctx.segmentation == "vault"
    assert [(k.keyword, k.placement) for k in ctx.keywords] == [("vault", "exact")]


def test_lookup_geo_service_secondary_track():
    ctx = _ctx("austinplumber.com")
    got = [(k.keyword, k.placement, k.sale_count) for k in ctx.keywords]
    assert got == [("austin", "start", 88), ("plumber", "end", 64)]


def test_lookup_unknown_keyword_is_absence_not_error():
    """Invented brandables are systematically underrepresented in keyword-keyed retail
    stats. 'No comps' must mean 'no evidence for this pattern', never 'worthless' --
    a naive 5c prompt would penalize exactly the secondary-track names we exist to catch."""
    ctx = _ctx("zylo.com")
    assert ctx.keywords == ()
    assert ctx.exact is None
    assert ctx.tld_baseline["extension"] == ".com"   # baseline still present to reason from


def test_lookup_attaches_tld_baseline_and_retrieved():
    ctx = _ctx("cloudvault.com")
    assert ctx.tld_baseline["all_retail"]["price_avg"] == 8587.02
    assert ctx.retrieved == "2026-07-16"


def test_context_to_json_always_carries_modeled_null():
    """The reserved ValuationProvider slot. If this disappears, adding HumbleWorth later
    becomes a schema migration instead of a data change. Do not 'clean up' the null."""
    payload = json.loads(comps.context_to_json(_ctx("cloudvault.com")))
    assert "modeled" in payload and payload["modeled"] is None
    assert payload["source"] == "namebio-free"
    assert payload["attribution"].startswith("Comparable sales data from NameBio")
    assert payload["keywords"][0]["placement"] == "start"
