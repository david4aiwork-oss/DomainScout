import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from domainscout import comps
from domainscout.config import load_criteria
from domainscout.models import RefreshResult

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RETAIL = FIXTURES / "namebio_retailstats_small.csv"
TLD = FIXTURES / "namebio_tldstats_small.csv"

REPO_ROOT = Path(__file__).resolve().parents[1]
CRIT = load_criteria(REPO_ROOT / "criteria.toml")


def test_load_index_keys_by_keyword_and_keeps_raw_line():
    idx = comps.load_index(RETAIL)
    assert set(idx) == {"cloud", "vault", "austin", "plumber", "shop", "cloudvault"}
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
    # dedup half: the whole-label lookup finds `vault` too, but it must be nulled out
    # (already in `keywords`), never double-counted into `exact`.
    assert ctx.exact is None


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


def test_lookup_surfaces_whole_label_compound_as_exact():
    """The `exact` field exists to catch a compound that is ITSELF a NameBio keyword
    (e.g. 'cloudvault'), separate from its cloud@start + vault@end parts. Guards the
    branch Phase 5c relies on; without a compound in the fixture this path was untested."""
    ctx = _ctx("cloudvault.com")
    # still split into position-based parts...
    assert [(k.keyword, k.placement) for k in ctx.keywords] == [("cloud", "start"), ("vault", "end")]
    # ...AND the whole-label compound surfaces SEPARATELY, not double-counted into keywords
    assert ctx.exact is not None
    assert ctx.exact.keyword == "cloudvault" and ctx.exact.placement == "exact"
    assert ctx.exact.sale_count == 3


def _write(p: Path, header, rows):
    p.write_text(",".join(header) + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def test_validate_download_accepts_good_file(tmp_path):
    ok, reason = comps.validate_download(
        RETAIL, expected_header=comps.RETAILSTATS_HEADER, baseline_rows=5,
        min_rows=1, shrink_tolerance=0.8)
    assert ok is True and reason == ""


def test_validate_download_rejects_error_page(tmp_path):
    """HTTP 200 + an HTML error page is the exact failure atomic rename does NOT cover."""
    p = tmp_path / "bad.csv"
    p.write_text("<html><body>rate limited</body></html>", encoding="utf-8")
    ok, reason = comps.validate_download(
        p, expected_header=comps.RETAILSTATS_HEADER, baseline_rows=5,
        min_rows=1, shrink_tolerance=0.8)
    assert ok is False and "header" in reason.lower()


def test_validate_download_rejects_shrink_below_tolerance(tmp_path):
    p = tmp_path / "short.csv"
    _write(p, comps.RETAILSTATS_HEADER, ["cloud," + ",".join(["1"] * 20)])
    ok, reason = comps.validate_download(
        p, expected_header=comps.RETAILSTATS_HEADER, baseline_rows=100,
        min_rows=1, shrink_tolerance=0.8)
    assert ok is False and "shrink" in reason.lower()


def test_validate_download_first_run_uses_min_rows_floor(tmp_path):
    """No sidecar baseline: an error page must not be able to SEED the cache either."""
    p = tmp_path / "tiny.csv"
    _write(p, comps.RETAILSTATS_HEADER, ["cloud," + ",".join(["1"] * 20)])
    ok, reason = comps.validate_download(
        p, expected_header=comps.RETAILSTATS_HEADER, baseline_rows=None,
        min_rows=1000, shrink_tolerance=0.8)
    assert ok is False and "min_rows" in reason.lower()


def test_meta_roundtrip_atomic(tmp_path):
    meta = {"retailstats": {"retrieved": "2026-07-16T10:00:00", "rows": 97568,
                            "sha256": "abc", "bytes": 10}}
    comps.write_meta(tmp_path, meta)
    assert (tmp_path / comps.META_FILENAME).is_file()
    assert comps.load_meta(tmp_path)["retailstats"]["rows"] == 97568


def test_load_meta_missing_or_corrupt_returns_empty(tmp_path):
    assert comps.load_meta(tmp_path) == {}
    (tmp_path / comps.META_FILENAME).write_text("{not json", encoding="utf-8")
    assert comps.load_meta(tmp_path) == {}   # degrade: refresh falls back to first-run rules


def test_resolve_cache_path_falls_back_to_prev(tmp_path, caplog):
    """Crash between `current->.prev` and `tmp->current` leaves NO current file."""
    import logging
    cur = tmp_path / "x.csv"
    prev = tmp_path / "x.csv.prev"
    prev.write_text("data", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="domainscout.comps"):
        path, used_prev = comps.resolve_cache_path(cur, prev)
    assert path == prev and used_prev is True
    assert "loading .prev" in caplog.text   # must be LOUD, not silent


def test_resolve_cache_path_prefers_current(tmp_path):
    cur = tmp_path / "x.csv"
    prev = tmp_path / "x.csv.prev"
    cur.write_text("new", encoding="utf-8")
    prev.write_text("old", encoding="utf-8")
    assert comps.resolve_cache_path(cur, prev) == (cur, False)


def test_resolve_cache_path_both_missing_raises(tmp_path):
    with pytest.raises(comps.CompsCacheMissing):
        comps.resolve_cache_path(tmp_path / "x.csv", tmp_path / "x.csv.prev")


def test_cache_age_days(tmp_path):
    now = datetime(2026, 7, 16, 12, 0, 0)
    meta = {"retailstats": {"retrieved": (now - timedelta(days=3)).isoformat()}}
    assert round(comps.cache_age_days(meta, "retailstats", now), 1) == 3.0
    assert comps.cache_age_days(meta, "tldstats", now) is None


def _fake_client(routes):
    """routes: url-substring -> (status, body_bytes) or an Exception to raise."""
    def handler(request: httpx.Request) -> httpx.Response:
        for frag, outcome in routes.items():
            if frag in str(request.url):
                if isinstance(outcome, Exception):
                    raise outcome
                status, body = outcome
                return httpx.Response(status, content=body)
        return httpx.Response(404, content=b"")
    return httpx.Client(transport=httpx.MockTransport(handler))


def _good(path: Path) -> bytes:
    return path.read_bytes()


NOW = datetime(2026, 7, 16, 12, 0, 0)


def _crit_small():
    """Fixtures are tiny, so drop the production min_rows floors to 1."""
    from dataclasses import replace
    return replace(CRIT, comps_min_rows_retailstats=1, comps_min_rows_tldstats=1)


def test_refresh_swaps_both_and_writes_sidecar(tmp_path):
    client = _fake_client({"retailstats-download": (200, _good(RETAIL)),
                           "tldstats-download": (200, _good(TLD))})
    res = comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW)
    assert [f.action for f in res.files] == ["swapped", "swapped"]
    assert (tmp_path / "namebio_retailstats.csv").is_file()
    meta = comps.load_meta(tmp_path)
    assert meta["retailstats"]["rows"] == 6
    assert meta["retailstats"]["retrieved"] == NOW.isoformat()
    assert len(meta["retailstats"]["sha256"]) == 64


