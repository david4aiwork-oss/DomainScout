from datetime import date
from pathlib import Path

import pytest

from domainscout.config import ConfigError, WhoisFreaksConfig
from domainscout.sources.base import FeedFile
from domainscout.sources.dynadot import DynadotSource
from domainscout.sources.whoisfreaks import WhoisFreaksSource

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "whoisfreaks-sample.csv"


def _wf():
    return WhoisFreaksSource(
        base_url="https://host/repo/main",
        expired_filename="{date}-free-expired-domains.csv",
        dropped_filename="{date}-free-dropped-domains.csv",
    )


class _CriteriaStub:
    def __init__(self, whoisfreaks):
        self.whoisfreaks = whoisfreaks


def test_feedfile_is_frozen_with_expected_fields():
    ff = FeedFile(source="whoisfreaks", feed_category="expired",
                  remote_url="https://h/x.csv", local_name="x.csv")
    assert (ff.source, ff.feed_category, ff.remote_url, ff.local_name) == (
        "whoisfreaks", "expired", "https://h/x.csv", "x.csv")
    with pytest.raises(Exception):
        ff.source = "other"  # frozen


def test_dynadot_stub_raises_phase_2b():
    src = DynadotSource.from_criteria(criteria=None)
    assert src.name == "dynadot"
    with pytest.raises(NotImplementedError, match="Phase 2b"):
        src.feed_files(date(2026, 7, 13))
    with pytest.raises(NotImplementedError, match="Phase 2b"):
        list(src.iter_domains(Path("nope.csv")))


def test_whoisfreaks_feed_files_builds_expired_and_dropped():
    files = _wf().feed_files(date(2026, 7, 13))
    assert [f.feed_category for f in files] == ["expired", "dropped"]
    assert files[0].local_name == "2026-07-13-free-expired-domains.csv"
    assert files[0].remote_url == "https://host/repo/main/2026-07-13-free-expired-domains.csv"
    assert files[1].local_name == "2026-07-13-free-dropped-domains.csv"
    assert all(f.source == "whoisfreaks" for f in files)


def test_whoisfreaks_iter_domains_yields_raw_names_skipping_blanks():
    names = list(_wf().iter_domains(FIXTURE))
    assert names == [
        "armorbeef.net", "zebuervamate.com", "apple.com", "GOOGLE.COM",
        "converse.com", "bar-baz.com", "abc123.com", "toolongdomain.com",
        "short.com", "nickel.com", "sub.domain.com", "example.org",
    ]  # 12 raw names, un-normalized, blank line dropped


def test_whoisfreaks_from_criteria_requires_config():
    with pytest.raises(ConfigError, match="whoisfreaks"):
        WhoisFreaksSource.from_criteria(_CriteriaStub(whoisfreaks=None))


def test_whoisfreaks_from_criteria_builds_from_config():
    cfg = WhoisFreaksConfig(
        base_url="https://host/repo/main",
        expired_filename="{date}-free-expired-domains.csv",
        dropped_filename="{date}-free-dropped-domains.csv",
    )
    src = WhoisFreaksSource.from_criteria(_CriteriaStub(whoisfreaks=cfg))
    assert src.name == "whoisfreaks"
    assert src.feed_files(date(2026, 7, 13))[0].remote_url.endswith(
        "/2026-07-13-free-expired-domains.csv")
