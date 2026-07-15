import asyncio
import json
from datetime import datetime
from pathlib import Path

from whodap import DomainResponse
from whodap.errors import NotFoundError

from domainscout import db, rdap
from domainscout.config import load_criteria
from domainscout.models import Candidate

REPO_ROOT = Path(__file__).resolve().parents[1]
CRIT = load_criteria(REPO_ROOT / "criteria.toml")


def _resp(status, events):
    payload = {
        "objectClassName": "domain", "ldhName": "EXAMPLE.COM",
        "status": list(status),
        "events": [{"eventAction": a, "eventDate": d} for a, d in events],
    }
    return DomainResponse.from_json(json.dumps(payload).encode())


def test_parse_observation_none_is_available():
    obs = rdap.parse_observation(None)
    assert obs.available is True
    assert obs.status == () and obs.status_json == "[]"


def test_parse_observation_lowercases_status_and_extracts_expiry():
    resp = _resp(
        ["Redemption Period", "Client Transfer Prohibited"],
        [("registration", "1998-01-01T00:00:00Z"), ("expiration", "2026-06-01T00:00:00Z")],
    )
    obs = rdap.parse_observation(resp)
    assert obs.available is False
    assert "redemption period" in obs.status
    assert obs.expiry_date.isoformat() == "2026-06-01"
    assert json.loads(obs.status_json) == ["redemption period", "client transfer prohibited"]


def test_lookup_one_available_on_notfound():
    class FakeClient:
        async def aio_lookup(self, label, tld):
            raise NotFoundError("404")
    obs = asyncio.run(rdap.lookup_one(FakeClient(), "example"))
    assert obs.available is True


def test_lookup_one_parses_registered():
    resp = _resp(["Active"], [("expiration", "2027-06-01T00:00:00Z")])

    class FakeClient:
        async def aio_lookup(self, label, tld):
            assert (label, tld) == ("example", "com")
            return resp
    obs = asyncio.run(rdap.lookup_one(FakeClient(), "example"))
    assert obs.available is False and "active" in obs.status


def test_new_dns_client_presets_com_map():
    import httpx
    client = rdap._new_dns_client(httpx.AsyncClient(), "https://rdap.verisign.com/com/v1/")
    assert client.iana_dns_server_map == {"com": "https://rdap.verisign.com/com/v1/"}


def _seed(conn, domain, *, feed_category="expired", filter_pass=1,
          lifecycle_status="unknown", verified_at=None, drop_date_est=None):
    cid = db.upsert_candidate(conn, Candidate(domain=domain, source="whoisfreaks",
                                              feed_category=feed_category))
    conn.execute(
        "UPDATE candidates SET filter_pass=?, lifecycle_status=?, verified_at=?, drop_date_est=? WHERE id=?",
        (filter_pass, lifecycle_status, verified_at, drop_date_est, cid))
    conn.commit()
    return cid


def _due_domains(conn, recheck_all=False, now=datetime(2026, 7, 15, 12, 0, 0)):
    return [r["domain"] for r in rdap.select_due(conn, CRIT, now, recheck_all)]


def test_select_due_excludes_closed_and_unfiltered(tmp_path):
    dbp = tmp_path / "d.db"; db.init_db(dbp); conn = db.connect(dbp)
    _seed(conn, "open.com", filter_pass=1)
    _seed(conn, "closed.com", filter_pass=1, lifecycle_status="renewed")
    _seed(conn, "unfiltered.com", filter_pass=0)
    assert _due_domains(conn) == ["open.com"]


def test_select_due_orders_dropped_feed_first(tmp_path):
    dbp = tmp_path / "d.db"; db.init_db(dbp); conn = db.connect(dbp)
    # both never-verified, both NULL drop_date_est -> only feed_category breaks the tie
    _seed(conn, "expired.com", feed_category="expired")
    _seed(conn, "dropped.com", feed_category="dropped")
    assert _due_domains(conn)[0] == "dropped.com"


def test_select_due_applies_cadence(tmp_path):
    dbp = tmp_path / "d.db"; db.init_db(dbp); conn = db.connect(dbp)
    # redemption verified 1 day ago, cadence 2 -> NOT due
    _seed(conn, "fresh.com", lifecycle_status="redemption", verified_at="2026-07-14T12:00:00")
    # redemption verified 3 days ago -> due
    _seed(conn, "stale.com", lifecycle_status="redemption", verified_at="2026-07-12T12:00:00")
    assert _due_domains(conn) == ["stale.com"]


def test_select_due_recheck_all_ignores_cadence(tmp_path):
    dbp = tmp_path / "d.db"; db.init_db(dbp); conn = db.connect(dbp)
    _seed(conn, "fresh.com", lifecycle_status="redemption", verified_at="2026-07-14T12:00:00")
    assert _due_domains(conn, recheck_all=True) == ["fresh.com"]


import json as _json

from domainscout.models import RdapObservation, VerifyCounts


def _mk_obs(status=(), available=False, expiry=None):
    return RdapObservation(available=available, status=tuple(status), events={},
                           expiry_date=expiry, status_json=_json.dumps(list(status)))


