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
