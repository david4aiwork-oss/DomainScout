from datetime import date
from pathlib import Path

import httpx
import pytest

from domainscout import db, ingest
from domainscout.config import load_criteria
from domainscout.sources.base import FeedFile
from domainscout.sources.whoisfreaks import WhoisFreaksSource

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "whoisfreaks-sample.csv"
CRIT = load_criteria(REPO_ROOT / "criteria.toml")
LANDED = {"zebuervamate.com", "apple.com", "google.com",
          "converse.com", "short.com", "nickel.com"}


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _conn(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    return db.connect(dbp)


def _source():
    return WhoisFreaksSource.from_criteria(CRIT)


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


def test_ingest_file_counts_and_lands_survivors(tmp_path):
    conn = _conn(tmp_path)
    counts = ingest.ingest_file(
        conn, _source(), path=FIXTURE, feed_category="expired",
        feed_file_name="whoisfreaks-sample.csv", run_date=date(2026, 7, 13),
        criteria=CRIT,
    )
    assert (counts.seen, counts.rejected_tld, counts.rejected_charset,
            counts.rejected_length, counts.landed) == (12, 2, 3, 1, 6)
    rows = {r["domain"] for r in conn.execute("SELECT domain FROM candidates")}
    assert rows == LANDED


def test_ingest_file_sets_category_leaves_lifecycle_unknown(tmp_path):
    conn = _conn(tmp_path)
    ingest.ingest_file(
        conn, _source(), path=FIXTURE, feed_category="dropped",
        feed_file_name="f.csv", run_date=date(2026, 7, 13), criteria=CRIT,
    )
    row = conn.execute(
        "SELECT feed_category, lifecycle_status, source FROM candidates "
        "WHERE domain='apple.com'"
    ).fetchone()
    assert row["feed_category"] == "dropped"
    assert row["lifecycle_status"] == "unknown"
    assert row["source"] == "whoisfreaks"


def test_ingest_file_writes_ingest_log(tmp_path):
    conn = _conn(tmp_path)
    ingest.ingest_file(
        conn, _source(), path=FIXTURE, feed_category="expired",
        feed_file_name="f.csv", run_date=date(2026, 7, 13), criteria=CRIT,
    )
    log = conn.execute("SELECT * FROM ingest_log").fetchone()
    assert log["seen"] == 12 and log["landed"] == 6
    assert log["source"] == "whoisfreaks" and log["feed_file"] == "f.csv"


def test_ingest_file_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    kw = dict(path=FIXTURE, feed_category="expired", feed_file_name="f.csv",
              run_date=date(2026, 7, 13), criteria=CRIT)
    ingest.ingest_file(conn, _source(), **kw)
    first_seen = conn.execute(
        "SELECT first_seen FROM candidates WHERE domain='apple.com'").fetchone()[0]
    ingest.ingest_file(conn, _source(), **kw)  # re-run
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM ingest_log").fetchone()[0] == 1
    again = conn.execute(
        "SELECT first_seen FROM candidates WHERE domain='apple.com'").fetchone()[0]
    assert again == first_seen  # first_seen preserved


def test_ingest_file_dry_run_writes_nothing(tmp_path):
    conn = _conn(tmp_path)
    counts = ingest.ingest_file(
        conn, _source(), path=FIXTURE, feed_category="expired",
        feed_file_name="f.csv", run_date=date(2026, 7, 13), criteria=CRIT,
        dry_run=True,
    )
    assert counts.landed == 6  # still tallied
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM ingest_log").fetchone()[0] == 0
