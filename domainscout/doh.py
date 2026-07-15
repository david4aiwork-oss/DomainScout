"""DNS-over-HTTPS resolution probe (Cloudflare JSON API). RECORDED SIGNAL ONLY — the result
never gates an RDAP call or influences lifecycle_status (NXDOMAIN != available for .com; a
redemption/pendingDelete domain is removed from the zone yet still registered)."""

from __future__ import annotations

DOH_URL = "https://cloudflare-dns.com/dns-query"
_STATUS = {0: "noerror", 3: "nxdomain", 2: "servfail"}


async def probe(http_client, domain: str) -> str:
    """Return 'noerror' | 'nxdomain' | 'servfail' | 'error'. Never raises (errors -> 'error')."""
    try:
        resp = await http_client.get(
            DOH_URL, params={"name": domain, "type": "A"},
            headers={"Accept": "application/dns-json"},
        )
        resp.raise_for_status()
        return _STATUS.get(resp.json().get("Status"), "error")
    except Exception:
        return "error"
