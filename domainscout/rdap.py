"""RDAP verification: async whodap fetch + normalization + orchestration.
Pure lifecycle/drop-date logic lives in lifecycle.py; this module owns all I/O.
See docs/PHASE-4-DESIGN.md."""

from __future__ import annotations

import asyncio
import json
import ssl
from datetime import date, datetime

import httpx
import truststore
from whodap import DNSClient, DomainResponse
from whodap.errors import NotFoundError

from domainscout import db, doh as doh_mod, lifecycle
from domainscout.models import LifecycleUpdate, RdapObservation, VerifyCounts
from domainscout.ratelimit import TokenBucket, with_backoff


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


def _iso(d) -> "str | None":
    return d.isoformat() if d is not None else None


def _tally(counts: VerifyCounts, status: str) -> None:
    if hasattr(counts, status):
        setattr(counts, status, getattr(counts, status) + 1)


async def verify_candidates(conn, criteria, *, limit, recheck_all, dry_run, now,
                            lookup, doh) -> VerifyCounts:
    """Orchestrate verification over due rows using INJECTED lookup/doh callables (network-free
    in tests). lookup(label)->RdapObservation; doh(domain)->dns_status. Writes via set_rdap_result."""
    counts = VerifyCounts()
    due = select_due(conn, criteria, now, recheck_all)
    batch = due[:limit] if limit else due
    counts.left_for_next_run = len(due) - len(batch)
    today = now.date()
    now_iso = now.isoformat(timespec="seconds")

    async def handle(row):
        try:
            label = row["domain"][:-4]  # strip ".com"
            obs = await lookup(label)
            dns = await doh(row["domain"])
            upd = lifecycle.next_state(row["lifecycle_status"], obs, today)
            if not dry_run:
                db.set_rdap_result(
                    conn, row["id"], lifecycle_status=upd.lifecycle_status, rdap_status=obs.status_json,
                    expiry_date=_iso(upd.expiry_date), drop_date_est=_iso(upd.drop_date_est),
                    drop_date_actual=_iso(upd.drop_date_actual), dns_status=dns, verified_at=now_iso,
                )
            counts.processed += 1
            _tally(counts, upd.lifecycle_status)
            for s in lifecycle.unmatched_statuses(obs):
                counts.unmatched[s] = counts.unmatched.get(s, 0) + 1
        except Exception:
            counts.errors += 1

    await asyncio.gather(*(handle(r) for r in batch))
    return counts


async def run_verify(conn, criteria, *, limit, recheck_all, dry_run, now=None) -> VerifyCounts:
    """Real network entry: build the truststore client + a bounded DNSClient pool + rate limiter,
    then delegate to verify_candidates. The pool (size = rdap_concurrency) is the concurrency bound;
    the TokenBucket paces to rdap_max_rps; with_backoff retries transient failures."""
    now = now or datetime.now()
    http_client = make_async_client(criteria)
    pool: asyncio.Queue = asyncio.Queue()
    for _ in range(criteria.rdap_concurrency):
        pool.put_nowait(_new_dns_client(http_client, criteria.rdap_endpoint))
    bucket = TokenBucket(criteria.rdap_max_rps)

    async def real_lookup(label):
        await bucket.acquire()
        client = await pool.get()
        try:
            return await with_backoff(lambda: lookup_one(client, label),
                                      retries=criteria.rdap_max_retries)
        finally:
            pool.put_nowait(client)

    async def real_doh(domain):
        return await doh_mod.probe(http_client, domain)

    try:
        return await verify_candidates(conn, criteria, limit=limit, recheck_all=recheck_all,
                                       dry_run=dry_run, now=now, lookup=real_lookup, doh=real_doh)
    finally:
        await http_client.aclose()


async def verify_single(criteria, name, *, conn=None, dry_run=False, now=None):
    """Single-domain debug path (--domain). Live lookup + DoH; writes ONLY when the name has an
    OPEN row and not dry_run — never onto a closed cycle. Returns (obs, update, dns, wrote)."""
    now = now or datetime.now()
    label = name[:-4] if name.endswith(".com") else name
    domain = f"{label}.com"
    http_client = make_async_client(criteria)
    try:
        client = _new_dns_client(http_client, criteria.rdap_endpoint)
        obs = await with_backoff(lambda: lookup_one(client, label),
                                 retries=criteria.rdap_max_retries)
        dns = await doh_mod.probe(http_client, domain)
    finally:
        await http_client.aclose()
    row = None
    if conn is not None:
        row = conn.execute(
            "SELECT id, lifecycle_status FROM candidates "
            "WHERE domain=? AND lifecycle_status NOT IN ('renewed','reregistered','dismissed')",
            (domain,)).fetchone()
    current = row["lifecycle_status"] if row else "unknown"
    upd = lifecycle.next_state(current, obs, now.date())
    wrote = False
    if row is not None and not dry_run:
        db.set_rdap_result(
            conn, row["id"], lifecycle_status=upd.lifecycle_status, rdap_status=obs.status_json,
            expiry_date=_iso(upd.expiry_date), drop_date_est=_iso(upd.drop_date_est),
            drop_date_actual=_iso(upd.drop_date_actual), dns_status=dns,
            verified_at=now.isoformat(timespec="seconds"))
        wrote = True
    return obs, upd, dns, wrote
