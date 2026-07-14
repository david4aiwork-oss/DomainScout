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
        conn.commit()
    finally:
        conn.close()
