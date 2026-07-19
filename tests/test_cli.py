import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from domainscout import commands
from domainscout.__main__ import build_parser, main

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
    rc = main(["digest"])          # digest is still a Phase-7 stub
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "not implemented" in out
    assert "phase 7" in out


FIXTURE = REPO_ROOT / "tests" / "fixtures" / "whoisfreaks-sample.csv"


def test_verify_cli_empty_db_prints_summary(tmp_path, capsys):
    dbp = tmp_path / "d.db"
    assert main(["--db", str(dbp), "init-db"]) == 0
    capsys.readouterr()
    rc = main(["--db", str(dbp), "verify", "--criteria", str(REPO_ROOT / "criteria.toml")])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "processed=0" in out   # no due rows -> no network


def test_verify_cli_dry_run_on_unfiltered_rows_is_network_free(tmp_path, capsys):
    # rows exist but filter_pass is unset -> select_due excludes them -> no network
    dbp = tmp_path / "d.db"
    assert main(["--db", str(dbp), "init-db"]) == 0
    assert main(["--db", str(dbp), "ingest", "--file", str(FIXTURE),
                 "--feed-category", "expired",
                 "--criteria", str(REPO_ROOT / "criteria.toml")]) == 0
    capsys.readouterr()
    rc = main(["--db", str(dbp), "verify", "--dry-run",
               "--criteria", str(REPO_ROOT / "criteria.toml")])
    assert rc == 0
    assert "processed=0" in capsys.readouterr().out.lower()


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


def test_comps_subcommands_are_no_longer_stubs():
    from domainscout.__main__ import build_parser
    parser = build_parser()
    args = parser.parse_args(["comps-refresh", "--force"])
    assert args.func.__name__ == "cmd_comps_refresh"
    assert args.force is True
    args2 = parser.parse_args(["comps", "--domain", "cloudvault.com"])
    assert args2.func.__name__ == "cmd_comps"
    assert args2.domain == "cloudvault.com"


def test_comps_refresh_dry_run_writes_nothing(tmp_path, monkeypatch):
    """--dry-run must not open a network client, download, or write any cache/sidecar."""
    from domainscout import commands

    def boom(*a, **k):
        raise AssertionError("comps-refresh --dry-run must not open a network client")

    monkeypatch.setattr("domainscout.ingest.make_client", boom)

    class A:
        criteria = str(REPO_ROOT / "criteria.toml")
        force = False
        dry_run = True
        data_dir = str(tmp_path)

    assert commands.cmd_comps_refresh(A()) == 0
    assert list(tmp_path.iterdir()) == []   # nothing written


def test_comps_domain_missing_cache_is_clean_error(tmp_path, capsys):
    """A missing cache must be a one-line helpful error + nonzero exit, NOT a traceback."""
    from domainscout import commands

    class A:
        criteria = str(REPO_ROOT / "criteria.toml")
        domain = "cloudvault.com"
        data_dir = str(tmp_path)   # empty dir -> no cache, no .prev

    rc = commands.cmd_comps(A())
    assert rc == 1
    err = capsys.readouterr().err
    assert "comps-refresh" in err   # the helpful remediation, not a stack trace


def test_cmd_comps_makes_no_network_calls(tmp_path, capsys, monkeypatch):
    """`comps --domain` is LOCAL ONLY - it must never be able to poison a refresh."""
    import shutil
    from pathlib import Path as _P

    from domainscout import commands, comps

    fx = _P(__file__).resolve().parent / "fixtures"
    shutil.copy(fx / "namebio_retailstats_small.csv", tmp_path / "namebio_retailstats.csv")
    shutil.copy(fx / "namebio_tldstats_small.csv", tmp_path / "namebio_tldstats.csv")
    comps.write_meta(tmp_path, {"retailstats": {"retrieved": "2026-07-16T10:00:00", "rows": 5}})

    def boom(*a, **k):
        raise AssertionError("comps --domain must not touch the network")

    monkeypatch.setattr("domainscout.ingest.make_client", boom)

    class A:
        # Absolute path: a bare "criteria.toml" is cwd-fragile (a Phase-4 review
        # already flagged that pattern in tests/test_config.py).
        criteria = str(_P(__file__).resolve().parents[1] / "criteria.toml")
        domain = "cloudvault.com"
        data_dir = str(tmp_path)

    assert commands.cmd_comps(A()) == 0
    out = capsys.readouterr().out
    assert "cloud" in out and "start" in out
    assert "cache:" in out


def test_stale_warning_is_cron_log_safe_encoding(tmp_path, capsys):
    """The stale warning must survive REDIRECTED stdout on Windows (cp1252) — Task Scheduler/cron
    redirect to a file, where a non-ASCII marker raises UnicodeEncodeError and breaks the exit-0
    contract in exactly the stale-cache case this feature exists to surface. capsys is UTF-8, so we
    assert cp1252-encodability explicitly."""
    from datetime import datetime, timedelta
    from domainscout import commands, comps
    from domainscout.config import load_criteria

    crit = load_criteria(REPO_ROOT / "criteria.toml")
    old = (datetime.now() - timedelta(days=90)).isoformat()   # >> stale_warn_factor*refresh_days (21d)
    comps.write_meta(tmp_path, {"retailstats": {"retrieved": old, "rows": 97568}})
    commands._warn_if_stale(crit, tmp_path)
    out = capsys.readouterr().out
    assert "STALE" in out and "retailstats" in out            # it actually warned
    out.encode("cp1252")                                       # must NOT raise


def test_screen_is_a_real_subcommand_not_a_stub():
    parser = build_parser()
    args = parser.parse_args(["screen", "--domain", "a.com"])
    assert args.func is commands.cmd_screen


def test_screen_without_api_key_exits_1_cleanly(monkeypatch, capsys, tmp_path):
    """A missing key must be a readable message and exit 1, never a raw traceback
    (5a's CompsCacheMissing precedent)."""
    monkeypatch.delenv("GOOGLE_SAFE_BROWSING_API_KEY", raising=False)
    monkeypatch.setattr("domainscout.config.load_dotenv", lambda *a, **k: None)
    args = build_parser().parse_args(
        ["screen", "--domain", "a.com", "--cache-path", str(tmp_path / "c.json")])
    assert args.func(args) == 1
    assert "GOOGLE_SAFE_BROWSING_API_KEY" in capsys.readouterr().err


def test_screen_dry_run_makes_no_network_calls(monkeypatch, capsys):
    def explode(*a, **k):
        raise AssertionError("dry-run must not build a network client")

    monkeypatch.setattr("domainscout.ingest.make_client", explode)
    args = build_parser().parse_args(["screen", "--domain", "a.com", "--dry-run"])
    assert args.func(args) == 0
    assert "dry-run" in capsys.readouterr().out


def test_screen_output_is_ascii_only(monkeypatch, capsys):
    """5a shipped one emoji that crashed the cron path on redirected cp1252 stdout."""
    args = build_parser().parse_args(["screen", "--domain", "a.com", "--dry-run"])
    args.func(args)
    capsys.readouterr().out.encode("cp1252")   # must not raise
