from dataclasses import replace
from pathlib import Path

from domainscout import db
from domainscout.config import load_criteria
from domainscout.filters import classify, decide, dict_score, filter_candidates
from domainscout.models import Candidate

CRIT = load_criteria(Path(__file__).resolve().parents[1] / "criteria.toml")


def _crit_invented(value):
    # clone CRIT with primary_allow_invented toggled
    return replace(CRIT, primary_allow_invented=value)


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


def test_decide_primary_dictionary_pass():
    ok, reason = decide("primary", 4.2, "red+fox", -9.0, CRIT)
    assert ok and reason.startswith("primary dict=4.2")


def test_decide_primary_invented_pass_when_allowed():
    ok, reason = decide("primary", 0.0, "zylo", -1.0, _crit_invented(True))
    assert ok and "pronounce=" in reason


def test_decide_primary_invented_reject_when_disallowed():
    ok, reason = decide("primary", 0.0, "zylo", -1.0, _crit_invented(False))
    assert not ok and reason.startswith("reject primary")


def test_decide_secondary_pronounce_only():
    ok, reason = decide("secondary", 0.0, "brixly", -1.0, CRIT)
    assert ok and "pronounce=" in reason


def test_decide_secondary_dict_only():
    ok, reason = decide("secondary", 3.4, "maple+desk", -99.0, CRIT)
    assert ok and reason.startswith("secondary dict=3.4")


def test_decide_secondary_both_fail():
    ok, reason = decide("secondary", 1.0, "zzqx", -99.0, CRIT)
    assert not ok and reason.startswith("reject secondary")


def _seed(conn, domains):
    return [db.upsert_candidate(conn, Candidate(domain=d, source="whoisfreaks")) for d in domains]


def test_filter_candidates_writes_fields_and_counts(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    _seed(conn, ["apple.com", "zzqxvv.com"])   # apple passes, zzqxvv rejects
    counts = filter_candidates(conn, CRIT)
    assert counts.processed == 2
    row = conn.execute("SELECT track, dict_score, pronounce_score, filter_pass, filtered_at "
                       "FROM candidates WHERE domain='apple.com'").fetchone()
    assert row["track"] == "primary"
    assert row["filter_pass"] == 1
    assert row["filtered_at"] is not None
    assert row["dict_score"] is not None and row["pronounce_score"] is not None


def test_filter_candidates_idempotent_and_recompute(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    _seed(conn, ["apple.com"])
    filter_candidates(conn, CRIT)
    again = filter_candidates(conn, CRIT)          # nothing new (filtered_at set)
    assert again.processed == 0
    forced = filter_candidates(conn, CRIT, recompute=True)
    assert forced.processed == 1


def test_recompute_does_not_touch_downstream_columns(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    (cid,) = _seed(conn, ["apple.com"])
    filter_candidates(conn, CRIT)
    conn.execute("UPDATE candidates SET tier1_score=7.0, verified_at='2026-07-14' WHERE id=?", (cid,))
    conn.commit()
    filter_candidates(conn, CRIT, recompute=True)
    row = conn.execute("SELECT tier1_score, verified_at FROM candidates WHERE id=?", (cid,)).fetchone()
    assert row["tier1_score"] == 7.0 and row["verified_at"] == "2026-07-14"


def test_filter_candidates_dry_run_writes_nothing(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    _seed(conn, ["apple.com"])
    counts = filter_candidates(conn, CRIT, dry_run=True)
    assert counts.processed == 1
    row = conn.execute("SELECT filtered_at FROM candidates WHERE domain='apple.com'").fetchone()
    assert row["filtered_at"] is None
