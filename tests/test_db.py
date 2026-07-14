import sqlite3

import pytest

from domainscout import db


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _indexes(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def test_init_db_creates_tables_and_indexes(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = sqlite3.connect(dbp)
    assert {"candidates", "ingest_log"} <= _tables(conn)
    assert "ux_open_cycle" in _indexes(conn)


def test_init_db_is_idempotent(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    db.init_db(dbp)  # must not raise
    conn = sqlite3.connect(dbp)
    assert {"candidates", "ingest_log"} <= _tables(conn)


def test_init_db_creates_parent_directory(tmp_path):
    dbp = tmp_path / "nested" / "data" / "d.db"
    db.init_db(dbp)
    assert dbp.exists()


def test_lifecycle_status_defaults_to_unknown(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    conn.execute(
        "INSERT INTO candidates (domain, source, first_seen) VALUES ('foo.com', 'wf', '2026-07-14')"
    )
    conn.commit()
    row = conn.execute("SELECT lifecycle_status FROM candidates WHERE domain='foo.com'").fetchone()
    assert row["lifecycle_status"] == "unknown"


def test_lifecycle_status_not_null_rejects_explicit_null(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO candidates (domain, first_seen, lifecycle_status) "
            "VALUES ('bar.com', '2026-07-14', NULL)"
        )


from domainscout.models import Candidate, IngestCounts


def test_upsert_two_open_rows_collapse_to_one(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    id1 = db.upsert_candidate(conn, Candidate(domain="foo.com", source="whoisfreaks"))
    id2 = db.upsert_candidate(conn, Candidate(domain="foo.com", source="dynadot"))
    assert id1 == id2  # same open cycle
    rows = conn.execute("SELECT source FROM candidates WHERE domain='foo.com'").fetchall()
    assert len(rows) == 1
    assert rows[0]["source"] == "dynadot"  # source refreshed


def test_reingest_does_not_reset_lifecycle_status(tmp_path):
    # The exact bug the open-cycle amendment guards: re-ingest must not clobber
    # an RDAP-advanced lifecycle_status back to 'unknown'.
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    db.upsert_candidate(conn, Candidate(domain="foo.com", source="whoisfreaks"))
    conn.execute("UPDATE candidates SET lifecycle_status='dropped' WHERE domain='foo.com'")
    conn.commit()
    db.upsert_candidate(conn, Candidate(domain="foo.com", source="whoisfreaks"))  # incoming 'unknown'
    row = conn.execute("SELECT lifecycle_status FROM candidates WHERE domain='foo.com'").fetchone()
    assert row["lifecycle_status"] == "dropped"  # preserved
    assert conn.execute("SELECT COUNT(*) c FROM candidates").fetchone()["c"] == 1


def test_upsert_preserves_first_seen(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    db.upsert_candidate(conn, Candidate(domain="foo.com", source="whoisfreaks"))
    first = conn.execute("SELECT first_seen FROM candidates WHERE domain='foo.com'").fetchone()["first_seen"]
    db.upsert_candidate(conn, Candidate(domain="foo.com", source="dynadot"))
    second = conn.execute("SELECT first_seen FROM candidates WHERE domain='foo.com'").fetchone()["first_seen"]
    assert first == second


def test_closed_and_open_rows_coexist(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    # cycle 1 closed (reregistered) — inserted directly, outside the partial index
    conn.execute(
        "INSERT INTO candidates (domain, source, first_seen, lifecycle_status) "
        "VALUES ('foo.com', 'whoisfreaks', '2026-01-01', 'reregistered')"
    )
    conn.commit()
    # cycle 2 opens via upsert — no conflict with a (nonexistent) open row
    db.upsert_candidate(conn, Candidate(domain="foo.com", source="whoisfreaks"))
    count = conn.execute("SELECT COUNT(*) c FROM candidates WHERE domain='foo.com'").fetchone()["c"]
    assert count == 2


def test_record_ingest_is_idempotent_per_file(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    counts = IngestCounts(
        source="whoisfreaks",
        feed_file="2026-07-14-free-dropped-domains.csv",
        seen=10000, rejected_tld=5000, rejected_charset=4800, rejected_length=150, landed=50,
        run_date=None,
    )
    db.record_ingest(conn, counts)
    counts.landed = 55  # a re-run recomputes
    db.record_ingest(conn, counts)
    rows = conn.execute("SELECT landed FROM ingest_log").fetchall()
    assert len(rows) == 1
    assert rows[0]["landed"] == 55