def test_refresh_retailstats_is_fetched_first(tmp_path):
    """If only one file survives the rate-limit window it must be the one Tier-2 needs."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        body = _good(RETAIL) if "retailstats" in str(request.url) else _good(TLD)
        return httpx.Response(200, content=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW)
    assert "retailstats" in seen[0]


def test_refresh_per_file_independence_429_on_second(tmp_path):
    """THE bug per-file swap exists to prevent: a tldstats 429 must NOT discard a
    validated 6.7MB retailstats bought with the long, uncharacterized 429 window."""
    client = _fake_client({"retailstats-download": (200, _good(RETAIL)),
                           "tldstats-download": (429, b"rate limited")})
    res = comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW)
    by = {f.name: f for f in res.files}
    assert by["retailstats"].action == "swapped"
    assert by["tldstats"].action == "refused" and "429" in by["tldstats"].reason
    assert (tmp_path / "namebio_retailstats.csv").is_file()   # KEPT
    assert not (tmp_path / "namebio_tldstats.csv").exists()
    assert res.any_swapped and res.any_refused


def test_refresh_429_never_retries_in_run(tmp_path):
    """Recovery takes HOURS -> in-run retry is useless. Exactly one attempt."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, content=b"")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    res = comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW)
    assert len(calls) == 2          # one attempt per file, no retries
    assert all(f.action == "refused" for f in res.files)


def test_refresh_retries_transport_error(tmp_path):
    attempts = []

    def handler(request):
        attempts.append(str(request.url))
        if "retailstats" in str(request.url) and len(attempts) == 1:
            raise httpx.ConnectError("boom")
        body = _good(RETAIL) if "retailstats" in str(request.url) else _good(TLD)
        return httpx.Response(200, content=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    res = comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW, sleep=lambda s: None)
    assert [f.action for f in res.files] == ["swapped", "swapped"]


