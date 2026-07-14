from pathlib import Path

import pytest

from domainscout.config import ConfigError, load_criteria

REPO_ROOT = Path(__file__).resolve().parents[1]

VALID_TOML = """
[ingestion]
tld = "com"
charset = "^[a-z]+$"
sources = ["whoisfreaks", "dynadot"]
schedule_hint = "late-morning"

[primary]
max_length = 8
max_words = 2

[secondary]
min_length = 9
max_length = 12

[dictionary]
zipf_min = 3.0

[pronounceability]
min_score = 0.02

[scoring]
tier2_cutoff = 30
digest_top_n = 10

[rdap]
endpoint = "https://rdap.verisign.com/com/v1/"
max_requests_per_sec = 1.0

[retention]
days = 360
"""


def _write(tmp_path, text):
    p = tmp_path / "criteria.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_valid_config_loads_and_derives_ingest_ceiling(tmp_path):
    crit = load_criteria(_write(tmp_path, VALID_TOML))
    assert crit.tld == "com"
    assert crit.charset == "^[a-z]+$"
    assert crit.sources == ("whoisfreaks", "dynadot")
    assert crit.primary_max_length == 8
    assert crit.secondary_max_length == 12
    # DERIVED ceiling = widest target (12), never a duplicated literal
    assert crit.ingest_max_length == 12
    assert crit.retention_days == 360


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_criteria(tmp_path / "nope.toml")


def test_non_com_tld_rejected(tmp_path):
    bad = VALID_TOML.replace('tld = "com"', 'tld = "net"')
    with pytest.raises(ConfigError, match="tld must be 'com'"):
        load_criteria(_write(tmp_path, bad))


def test_missing_key_names_the_key(tmp_path):
    bad = VALID_TOML.replace("zipf_min = 3.0", "")
    with pytest.raises(ConfigError, match="zipf_min"):
        load_criteria(_write(tmp_path, bad))


def test_invalid_charset_regex_rejected(tmp_path):
    bad = VALID_TOML.replace('charset = "^[a-z]+$"', 'charset = "^[a-z"')
    with pytest.raises(ConfigError, match="charset"):
        load_criteria(_write(tmp_path, bad))


def test_repo_criteria_toml_is_valid():
    # Guards against the shipped config drifting out of sync with the loader.
    crit = load_criteria(REPO_ROOT / "criteria.toml")
    assert crit.tld == "com"
    assert crit.ingest_max_length == max(crit.primary_max_length, crit.secondary_max_length)