def _run_verify(conn, mapping, *, limit=1000, dry_run=False, recheck_all=False,
                now=datetime(2026, 7, 15, 12, 0, 0), doh_result="nxdomain"):
    async def fake_lookup(label):
        return mapping[label]
    async def fake_doh(domain):
        return doh_result
    return asyncio.run(rdap.verify_candidates(
        conn, CRIT, limit=limit, recheck_all=recheck_all, dry_run=dry_run,
        now=now, lookup=fake_lookup, doh=fake_doh))


def test_verify_candidates_writes_rows(tmp_path):
    dbp = tmp_path / "d.db"; db.init_db(dbp); conn = db.connect(dbp)
    cid = _seed(conn, "gone.com", feed_category="dropped")
    counts = _run_verify(conn, {"gone": _mk_obs(available=True)})
    assert counts.processed == 1 and counts.dropped == 1
    row = conn.execute("SELECT lifecycle_status, drop_date_actual, dns_status FROM candidates WHERE id=?",
                       (cid,)).fetchone()
    assert row["lifecycle_status"] == "dropped"
    assert row["drop_date_actual"] == "2026-07-15"
    assert row["dns_status"] == "nxdomain"


def test_verify_candidates_dry_run_writes_nothing(tmp_path):
    dbp = tmp_path / "d.db"; db.init_db(dbp); conn = db.connect(dbp)
    _seed(conn, "gone.com")
    counts = _run_verify(conn, {"gone": _mk_obs(available=True)}, dry_run=True)
    assert counts.processed == 1
    row = conn.execute("SELECT lifecycle_status, verified_at FROM candidates WHERE domain='gone.com'").fetchone()
    assert row["lifecycle_status"] == "unknown" and row["verified_at"] is None


def test_verify_candidates_tallies_and_counts_unmatched(tmp_path):
    dbp = tmp_path / "d.db"; db.init_db(dbp); conn = db.connect(dbp)
    _seed(conn, "a.com"); _seed(conn, "b.com")
    counts = _run_verify(conn, {
        "a": _mk_obs(status=("redemption period", "surprise status")),
        "b": _mk_obs(status=("pending delete",)),
    })
    assert counts.redemption == 1 and counts.pending_delete == 1
    assert counts.unmatched == {"surprise status": 1}


def test_verify_candidates_error_bucket_does_not_abort(tmp_path):
    dbp = tmp_path / "d.db"; db.init_db(dbp); conn = db.connect(dbp)
    _seed(conn, "ok.com"); _seed(conn, "boom.com")

    async def fake_lookup(label):
        if label == "boom":
            raise RuntimeError("network gone")
        return _mk_obs(available=True)
    async def fake_doh(domain):
        return "error"
    counts = asyncio.run(rdap.verify_candidates(
        conn, CRIT, limit=1000, recheck_all=False, dry_run=False,
        now=datetime(2026, 7, 15, 12, 0, 0), lookup=fake_lookup, doh=fake_doh))
    assert counts.errors == 1 and counts.dropped == 1  # ok.com still processed


def test_verify_candidates_limit_sets_left_for_next_run(tmp_path):
    dbp = tmp_path / "d.db"; db.init_db(dbp); conn = db.connect(dbp)
    for i in range(3):
        _seed(conn, f"d{i}.com")
    counts = _run_verify(conn, {f"d{i}": _mk_obs(available=True) for i in range(3)}, limit=2)
    assert counts.processed == 2 and counts.left_for_next_run == 1


def test_verify_candidates_idempotent_within_cadence(tmp_path):
    dbp = tmp_path / "d.db"; db.init_db(dbp); conn = db.connect(dbp)
    _seed(conn, "r.com")
    now1 = datetime(2026, 7, 15, 12, 0, 0)
    asyncio.run(rdap.verify_candidates(conn, CRIT, limit=1000, recheck_all=False, dry_run=False,
        now=now1, lookup=_const_lookup(_mk_obs(status=("redemption period",))), doh=_const_doh()))
    # 1 day later, redemption cadence is 2 days -> not due -> processes nothing
    now2 = datetime(2026, 7, 16, 12, 0, 0)
    counts = asyncio.run(rdap.verify_candidates(conn, CRIT, limit=1000, recheck_all=False, dry_run=False,
        now=now2, lookup=_const_lookup(_mk_obs(available=True)), doh=_const_doh()))
    assert counts.processed == 0
    row = conn.execute("SELECT lifecycle_status FROM candidates WHERE domain='r.com'").fetchone()
    assert row["lifecycle_status"] == "redemption"  # unchanged by the skipped second run


def _const_lookup(obs):
    async def _f(label):
        return obs
    return _f


def _const_doh(result="nxdomain"):
    async def _f(domain):
        return result
    return _f


import pytest


@pytest.mark.skip(reason="live network — run manually against Verisign RDAP")
def test_live_smoke_known_registered_and_available():
    from domainscout.config import load_criteria
    crit = load_criteria(REPO_ROOT / "criteria.toml")
    reg, _, _, _ = asyncio.run(rdap.verify_single(crit, "google.com"))
    assert reg.available is False
    # a random unlikely-registered label should 404 -> available
    gone, _, _, _ = asyncio.run(rdap.verify_single(crit, "qzxkvbnmplkjhg.com"))
    assert gone.available is True
