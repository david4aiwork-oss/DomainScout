from pathlib import Path

from domainscout.config import load_criteria
from domainscout.ingest import gate

CRIT = load_criteria(Path(__file__).resolve().parents[1] / "criteria.toml")


def test_gate_passes_plain_com():
    assert gate("apple.com", CRIT) == (True, None)


def test_gate_normalizes_case_and_whitespace():
    assert gate("  GOOGLE.COM \n", CRIT) == (True, None)


def test_gate_rejects_non_com_tld():
    assert gate("armorbeef.net", CRIT) == (False, "rejected_tld")
    assert gate("example.org", CRIT) == (False, "rejected_tld")


def test_gate_rejects_hyphen_digit_dot_in_label():
    assert gate("bar-baz.com", CRIT) == (False, "rejected_charset")
    assert gate("abc123.com", CRIT) == (False, "rejected_charset")
    assert gate("sub.domain.com", CRIT) == (False, "rejected_charset")


def test_gate_rejects_empty_label():
    assert gate(".com", CRIT) == (False, "rejected_charset")


def test_gate_length_boundaries():
    assert gate("converse.com", CRIT) == (True, None)       # label len 8
    assert gate("zebuervamate.com", CRIT) == (True, None)   # label len 12 (ceiling)
    assert gate("toolongdomain.com", CRIT) == (False, "rejected_length")  # label len 13


def test_gate_first_failure_wins():
    # non-.com AND bad charset -> tld reported first
    assert gate("bad_label.net", CRIT) == (False, "rejected_tld")
