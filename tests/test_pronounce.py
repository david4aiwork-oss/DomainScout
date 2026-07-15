import json
import math

import pytest

from domainscout import pronounce

# an English-ish training vocab that makes '-and'/'br-' patterns common
FIXTURE_WORDS = ["brand", "brandy", "band", "land", "sand", "hand", "grand",
                 "stand", "bland", "brain", "bread", "break", "brown"]


@pytest.fixture
def model():
    return pronounce.Model.from_tables(pronounce.build_tables(words=FIXTURE_WORDS))


def test_build_tables_counts_and_padding():
    t = pronounce.build_tables(words=["fox", "ox"])
    # "^^fox$" trigrams: ^^f ^fo fox ox$ ; "^^ox$": ^^o ^ox ox$
    assert t["trigram_counts"]["ox$"] == 2   # appears in both
    assert t["trigram_counts"]["fox"] == 1
    assert t["context2_totals"]["ox"] == 2   # 'ox' precedes '$' in both words
    assert "_meta" in t and t["_meta"]["alphabet"]


def test_build_tables_filters_non_alpha_words():
    t = pronounce.build_tables(words=["fox", "f0x", "fo-x", "FOX", ""])
    # only "fox" survives the ^[a-z]+$ filter
    assert t["trigram_counts"].get("fox") == 1


def test_save_tables_is_sorted_and_loadable(tmp_path):
    t = pronounce.build_tables(words=["fox", "ox"])
    p = tmp_path / "tables.json"
    pronounce.save_tables(t, p)
    raw = p.read_text(encoding="utf-8")
    loaded = json.loads(raw)
    assert loaded["trigram_counts"]["fox"] == 1
    # sorted keys => "context2_totals" appears before "trigram_counts"
    assert raw.index('"context2_totals"') < raw.index('"trigram_counts"')


def test_score_orders_realish_above_mash(model):
    assert pronounce.score("brand", model) > pronounce.score("xqzk", model)
    assert pronounce.score("bland", model) > pronounce.score("xqzk", model)


def test_score_smoothing_is_finite(model):
    s = pronounce.score("xqzk", model)  # all-unseen trigrams
    assert math.isfinite(s)             # add-one => never -inf


def test_score_scale_contract(model):
    # pins the SPACE: every score finite, log-space bound (<= 0), and monotonic
    labels = ["brand", "bland", "xqzk"]
    scores = [pronounce.score(x, model) for x in labels]
    assert all(math.isfinite(s) and s <= 0.0 for s in scores)
    assert scores[0] >= scores[1] >= scores[2]
