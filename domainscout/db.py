"""SQLite schema, connection helpers, and idempotent init-db.

Open-cycle identity model (TDD §5): a surrogate id PK plus a partial unique
index so at most ONE open cycle exists per domain. 'dropped' is an OPEN state,
so a cycle closes only on 'renewed'/'reregistered'/'dismissed'.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

from domainscout.models import Candidate, IngestCounts

DEFAULT_DB_PATH = "data/domainscout.db"

# Exact predicate — MUST be identical in the index and in every upsert conflict target.
_OPEN_PREDICATE = "lifecycle_status NOT IN ('renewed','reregistered','dismissed')"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS candidates (
  id                 INTEGER PRIMARY KEY,
  domain             TEXT NOT NULL,
  source             TEXT,
  feed_category      TEXT,
  first_seen         TIMESTAMP NOT NULL,
  expiry_date        DATE,
  drop_date_est      DATE,
  drop_date_actual   DATE,
  lifecycle_status   TEXT NOT NULL DEFAULT 'unknown',
  rdap_status        TEXT,
  verified_at        TIMESTAMP,
  filter_pass        BOOLEAN,
  filter_reason      TEXT,
  track              TEXT,
  dict_score         REAL,
  pronounce_score    REAL,
  filtered_at        TIMESTAMP,
  tier1_score        REAL,
  tier2_scores       TEXT,
  value_range        TEXT,
  rationale          TEXT,
  recommended_action TEXT,
  scored_at          TIMESTAMP,
  outcome            TEXT,
  outcome_price      REAL,
  outcome_date       DATE
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_open_cycle ON candidates(domain)
  WHERE {_OPEN_PREDICATE};

CREATE INDEX IF NOT EXISTS idx_drop_est    ON candidates(drop_date_est);
CREATE INDEX IF NOT EXISTS idx_filter_pass ON candidates(filter_pass);
CREATE INDEX IF NOT EXISTS idx_lifecycle   ON candidates(lifecycle_status);

CREATE TABLE IF NOT EXISTS ingest_log (
  run_date         DATE NOT NULL,
  source           TEXT NOT NULL,
  feed_file        TEXT NOT NULL,
  seen             INTEGER,
  rejected_tld     INTEGER,
  rejected_charset INTEGER,
  rejected_length  INTEGER,
  landed           INTEGER,
  PRIMARY KEY (run_date, source, feed_file)
);
"""


# Columns added after the initial candidates schema — migrated in on existing DBs.
_MIGRATION_COLUMNS = [
    ("track", "TEXT"),
    ("dict_score", "REAL"),
    ("pronounce_score", "REAL"),
    ("filtered_at", "TIMESTAMP"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotently add any missing post-initial columns (PRAGMA-guarded)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(candidates)")}
    for name, decl in _MIGRATION_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE candidates ADD COLUMN {name} {decl}")


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    """Create the schema. Idempotent: safe to run on every daily invocation."""
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def upsert_candidate(conn: sqlite3.Connection, candidate: Candidate) -> int:
    """Insert a new open cycle for candidate.domain, or update the existing open
    row. Returns the row id. Refreshes source/feed_category only — lifecycle_status
    (RDAP owns it post-ingestion) and first_seen (insert-only) are never touched."""
    first_seen = candidate.first_seen or datetime.now()
    if isinstance(first_seen, datetime):
        first_seen = first_seen.isoformat(timespec="seconds")
    cur = conn.execute(
        f"""
        INSERT INTO candidates (domain, source, feed_category, first_seen, lifecycle_status)
        VALUES (:domain, :source, :feed_category, :first_seen, :lifecycle_status)
        ON CONFLICT(domain) WHERE {_OPEN_PREDICATE}
        DO UPDATE SET
            source = excluded.source,
            feed_category = excluded.feed_category
        RETURNING id
        """,
        {
            "domain": candidate.domain,
            "source": candidate.source,
            "feed_category": candidate.feed_category,
            "first_seen": first_seen,
            "lifecycle_status": candidate.lifecycle_status,
        },
    )
    row_id = cur.fetchone()[0]
    conn.commit()
    return row_id


def set_filter_result(
    conn: sqlite3.Connection,
    candidate_id: int,
    *,
    track: str,
    dict_score: float,
    pronounce_score: float,
    filter_pass: bool,
    filter_reason: str,
    filtered_at: str | None = None,
) -> None:
    """Write the 6 Phase-3 filter columns for one candidate. Touches nothing else."""
    stamp = filtered_at or datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE candidates
           SET track = ?, dict_score = ?, pronounce_score = ?,
               filter_pass = ?, filter_reason = ?, filtered_at = ?
         WHERE id = ?
        """,
        (track, dict_score, pronounce_score, 1 if filter_pass else 0,
         filter_reason, stamp, candidate_id),
    )
    conn.commit()


def record_ingest(conn: sqlite3.Connection, counts: IngestCounts) -> None:
    """Upsert one ingest_log row, keyed (run_date, source, feed_file). Re-running
    a day's file recomputes and overwrites the counts (idempotent)."""
    run_date = counts.run_date or date.today()
    if isinstance(run_date, date):
        run_date = run_date.isoformat()
    conn.execute(
        """
        INSERT INTO ingest_log
            (run_date, source, feed_file, seen, rejected_tld, rejected_charset, rejected_length, landed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_date, source, feed_file) DO UPDATE SET
            seen = excluded.seen,
            rejected_tld = excluded.rejected_tld,
            rejected_charset = excluded.rejected_charset,
            rejected_length = excluded.rejected_length,
            landed = excluded.landed
        """,
        (
            run_date, counts.source, counts.feed_file, counts.seen,
            counts.rejected_tld, counts.rejected_charset, counts.rejected_length, counts.landed,
        ),
    )
    conn.commit()
