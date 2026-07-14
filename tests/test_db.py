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
