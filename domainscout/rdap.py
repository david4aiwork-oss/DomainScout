"""RDAP verification: async whodap fetch + normalization + orchestration.
Pure lifecycle/drop-date logic lives in lifecycle.py; this module owns all I/O.
See docs/PHASE-4-DESIGN.md."""

from __future__ import annotations

import json
import ssl
from datetime import date, datetime

import httpx
import truststore
from whodap import DNSClient, DomainResponse
from whodap.errors import NotFoundError

from domainscout import db, lifecycle
from domainscout.models import RdapObservation


def parse_observation(resp: "DomainResponse | None") -> RdapObservation:
    """Normalize a whodap DomainResponse (or None for a 404) into an RdapObservation."""
    if resp is None:
        return RdapObservation(available=True, status=(), events={}, expiry_date=None, status_json="[]")
    status = tuple((s or "").lower() for s in (resp.status or []))
    events: dict[str, date] = {}
    for e in (resp.events or []):
        action = (getattr(e, "eventAction", "") or "").lower()
        d = getattr(e, "eventDate", None)
        if isinstance(d, datetime):
            d = d.date()
        if action:
            events[action] = d
    return RdapObservation(
        available=False, status=status, events=events,
        expiry_date=events.get("expiration"), status_json=json.dumps(list(status)),
    )


def make_async_client(criteria) -> httpx.AsyncClient:
    """Async truststore client (async twin of ingest.make_client): verify TLS against the OS
    trust store so the dev-box AV/proxy MITM root CA is honored. Portable to a Linux VPS."""
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return httpx.AsyncClient(
        verify=ctx, follow_redirects=True, timeout=criteria.rdap_timeout,
        headers={"User-Agent": criteria.rdap_user_agent},
    )


def _new_dns_client(http_client: httpx.AsyncClient, endpoint: str) -> DNSClient:
    """Construct a whodap DNSClient directly and preset the .com endpoint — skips the IANA
    bootstrap network call. One instance per concurrent worker (DNSClient is stateful)."""
    client = DNSClient(http_client)
    client.iana_dns_server_map = {"com": endpoint}
    return client


async def lookup_one(dns_client: DNSClient, label: str) -> RdapObservation:
    """One RDAP lookup for '<label>.com'. NotFoundError (404) -> available. Other whodap/httpx
    errors propagate to the caller's backoff/error handling."""
    try:
        resp = await dns_client.aio_lookup(label, "com")
    except NotFoundError:
        resp = None
    return parse_observation(resp)


_SELECT_DUE_SQL = """
SELECT id, domain, feed_category, lifecycle_status, drop_date_actual, verified_at
FROM candidates
WHERE lifecycle_status NOT IN ('renewed','reregistered','dismissed')
  AND filter_pass = 1
ORDER BY (feed_category = 'dropped') DESC,
         (drop_date_est IS NULL), drop_date_est ASC,
         (verified_at IS NULL) DESC, verified_at ASC
"""


def select_due(conn, criteria, now: datetime, recheck_all: bool) -> list:
    """Open + filter_pass rows to verify this run, in priority order (dropped-feed first, then
    soonest-drop, then stalest). Cadence-filtered unless recheck_all. No LIMIT — caller slices."""
    rows = conn.execute(_SELECT_DUE_SQL).fetchall()
    if recheck_all:
        return list(rows)
    due = []
    for r in rows:
        va = r["verified_at"]
        va_dt = datetime.fromisoformat(va) if va else None
        if lifecycle._is_due(r["lifecycle_status"], va_dt, now, criteria.rdap_recheck_days):
            due.append(r)
    return due
