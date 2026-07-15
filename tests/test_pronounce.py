import json

from domainscout import pronounce


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
