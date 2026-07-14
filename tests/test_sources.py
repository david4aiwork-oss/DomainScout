from datetime import date
from pathlib import Path

import pytest

from domainscout.sources.base import FeedFile
from domainscout.sources.dynadot import DynadotSource


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
