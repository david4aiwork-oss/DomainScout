from pathlib import Path

from domainscout.config import load_criteria
from domainscout.filters import classify, dict_score

CRIT = load_criteria(Path(__file__).resolve().parents[1] / "criteria.toml")


def test_classify_boundaries():
    assert classify("converse", CRIT) == "primary"     # len 8
    assert classify("ninechars", CRIT) == "secondary"  # len 9
    assert classify("zebuervamat", CRIT) == "secondary" # len 11


def test_dict_score_whole_word():
    score, seg = dict_score("apple", CRIT)
    assert score > 4.0
    assert seg == "apple"


def test_dict_score_two_way_split():
    score, seg = dict_score("redfox", CRIT)   # red + fox, min-combine
    assert seg == "red+fox"
    assert score > 3.0


def test_dict_score_nonword_near_zero():
    score, seg = dict_score("xqzk", CRIT)
    assert score == 0.0


def test_dict_score_no_single_char_fragments():
    # 'a'+'pple' must not win via common single letter 'a'
    score, seg = dict_score("apple", CRIT)
    assert "+" not in seg  # whole word wins, not a 1-char split


def test_dict_score_no_two_char_fragment_noise():
    # Regression: split parts are floored at 3 chars. A 2-char floor would admit
    # consonant-mash via wordfreq's noisy 2-letter zipf (th=4.2, ng=3.9), letting
    # 'thng' -> 'th+ng' (min 3.9) falsely clear the dictionary gate (zipf_min=3.0).
    score, seg = dict_score("thng", CRIT)
    assert score < CRIT.zipf_min  # must NOT clear the dictionary gate
    assert "+" not in seg         # no valid >=3+>=3 split for a 4-char label
