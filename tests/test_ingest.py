from pathlib import Path

import httpx
import pytest

from domainscout import ingest
from domainscout.sources.base import FeedFile


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_download_writes_file_and_returns_path(tmp_path):
    ff = FeedFile(source="whoisfreaks", feed_category="expired",
                  remote_url="https://host/x.csv", local_name="x.csv")
    client = _client(lambda req: httpx.Response(200, content=b"apple.com\n"))
    dest = ingest.download(ff, tmp_path / "feeds", client)
    assert dest == tmp_path / "feeds" / "x.csv"
    assert dest.read_bytes() == b"apple.com\n"


def test_download_skips_when_file_exists(tmp_path):
    ff = FeedFile(source="whoisfreaks", feed_category="expired",
                  remote_url="https://host/x.csv", local_name="x.csv")
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, content=b"apple.com\n")

    client = _client(handler)
    ingest.download(ff, tmp_path / "feeds", client)
    ingest.download(ff, tmp_path / "feeds", client)  # second call: file present
    assert calls["n"] == 1  # network hit only once


def test_download_raises_on_404(tmp_path):
    ff = FeedFile(source="whoisfreaks", feed_category="expired",
                  remote_url="https://host/missing.csv", local_name="missing.csv")
    client = _client(lambda req: httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        ingest.download(ff, tmp_path / "feeds", client)
