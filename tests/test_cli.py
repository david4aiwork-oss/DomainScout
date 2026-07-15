import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from domainscout.__main__ import main

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_init_db_subcommand_creates_database(tmp_path, capsys):
    dbp = tmp_path / "d.db"
    rc = main(["--db", str(dbp), "init-db"])
    assert rc == 0
    assert dbp.exists()
    out = capsys.readouterr().out.lower()
    assert "initialized" in out
    conn = sqlite3.connect(dbp)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"candidates", "ingest_log"} <= names


def test_init_db_is_idempotent_via_cli(tmp_path):
    dbp = tmp_path / "d.db"
    assert main(["--db", str(dbp), "init-db"]) == 0
    assert main(["--db", str(dbp), "init-db"]) == 0  # second run must not error


def test_stub_subcommand_reports_phase(capsys):
    rc = main(["verify"])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "not implemented" in out
    assert "phase 4" in out


FIXTURE = REPO_ROOT / "tests" / "fixtures" / "whoisfreaks-sample.csv"


def test_ingest_cli_file_creates_rows_and_prints_summary(tmp_path, capsys):
    dbp = tmp_path / "d.db"
    assert main(["--db", str(dbp), "init-db"]) == 0
    capsys.readouterr()  # drop init-db output
    rc = main(["--db", str(dbp), "ingest", "--file", str(FIXTURE),
               "--feed-category", "expired",
               "--criteria", str(REPO_ROOT / "criteria.toml")])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "landed=6" in out
    conn = sqlite3.connect(dbp)
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 6


def test_ingest_cli_dry_run_writes_nothing(tmp_path):
    dbp = tmp_path / "d.db"
    assert main(["--db", str(dbp), "init-db"]) == 0
    rc = main(["--db", str(dbp), "ingest", "--file", str(FIXTURE),
               "--feed-category", "expired", "--dry-run",
               "--criteria", str(REPO_ROOT / "criteria.toml")])
    assert rc == 0
    conn = sqlite3.connect(dbp)
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0


def test_score_subcommands_exist_and_stub(capsys):
    assert main(["score-submit"]) == 0
    assert main(["score-collect"]) == 0
    out = capsys.readouterr().out.lower()
    assert out.count("phase 5") == 2


def test_outcome_help_records_dismiss_intent(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["outcome", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out.lower()
    assert "dismiss" in out


def test_filter_cli_runs_on_seeded_db(tmp_path, capsys):
    dbp = tmp_path / "d.db"
    assert main(["--db", str(dbp), "init-db"]) == 0
    assert main(["--db", str(dbp), "ingest", "--file", str(FIXTURE),
                 "--feed-category", "expired",
                 "--criteria", str(REPO_ROOT / "criteria.toml")]) == 0
    capsys.readouterr()
    rc = main(["--db", str(dbp), "filter", "--criteria", str(REPO_ROOT / "criteria.toml")])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "processed" in out and "passed" in out
    conn = sqlite3.connect(dbp)
    n = conn.execute("SELECT COUNT(*) FROM candidates WHERE filtered_at IS NOT NULL").fetchone()[0]
    assert n == 6  # all six landed candidates got filtered


def test_build_ngrams_cli_writes_sorted_json(tmp_path):
    out = tmp_path / "t.json"
    rc = main(["build-ngrams", "--top-n", "5000", "--out", str(out)])
    assert rc == 0
    assert out.exists()
    import json
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "trigram_counts" in data and data["_meta"]["top_n"] == 5000


def test_module_entrypoint_runs(tmp_path):
    dbp = tmp_path / "e.db"
    result = subprocess.run(
        [sys.executable, "-m", "domainscout", "--db", str(dbp), "init-db"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert dbp.exists()
