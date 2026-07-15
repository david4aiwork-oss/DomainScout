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


def test_init_db_adds_filter_columns_to_existing_db(tmp_path):
    import re

    dbp = tmp_path / "d.db"
    # Simulate a REAL pre-Phase-3 DB: the shipped schema minus the 4 migrated
    # columns (a minimal hand-made table would be missing base columns the
    # indexes reference — an unrealistic starting point).
    old_schema = db.SCHEMA
    for name, _decl in db._MIGRATION_COLUMNS:
        old_schema = re.sub(rf"\n[^\n]*\b{name}\b[^\n]*,", "", old_schema, count=1)
    conn = sqlite3.connect(dbp)
    conn.executescript(old_schema)
    conn.commit()
    pre = {r[1] for r in conn.execute("PRAGMA table_info(candidates)")}
    conn.close()
    # sanity: the simulated old DB really lacks the 4 new columns
    assert not ({"track", "dict_score", "pronounce_score", "filtered_at"} & pre)

    db.init_db(dbp)  # must migrate
    conn = db.connect(dbp)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(candidates)")}
    assert {"track", "dict_score", "pronounce_score", "filtered_at"} <= cols


def test_init_db_migration_is_idempotent(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    db.init_db(dbp)  # second run must not raise (columns already exist)
    conn = db.connect(dbp)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(candidates)")}
    assert {"track", "dict_score", "pronounce_score", "filtered_at"} <= cols


def test_set_filter_result_writes_all_fields(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    cid = db.upsert_candidate(conn, Candidate(domain="redfox.com", source="whoisfreaks"))
    db.set_filter_result(
        conn, cid, track="primary", dict_score=3.4, pronounce_score=-2.1,
        filter_pass=True, filter_reason="primary dict=3.40 red+fox",
    )
    row = conn.execute(
        "SELECT track, dict_score, pronounce_score, filter_pass, filter_reason, filtered_at "
        "FROM candidates WHERE id=?", (cid,)
    ).fetchone()
    assert row["track"] == "primary"
    assert row["dict_score"] == 3.4
    assert row["pronounce_score"] == -2.1
    assert row["filter_pass"] == 1
    assert "red+fox" in row["filter_reason"]
    assert row["filtered_at"] is not None  # timestamp set


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


def test_init_db_adds_dns_status_to_existing_db(tmp_path):
    import re
    dbp = tmp_path / "d.db"
    # simulate a pre-Phase-4 DB: shipped schema minus dns_status
    old_schema = re.sub(r"\n[^\n]*\bdns_status\b[^\n]*,", "", db.SCHEMA, count=1)
    conn = sqlite3.connect(dbp)
    conn.executescript(old_schema)
    conn.commit()
    pre = {r[1] for r in conn.execute("PRAGMA table_info(candidates)")}
    conn.close()
    assert "dns_status" not in pre
    db.init_db(dbp)  # must migrate
    conn = db.connect(dbp)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(candidates)")}
    assert "dns_status" in cols


def test_set_rdap_result_writes_fields(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    cid = db.upsert_candidate(conn, Candidate(domain="foo.com", source="whoisfreaks"))
    db.set_rdap_result(
        conn, cid, lifecycle_status="redemption", rdap_status='["redemption period"]',
        expiry_date="2026-06-01", drop_date_est="2026-08-19", drop_date_actual=None,
        dns_status="nxdomain", verified_at="2026-07-15T10:00:00",
    )
    row = conn.execute(
        "SELECT lifecycle_status, rdap_status, expiry_date, drop_date_est, "
        "drop_date_actual, dns_status, verified_at FROM candidates WHERE id=?", (cid,)
    ).fetchone()
    assert row["lifecycle_status"] == "redemption"
    assert row["rdap_status"] == '["redemption period"]'
    assert row["drop_date_est"] == "2026-08-19"
    assert row["drop_date_actual"] is None
    assert row["dns_status"] == "nxdomain"
    assert row["verified_at"] == "2026-07-15T10:00:00"


def test_set_rdap_result_coalesces_first_drop_date(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    cid = db.upsert_candidate(conn, Candidate(domain="foo.com", source="whoisfreaks"))
    db.set_rdap_result(conn, cid, lifecycle_status="dropped", rdap_status="[]",
                       expiry_date=None, drop_date_est=None, drop_date_actual="2026-07-15",
                       dns_status="nxdomain", verified_at="2026-07-15T10:00:00")
    # a later confirm passes a different actual -> must NOT overwrite the first
    db.set_rdap_result(conn, cid, lifecycle_status="dropped", rdap_status="[]",
                       expiry_date=None, drop_date_est=None, drop_date_actual="2026-07-22",
                       dns_status="nxdomain", verified_at="2026-07-22T10:00:00")
    row = conn.execute("SELECT drop_date_actual FROM candidates WHERE id=?", (cid,)).fetchone()
    assert row["drop_date_actual"] == "2026-07-15"  # first one sticks


def test_set_rdap_result_leaves_filter_columns_untouched(tmp_path):
    dbp = tmp_path / "d.db"
    db.init_db(dbp)
    conn = db.connect(dbp)
    cid = db.upsert_candidate(conn, Candidate(domain="foo.com", source="whoisfreaks"))
    db.set_filter_result(conn, cid, track="primary", dict_score=3.4, pronounce_score=-2.1,
                         filter_pass=True, filter_reason="primary dict=3.40 foo")
    db.set_rdap_result(conn, cid, lifecycle_status="grace", rdap_status="[]",
                       expiry_date=None, drop_date_est="2026-08-29", drop_date_actual=None,
                       dns_status="noerror", verified_at="2026-07-15T10:00:00")
    row = conn.execute("SELECT track, dict_score, filter_pass FROM candidates WHERE id=?", (cid,)).fetchone()
    assert row["track"] == "primary" and row["dict_score"] == 3.4 and row["filter_pass"] == 1
