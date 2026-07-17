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

WF_SECTION = """
[sources.whoisfreaks]
base_url = "https://raw.githubusercontent.com/WhoisFreaks/daily-expired-and-dropped-domains/main"
expired_filename = "{date}-free-expired-domains.csv"
dropped_filename = "{date}-free-dropped-domains.csv"
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


def test_whoisfreaks_config_absent_is_none(tmp_path):
    crit = load_criteria(_write(tmp_path, VALID_TOML))
    assert crit.whoisfreaks is None


def test_whoisfreaks_config_loads(tmp_path):
    crit = load_criteria(_write(tmp_path, VALID_TOML + WF_SECTION))
    assert crit.whoisfreaks is not None
    assert crit.whoisfreaks.base_url.endswith("/main")
    assert crit.whoisfreaks.expired_filename == "{date}-free-expired-domains.csv"
    assert crit.whoisfreaks.dropped_filename == "{date}-free-dropped-domains.csv"


def test_whoisfreaks_missing_key_raises(tmp_path):
    bad = VALID_TOML + WF_SECTION.replace(
        'expired_filename = "{date}-free-expired-domains.csv"\n', ""
    )
    with pytest.raises(ConfigError, match="expired_filename"):
        load_criteria(_write(tmp_path, bad))


def test_filter_knobs_default_when_absent(tmp_path):
    crit = load_criteria(_write(tmp_path, VALID_TOML))  # VALID_TOML has no allow_invented/combine
    assert crit.primary_allow_invented is True
    assert crit.dictionary_combine == "min"


def test_filter_knobs_explicit(tmp_path):
    toml = VALID_TOML.replace("[primary]\n", "[primary]\nallow_invented = false\n")
    toml = toml.replace("[dictionary]\n", "[dictionary]\ncombine = \"mean\"\n")
    crit = load_criteria(_write(tmp_path, toml))
    assert crit.primary_allow_invented is False
    assert crit.dictionary_combine == "mean"


def test_bad_combine_rejected(tmp_path):
    toml = VALID_TOML.replace("[dictionary]\n", "[dictionary]\ncombine = \"median\"\n")
    with pytest.raises(ConfigError, match="combine"):
        load_criteria(_write(tmp_path, toml))


def test_repo_criteria_toml_is_valid():
    # Guards against the shipped config drifting out of sync with the loader.
    crit = load_criteria(REPO_ROOT / "criteria.toml")
    assert crit.tld == "com"
    assert crit.ingest_max_length == max(crit.primary_max_length, crit.secondary_max_length)
    assert crit.whoisfreaks is not None
    assert "WhoisFreaks/daily-expired-and-dropped-domains" in crit.whoisfreaks.base_url


def test_criteria_has_rdap_defaults(tmp_path):
    from domainscout.config import load_criteria
    crit = load_criteria("criteria.toml")
    assert crit.rdap_concurrency == 5
    assert crit.rdap_max_retries == 4
    assert crit.rdap_timeout == 15.0
    assert "personal expired-domain research" in crit.rdap_user_agent
    assert crit.rdap_recheck_days["pending_delete"] == 1
    assert crit.rdap_recheck_days["redemption"] == 2
    assert crit.rdap_recheck_days["grace"] == 7
    assert crit.rdap_recheck_days["dropped"] == 7
    assert "expiring" not in crit.rdap_recheck_days  # dead key removed


def test_rdap_recheck_days_defaults_when_table_absent(tmp_path):
    from domainscout.config import load_criteria
    toml = tmp_path / "c.toml"
    # minimal criteria without [rdap.recheck_days]
    text = '''[ingestion]
tld = "com"
charset = "^[a-z]+$"
sources = ["whoisfreaks"]
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
min_score = -4.0
[scoring]
tier2_cutoff = 30
digest_top_n = 10
[rdap]
endpoint = "https://rdap.verisign.com/com/v1/"
max_requests_per_sec = 1.0
[retention]
days = 360
'''
    toml.write_text(text, encoding="utf-8")
    crit = load_criteria(toml)
    assert crit.rdap_recheck_days == {"pending_delete": 1, "redemption": 2, "grace": 7, "dropped": 7}
    assert crit.rdap_concurrency == 5  # default when [rdap].concurrency absent


def test_criteria_has_comps_defaults():
    crit = load_criteria(REPO_ROOT / "criteria.toml")
    assert crit.comps_base_url == "https://api.namebio.com"
    assert crit.comps_retailstats_path == "/retailstats-download"
    assert crit.comps_tldstats_path == "/tldstats-download"
    assert crit.comps_refresh_days == 7
    assert crit.comps_shrink_tolerance == 0.8
    assert crit.comps_min_rows_retailstats == 1000
    assert crit.comps_min_rows_tldstats == 100
    assert crit.comps_stale_warn_factor == 3


def test_comps_section_is_optional(tmp_path):
    """A criteria.toml with no [comps] still loads, using dataclass defaults."""
    src = (REPO_ROOT / "criteria.toml").read_text(encoding="utf-8")
    trimmed = src.split("[comps]")[0]
    p = tmp_path / "c.toml"
    p.write_text(trimmed, encoding="utf-8")
    crit = load_criteria(p)
    assert crit.comps_refresh_days == 7


def test_comps_refresh_days_must_be_int(tmp_path):
    src = (REPO_ROOT / "criteria.toml").read_text(encoding="utf-8")
    p = tmp_path / "c.toml"
    p.write_text(src.replace("refresh_days = 7", 'refresh_days = "weekly"'), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"\[comps\].refresh_days"):
        load_criteria(p)
