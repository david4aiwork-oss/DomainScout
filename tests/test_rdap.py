import asyncio
import json

from whodap import DomainResponse
from whodap.errors import NotFoundError

from domainscout import rdap


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