def test_refresh_bad_download_leaves_cache_byte_identical(tmp_path):
    crit = _crit_small()
    client = _fake_client({"retailstats-download": (200, _good(RETAIL)),
                           "tldstats-download": (200, _good(TLD))})
    comps.refresh_cache(client, crit, tmp_path, now=NOW)
    before = (tmp_path / "namebio_retailstats.csv").read_bytes()

    bad = _fake_client({"retailstats-download": (200, b"<html>error</html>"),
                        "tldstats-download": (200, _good(TLD))})
    res = comps.refresh_cache(bad, crit, tmp_path, now=NOW + timedelta(days=30), force=True)
    by = {f.name: f for f in res.files}
    assert by["retailstats"].action == "refused" and "header" in by["retailstats"].reason
    assert (tmp_path / "namebio_retailstats.csv").read_bytes() == before   # untouched


def test_refresh_keeps_one_prev_on_swap(tmp_path):
    client = _fake_client({"retailstats-download": (200, _good(RETAIL)),
                           "tldstats-download": (200, _good(TLD))})
    comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW)
    comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW + timedelta(days=30))
    assert (tmp_path / "namebio_retailstats.csv.prev").is_file()


def test_refresh_noops_when_fresh(tmp_path):
    client = _fake_client({"retailstats-download": (200, _good(RETAIL)),
                           "tldstats-download": (200, _good(TLD))})
    comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW)
    res = comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW + timedelta(days=2))
    assert [f.action for f in res.files] == ["skipped_fresh", "skipped_fresh"]


def test_refresh_force_overrides_freshness(tmp_path):
    client = _fake_client({"retailstats-download": (200, _good(RETAIL)),
                           "tldstats-download": (200, _good(TLD))})
    comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW)
    res = comps.refresh_cache(client, _crit_small(), tmp_path,
                              now=NOW + timedelta(days=2), force=True)
    assert [f.action for f in res.files] == ["swapped", "swapped"]


def test_force_never_bypasses_header_check(tmp_path):
    """No flag may install an error page."""
    client = _fake_client({"retailstats-download": (200, b"<html>nope</html>"),
                           "tldstats-download": (200, _good(TLD))})
    res = comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW, force=True)
    by = {f.name: f for f in res.files}
    assert by["retailstats"].action == "refused" and "header" in by["retailstats"].reason


def test_refresh_swap_oserror_is_isolated_to_one_file(tmp_path, monkeypatch):
    """A swap-time OSError (e.g. AV file lock on Windows) on retailstats must NOT
    abort tldstats or raise out of refresh_cache — it refuses only the affected file."""
    client = _fake_client({"retailstats-download": (200, _good(RETAIL)),
                           "tldstats-download": (200, _good(TLD))})
    real_sha = comps._sha256
    def flaky_sha(path):
        if "retailstats" in str(path):
            raise OSError("simulated AV lock")
        return real_sha(path)
    monkeypatch.setattr(comps, "_sha256", flaky_sha)
    res = comps.refresh_cache(client, _crit_small(), tmp_path, now=NOW)   # must NOT raise
    by = {f.name: f for f in res.files}
    assert by["retailstats"].action == "refused" and "swap failed" in by["retailstats"].reason
    assert by["tldstats"].action == "swapped"          # sibling unaffected
    assert (tmp_path / "namebio_tldstats.csv").is_file()


@pytest.mark.skip(reason="live network - run manually against NameBio's free endpoints")
def test_live_smoke_refresh_and_lookup(tmp_path):
    from domainscout.ingest import make_client
    client = make_client()
    try:
        res = comps.refresh_cache(client, CRIT, tmp_path, force=True)
    finally:
        client.close()
    by = {f.name: f for f in res.files}
    # NB: may legitimately be REFUSED(429) if the download window has not cleared.
    assert by["retailstats"].action in ("swapped", "refused")
    if by["retailstats"].action == "swapped":
        assert by["retailstats"].rows > 50_000       # real file had 97,568
        ctx = comps.lookup("cloudvault.com", comps.load_index(tmp_path / "namebio_retailstats.csv"),
                           comps.load_tld_stats(tmp_path / "namebio_tldstats.csv"),
                           CRIT, retrieved="live")
        assert any(k.keyword == "cloud" and k.placement == "start" for k in ctx.keywords)
