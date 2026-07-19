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


class _FakeScreenClient:
    """Stands in for the httpx.Client `ingest.make_client` would return, so tests
    that walk past the dry-run branch never touch the network."""

    def close(self):
        pass


def _no_network_screen_env(monkeypatch):
    """Shared setup for tests that must reach past dry-run without any real network:
    a fake GSB key (so GsbClient.from_env succeeds), a stubbed .env loader (so a real
    .env file, if one ever exists, cannot interfere), and a fake http client."""
    monkeypatch.setenv("GOOGLE_SAFE_BROWSING_API_KEY", "fake-key-for-test")
    monkeypatch.setattr("domainscout.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr("domainscout.ingest.make_client", lambda *a, **k: _FakeScreenClient())


def test_screen_no_domain_and_no_domains_exits_1_cleanly(capsys):
    """Finding 1: neither flag is `required`, so args.domain is None by default. Before
    the fix this fell through to `[d.strip() for d in [None] if d.strip()]` -> a raw
    AttributeError traceback. Must instead be a clean one-liner naming both flags."""
    args = build_parser().parse_args(["screen"])
    rc = args.func(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "--domain" in err and "--domains" in err
    assert "Traceback" not in err
    assert "AttributeError" not in err


def test_screen_domains_empty_string_exits_1_cleanly(capsys):
    """Same failure class as above, reached via `--domains ""` instead of omission."""
    args = build_parser().parse_args(["screen", "--domains", ""])
    rc = args.func(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "--domain" in err and "--domains" in err
    assert "Traceback" not in err


def test_screen_domain_and_domains_are_additive_and_deduped(monkeypatch):
    """Finding 2: both flags together must screen the UNION, --domain first, then
    --domains in order, with duplicates collapsed to their first occurrence."""
    _no_network_screen_env(monkeypatch)
    captured = {}

    def fake_screen(domains, *, cdx, gsb, criteria, cache=None):
        captured["domains"] = list(domains)
        return []

    monkeypatch.setattr("domainscout.toxicity.screen", fake_screen)
    args = build_parser().parse_args([
        "screen", "--domain", "a.com", "--domains", "b.com,a.com,c.com", "--no-cache",
    ])
    rc = args.func(args)
    assert rc == 0
    assert captured["domains"] == ["a.com", "b.com", "c.com"]


def test_screen_domains_handles_empty_elements_and_trailing_commas(monkeypatch):
    """Finding 2: `--domains "a.com,,b.com,"` must yield exactly two domains, no crash."""
    _no_network_screen_env(monkeypatch)
    captured = {}

    def fake_screen(domains, *, cdx, gsb, criteria, cache=None):
        captured["domains"] = list(domains)
        return []

    monkeypatch.setattr("domainscout.toxicity.screen", fake_screen)
    args = build_parser().parse_args(
        ["screen", "--domains", "a.com,,b.com,", "--no-cache"])
    rc = args.func(args)
    assert rc == 0
    assert captured["domains"] == ["a.com", "b.com"]


def test_screen_dry_run_reports_correct_chunk_count_across_multiple_chunks(monkeypatch, capsys):
    """Finding 3: the dry-run message hardcoded 'in 1 batch', true only up to
    tox_gsb_batch_size // 2 domains. This shrinks the batch size via the same criteria
    object cmd_screen loads (monkeypatching commands.load_criteria, reachable from the
    test) so 5 domains genuinely span 3 chunks of 2, and asserts the real count."""
    real_load_criteria = commands.load_criteria

    def shrunk(path):
        from dataclasses import replace
        return replace(real_load_criteria(path), tox_gsb_batch_size=4)   # chunk size = 2

    monkeypatch.setattr(commands, "load_criteria", shrunk)
    domains = ",".join(f"d{i}.com" for i in range(5))   # 5 domains, chunk=2 -> 3 chunks
    args = build_parser().parse_args(["screen", "--domains", domains, "--dry-run"])
    rc = args.func(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "5 domain(s)" in out
    assert "3 batches" in out


def test_screen_human_output_covers_every_print_branch_ascii_safe(monkeypatch, capsys):
    """Finding 4: test_screen_output_is_ascii_only only drives --dry-run, so the
    non-dry-run print branches (safe-browsing line, lifetime line, divergence line,
    no-history line) have NO cp1252 regression guard -- capsys is UTF-8 and would not
    catch the class of bug a previous phase actually shipped. This drives cmd_screen's
    full human-readable path with hand-built verdicts covering all four branches."""
    from domainscout import models
    from domainscout import toxicity as toxicity_mod

    _no_network_screen_env(monkeypatch)

    lifetime = models.ShapeBlock("20100101000000", "20200101000000", 10.0, 20, 10,
                                 1.0, 0.5, 2.0, {"2xx": 20}, {"text/html": 20})
    tail = models.ShapeBlock("20190101000000", "20200101000000", 1.0, 5, 1,
                             1.0, 0.9, 5.0, {"2xx": 2, "3xx": 3}, {"text/html": 5})
    divergence = models.Divergence(churn_ratio=1.8, status_shift=-0.35, mime_shift=0.1,
                                   captures_per_year_ratio=2.5)
    history_with_divergence = models.HistoryShape(lifetime=lifetime, tail=tail,
                                                   divergence=divergence)
    history_no_divergence = models.HistoryShape(lifetime=lifetime, tail=None, divergence=None)
    gsb_hit = models.GsbResult(True, ("MALWARE",), "2026-07-18T00:00:00")

    verdicts = [
        models.ToxicityVerdict(
            domain="a.com", verdict=models.VERDICT_REJECT,
            reason="safe-browsing listed: MALWARE", gsb=gsb_hit,
            history=history_with_divergence, screened_at="2026-07-18T00:00:00",
            collapse="timestamp:6"),
        models.ToxicityVerdict(
            domain="b.com", verdict=models.VERDICT_UNKNOWN_NO_HISTORY,
            reason="no wayback captures", gsb=None, history=None,
            screened_at="2026-07-18T00:00:00", collapse="timestamp:6"),
        models.ToxicityVerdict(
            domain="c.com", verdict=models.VERDICT_PASS,
            reason="not currently listed; history shape recorded", gsb=None,
            history=history_no_divergence, screened_at="2026-07-18T00:00:00",
            collapse="timestamp:6"),
    ]

    def fake_screen(domains, *, cdx, gsb, criteria, cache=None):
        return verdicts

    monkeypatch.setattr(toxicity_mod, "screen", fake_screen)

    args = build_parser().parse_args(
        ["screen", "--domains", "a.com,b.com,c.com", "--no-cache"])
    rc = args.func(args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "safe-browsing currently_listed" in out
    assert "tail divergence: churn_ratio" in out
    assert "tail divergence: n/a" in out
    assert "no wayback captures" in out
    out.encode("cp1252")   # must NOT raise
