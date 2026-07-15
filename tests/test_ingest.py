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


def test_make_client_builds_os_trust_store_client():
    client = ingest.make_client()
    try:
        assert isinstance(client, httpx.Client)
    finally:
        client.close()


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


def test_infer_feed_category():
    assert ingest.infer_feed_category("2026-07-13-free-expired-domains.csv") == "expired"
    assert ingest.infer_feed_category("2026-07-13-free-dropped-domains.csv") == "dropped"
    assert ingest.infer_feed_category("mystery.csv") is None


def test_build_source_unknown_raises():
    with pytest.raises(ValueError, match="unknown source"):
        ingest.build_source("nope", CRIT)


def test_ingest_source_downloads_and_ingests_both_files(tmp_path):
    conn = _conn(tmp_path)
    body = FIXTURE.read_bytes()
    client = _client(lambda req: httpx.Response(200, content=body))
    results = ingest.ingest_source(
        conn, _source(), date(2026, 7, 13), CRIT, tmp_path / "feeds", client)
    assert [c.feed_file for c in results] == [
        "2026-07-13-free-expired-domains.csv",
        "2026-07-13-free-dropped-domains.csv",
    ]
    # both files carry the same fixture -> same 6 domains collapse to 6 open rows
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM ingest_log").fetchone()[0] == 2


def test_ingest_source_skips_404(tmp_path):
    conn = _conn(tmp_path)
    client = _client(lambda req: httpx.Response(404))
    results = ingest.ingest_source(
        conn, _source(), date(2026, 7, 13), CRIT, tmp_path / "feeds", client)
    assert results == []  # both 404 -> skipped, no crash
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0


def test_ingest_local_file_infers_category(tmp_path):
    conn = _conn(tmp_path)
    named = tmp_path / "2026-07-13-free-dropped-domains.csv"
    named.write_bytes(FIXTURE.read_bytes())
    counts = ingest.ingest_local_file(
        conn, path=named, criteria=CRIT, run_date=date(2026, 7, 13))
    assert counts.landed == 6
    row = conn.execute(
        "SELECT feed_category FROM candidates WHERE domain='apple.com'").fetchone()
    assert row["feed_category"] == "dropped"


def test_ingest_local_file_unknown_category_raises(tmp_path):
    conn = _conn(tmp_path)
    mystery = tmp_path / "mystery.csv"
    mystery.write_bytes(FIXTURE.read_bytes())
    with pytest.raises(ValueError, match="feed.category"):
        ingest.ingest_local_file(
            conn, path=mystery, criteria=CRIT, run_date=date(2026, 7, 13))


def test_run_ingest_skips_dynadot_stub_with_notice(tmp_path, capsys):
    conn = _conn(tmp_path)
    body = FIXTURE.read_bytes()
    client = _client(lambda req: httpx.Response(200, content=body))
    results = ingest.run_ingest(
        conn, criteria=CRIT, run_date=date(2026, 7, 13),
        source_names=["whoisfreaks", "dynadot"], feeds_dir=tmp_path / "feeds",
        client=client)
    out = capsys.readouterr().out.lower()
    assert "dynadot" in out and "phase 2b" in out
    assert len(results) == 2  # only whoisfreaks' two files


def test_summary_line_mentions_landed():
    from domainscout.models import IngestCounts
    line = ingest.summary_line(IngestCounts(
        source="whoisfreaks", feed_file="f.csv", seen=12,
        rejected_tld=2, rejected_charset=3, rejected_length=1, landed=6))
    assert "landed=6" in line and "whoisfreaks" in line
