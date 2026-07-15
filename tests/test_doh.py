import asyncio

from domainscout import doh


class FakeResp:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, payload=None, exc=None):
        self._payload = payload
        self._exc = exc
        self.last_params = None
    async def get(self, url, params=None, headers=None):
        self.last_params = params
        if self._exc:
            raise self._exc
        return FakeResp(self._payload)


def test_probe_noerror():
    c = FakeClient(payload={"Status": 0})
    assert asyncio.run(doh.probe(c, "example.com")) == "noerror"
    assert c.last_params["name"] == "example.com" and c.last_params["type"] == "A"


def test_probe_nxdomain():
    c = FakeClient(payload={"Status": 3})
    assert asyncio.run(doh.probe(c, "gone.com")) == "nxdomain"


def test_probe_servfail():
    c = FakeClient(payload={"Status": 2})
    assert asyncio.run(doh.probe(c, "x.com")) == "servfail"


def test_probe_swallows_exceptions_to_error():
    c = FakeClient(exc=RuntimeError("boom"))
    assert asyncio.run(doh.probe(c, "x.com")) == "error"


def test_probe_unknown_status_is_error():
    c = FakeClient(payload={"Status": 9})
    assert asyncio.run(doh.probe(c, "x.com")) == "error"
