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
    rc = main(["ingest"])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "not implemented" in out
    assert "phase 2" in out


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


def test_module_entrypoint_runs(tmp_path):
    dbp = tmp_path / "e.db"
    result = subprocess.run(
        [sys.executable, "-m", "domainscout", "--db", str(dbp), "init-db"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert dbp.exists()
