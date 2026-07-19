"""Phase 5b: the toxicity gate.

A library, NOT a pipeline stage: the gate runs between Tier-1 and Tier-2, and
Tier-1 - which decides who is worth screening - does not exist until 5c. 5c calls
screen() on its Tier-1 survivors, exactly as it calls comps.lookup().

Network lives ONLY in CdxClient and GsbClient; both are injected, so the suite
makes zero network calls. Read docs/PHASE-5B-DESIGN.md before touching the CDX
query strategy - the ordering/truncation behaviour there is measured, not assumed.
"""

from __future__ import annotations

import calendar
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

import httpx

from domainscout.models import (
    Capture, Divergence, GsbResult, HistoryShape, ShapeBlock, ToxicityVerdict,
    VERDICT_PASS, VERDICT_REJECT, VERDICT_UNKNOWN_ERROR, VERDICT_UNKNOWN_NO_HISTORY,
)


class CdxError(Exception):
    """Wayback CDX was unreachable or unparseable. Becomes unknown_error - NEVER a pass."""


class GsbError(Exception):
    """Safe Browsing failed. Becomes unknown_error - NEVER a pass."""


class ToxicityKeyMissing(Exception):
    """GOOGLE_SAFE_BROWSING_API_KEY is absent. Surfaced as a clean CLI message."""


def parse_cdx(payload: list) -> list[Capture]:
    """CDX json output is [header_row, *data_rows]. Columns are read BY NAME, because
    their order follows the fl= parameter. An empty list AND a header-only response
    both mean 'no captures' - a never-archived domain must never look like a failure."""
    if not payload:
        return []
    header, *rows = payload
    idx = {str(name): i for i, name in enumerate(header)}
    try:
        ts_i, st_i, mt_i, dg_i = (idx["timestamp"], idx["statuscode"],
                                  idx["mimetype"], idx["digest"])
    except KeyError as exc:
        raise CdxError(f"CDX response missing expected column {exc}") from exc
    out: list[Capture] = []
    for row in rows:
        if len(row) <= max(ts_i, st_i, mt_i, dg_i):
            continue
        out.append(Capture(timestamp=str(row[ts_i]), statuscode=str(row[st_i]),
                           mimetype=str(row[mt_i]), digest=str(row[dg_i])))
    return out


def bucket_monthly(captures: Iterable[Capture]) -> list[Capture]:
    """Collapse to one capture per calendar month over the WHOLE time-sorted series.

    CdxClient already asks the server to collapse, but it issues TWO queries per domain
    (apex + www.) and merges them - so the merged list is neither time-ordered nor free
    of duplicate months. This pass makes the sampling exact and the result independent
    of merge order. At ~600 merged rows it is free.

    Historical note (see docs/PHASE-5B-SPIKE.md): server-side collapse is only
    trustworthy because each query is matchType=exact, i.e. a single urlkey. Under
    matchType=domain, collapse acts on adjacent rows across THOUSANDS of urlkeys
    (cnn.com: 2,768), sampling per-URL-block and inflating digest_churn by reading URL
    diversity as content volatility."""
    seen: set[str] = set()
    out: list[Capture] = []
    for cap in sorted(captures, key=lambda c: c.timestamp):
        month = cap.timestamp[:6]
        if month in seen:
            continue
        seen.add(month)
        out.append(cap)
    return out


def _to_dt(timestamp: str) -> datetime:
    return datetime.strptime(timestamp[:14].ljust(14, "0"), "%Y%m%d%H%M%S")


def _months_before(moment: datetime, months: int) -> datetime:
    """Calendar-correct month subtraction without pulling in dateutil."""
    year, month = moment.year, moment.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def _status_bucket(code: str) -> str:
    return f"{code[0]}xx" if code[:1].isdigit() and code[0] in "2345" else "other"


def _block(captures: Sequence[Capture]) -> ShapeBlock:
    first, last = captures[0], captures[-1]
    span_days = (_to_dt(last.timestamp) - _to_dt(first.timestamp)).days
    span_years = span_days / 365.25
    status_mix: dict = {}
    mime_mix: dict = {}
    for cap in captures:
        bucket = _status_bucket(cap.statuscode)
        status_mix[bucket] = status_mix.get(bucket, 0) + 1
        mime_mix[cap.mimetype] = mime_mix.get(cap.mimetype, 0) + 1
    max_gap_days = 0
    for prev, nxt in zip(captures, captures[1:]):
        max_gap_days = max(max_gap_days,
                           (_to_dt(nxt.timestamp) - _to_dt(prev.timestamp)).days)
    return ShapeBlock(
        first_capture=first.timestamp,
        last_capture=last.timestamp,
        span_years=round(span_years, 3),
        capture_count=len(captures),
        distinct_years=len({c.timestamp[:4] for c in captures}),
        max_gap_years=round(max_gap_days / 365.25, 3),
        digest_churn=round(len({c.digest for c in captures}) / len(captures), 4),
        captures_per_year=round(len(captures) / max(span_years, 1 / 365.25), 3),
        status_mix=status_mix,
        mime_mix=mime_mix,
    )


def _proportion(mix: dict, total: int, *keys: str) -> float:
    return sum(mix.get(k, 0) for k in keys) / total if total else 0.0


def compute_shape(captures, *, tail_window_months: int,
                  tail_min_captures: int) -> HistoryShape | None:
    """None means NO captures - stable, informative absence, which decide() turns into
    unknown_no_history. It must never become a ShapeBlock of zeros, which would read
    downstream as 'we measured this domain and it scored badly'."""
    sampled = bucket_monthly(captures)
    if not sampled:
        return None
    lifetime = _block(sampled)

    cutoff = _months_before(_to_dt(sampled[-1].timestamp), tail_window_months)
    tail_caps = [c for c in sampled if _to_dt(c.timestamp) >= cutoff]

    # Too thin to support a ratio, or the tail IS the whole life (every ratio would be
    # 1.0 by construction - a meaningless 'no divergence' that reads as 'checked, fine').
    if len(tail_caps) < tail_min_captures or len(tail_caps) == len(sampled):
        return HistoryShape(lifetime=lifetime, tail=None, divergence=None)

    tail = _block(tail_caps)
    lt_total, t_total = lifetime.capture_count, tail.capture_count
    divergence = Divergence(
        churn_ratio=(round(tail.digest_churn / lifetime.digest_churn, 4)
                     if lifetime.digest_churn else None),
        status_shift=round(_proportion(tail.status_mix, t_total, "2xx")
                           - _proportion(lifetime.status_mix, lt_total, "2xx"), 4),
        mime_shift=round(_proportion(tail.mime_mix, t_total, "text/html")
                         - _proportion(lifetime.mime_mix, lt_total, "text/html"), 4),
        captures_per_year_ratio=(round(tail.captures_per_year / lifetime.captures_per_year, 4)
                                 if lifetime.captures_per_year else None),
    )
    return HistoryShape(lifetime=lifetime, tail=tail, divergence=divergence)


def decide(gsb: GsbResult | None, shape: HistoryShape | None,
           errors: Sequence[str]) -> tuple[str, str]:
    """Precedence, in order:

        gsb listed            -> reject               terminal; outranks everything,
                                                      including a failed CDX leg
        gsb or cdx errored    -> unknown_error        transient; retried next run
        cdx ok, 0 captures    -> unknown_no_history   stable absence
        otherwise             -> pass

    Every non-reject verdict PROCEEDS to Tier-2 carrying its reason. Failing closed on
    unknown would let one bad archive.org day silently empty the digest - a failure mode
    far harder to notice than a false positive."""
    if gsb is not None and gsb.currently_listed:
        return VERDICT_REJECT, "safe-browsing listed: " + ",".join(gsb.threat_types)
    if errors:
        return VERDICT_UNKNOWN_ERROR, "; ".join(errors)
    if shape is None:
        return (VERDICT_UNKNOWN_NO_HISTORY,
                "no wayback captures - absence of evidence, not evidence of anything")
    return VERDICT_PASS, "not currently listed; history shape recorded"


DEFAULT_CACHE_PATH = "data/toxicity_cache.json"


class VerdictCache:
    """Domain -> verdict, with per-verdict TTLs.

    Two rules carry this design:
      1. unknown_error is NEVER written. Not TTL-0 - never persisted, so a transient
         failure cannot be configured into stickiness.
      2. Every entry records the collapse it was computed under, and an entry whose
         collapse differs from the current config is a MISS. That makes 'thresholds
         are calibrated to this sampling' self-enforcing instead of a comment someone
         has to notice."""

    def __init__(self, path, *, cache_days: dict, collapse: str, now: datetime | None = None):
        self.path = Path(path)
        self.cache_days = cache_days
        self.collapse = collapse
        self.now = now or datetime.now()
        self._entries: dict = {}
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._entries = loaded
            except (OSError, ValueError):
                self._entries = {}   # a corrupt cache is a COLD cache, never a crash

    def get(self, domain: str) -> ToxicityVerdict | None:
        entry = self._entries.get(domain)
        if not isinstance(entry, dict):
            return None
        if entry.get("collapse") != self.collapse:
            return None
        ttl = self.cache_days.get(entry.get("verdict", ""))
        if ttl is None:
            return None
        try:
            screened = datetime.fromisoformat(entry["screened_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if (self.now - screened).days >= ttl:
            return None
        return ToxicityVerdict(
            domain=domain, verdict=entry["verdict"], reason=entry.get("reason", ""),
            gsb=None, history=None, screened_at=entry["screened_at"],
            collapse=entry["collapse"])

    def put(self, verdict: ToxicityVerdict) -> None:
        if verdict.verdict == VERDICT_UNKNOWN_ERROR:
            return   # see rule 1
        self._entries[verdict.domain] = {
            "verdict": verdict.verdict, "reason": verdict.reason,
            "screened_at": verdict.screened_at, "collapse": verdict.collapse,
        }

    def save(self) -> None:
        """Temp-file + os.replace. The OSError catch is not theoretical: 5a hit a real
        Windows AV file-lock during exactly this rename."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self._entries, indent=1, sort_keys=True),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            print(f"toxicity: WARNING - could not write cache {self.path}: {exc}")


class CdxClient:
    """Wayback CDX. TWO GETs per domain - the apex and the www. host - each
    matchType=exact with SERVER-side collapse, then merged. No batching exists.

    QUERY STRATEGY (measured in the Task-1 spike, see docs/PHASE-5B-SPIKE.md; do not
    change without re-measuring). Three alternatives were tested and all three FAILED:

      * matchType=domain, uncollapsed: unmanageable. The single-URL apex alone is
        72.2 MB / 1,042,676 rows; the domain-wide unbounded query did not finish in
        257 s+, and a read-timeout never fires because the server trickles bytes.
      * from=<date> bounding: truncates 6 days into a 2.5-year window. Rows stay
        urlkey-then-timestamp sorted regardless of the filter, so time-bounding does
        not defeat row-truncation.
      * negative limit: reaches recent timestamps but returns 100% ONE static asset
        (the alphabetically-last urlkey) - zero page-history signal.

    matchType=exact + server collapse works because a single urlkey makes adjacent-row
    collapse identical to monthly collapse, and because collapsing shrinks 1M+ rows to
    ~311 - far below any cap, so truncation never engages at all.

    Both hosts are queried because a domain whose apex merely 301s to www. would
    otherwise yield a shape computed from redirect history, and NOTHING would look
    wrong - a 301-only history reads as a thin but valid shape."""

    def __init__(self, client: httpx.Client, criteria, sleep=time.sleep):
        self.client = client
        self.criteria = criteria
        self.sleep = sleep   # injected so retry tests do not actually wait out the backoff

    def _params(self, host: str) -> dict:
        return {
            "url": host,
            "output": "json",
            "matchType": self.criteria.tox_cdx_match_type,   # 'exact' - one urlkey
            "collapse": self.criteria.tox_cdx_collapse,      # server-side; legit on one urlkey
            "fl": "timestamp,statuscode,mimetype,digest",
            "limit": self.criteria.tox_cdx_limit,            # runaway guard; never engages
        }

    def _get(self, params: dict) -> list:
        last: Exception | None = None
        for attempt in range(self.criteria.tox_cdx_max_retries):
            try:
                resp = self.client.get(self.criteria.tox_cdx_base_url, params=params,
                                       timeout=self.criteria.tox_cdx_timeout)
                if resp.status_code >= 500 or resp.status_code == 429:
                    last = CdxError(f"CDX HTTP {resp.status_code}")
                elif resp.status_code == 200:
                    return resp.json() or []
                else:
                    # Non-retryable: raise CdxError IMMEDIATELY, do not burn the retry
                    # budget or sleep. httpx.HTTPStatusError is a SIBLING of
                    # TransportError under HTTPError, not a subclass, so
                    # resp.raise_for_status() here would escape both the except clause
                    # below AND fetch()'s `except CdxError` - aborting the whole batch
                    # (and skipping the second host, if this was the first) instead of
                    # being recorded as this one host's failure.
                    raise CdxError(f"CDX HTTP {resp.status_code} for {params['url']}")
            except (httpx.TransportError, ValueError) as exc:
                last = exc
            self.sleep(min(2 ** attempt, 8) / max(self.criteria.tox_cdx_max_rps, 0.1))
        raise CdxError(f"CDX failed after {self.criteria.tox_cdx_max_retries} attempts: {last}")

    def hosts(self, domain: str) -> tuple[str, str]:
        bare = domain[4:] if domain.startswith("www.") else domain
        return (bare, f"www.{bare}")

    def fetch(self, domain: str) -> list[Capture]:
        """Returns [] for a never-archived domain and RAISES CdxError on failure.
        These must stay distinguishable - one is stable absence, the other transient
        ignorance, and they become different verdicts.

        One host failing does NOT fail the domain: if either query succeeds its captures
        are used, mirroring the partial-results rule. Only a failure of BOTH is CdxError,
        because only then do we genuinely know nothing."""
        captures: list[Capture] = []
        failures: list[str] = []
        for host in self.hosts(domain):
            try:
                captures.extend(parse_cdx(self._get(self._params(host))))
            except CdxError as exc:
                failures.append(f"{host}: {exc}")
        if failures and not captures:
            raise CdxError(f"CDX failed for every host of {domain} - " + "; ".join(failures))
        # De-dupe: the two hosts often overlap (and for some domains CDX appears to
        # canonicalize them to the same record entirely). bucket_monthly re-sorts, so
        # merge order does not matter.
        return list({(c.timestamp, c.digest): c for c in captures}.values())


GSB_KEY_ENV = "GOOGLE_SAFE_BROWSING_API_KEY"


class GsbClient:
    """Google Safe Browsing v4 threatMatches:find.

    Batches up to 500 URLs per request, so an entire day's screen is ONE call and the
    rate-limit surface is effectively nil. Both http:// and https:// forms are sent per
    domain: canonicalization usually makes a host-level entry match either, but at 60
    URLs against a 500 cap it is free insurance for the case where it does not."""

    def __init__(self, client: httpx.Client, criteria, api_key: str):
        self.client = client
        self.criteria = criteria
        self.api_key = api_key

    @classmethod
    def from_env(cls, client: httpx.Client, criteria) -> "GsbClient":
        key = os.environ.get(GSB_KEY_ENV, "").strip()
        if not key:
            raise ToxicityKeyMissing(
                f"{GSB_KEY_ENV} is not set. Safe Browsing needs a free Google Cloud API "
                f"key (no billing account required). Put it in .env - see .env.example.")
        return cls(client, criteria, key)

    def check(self, domains: Sequence[str]) -> dict:
        checked_at = datetime.now().isoformat(timespec="seconds")
        results = {d: GsbResult(False, (), checked_at) for d in domains}
        if not domains:
            return results
        per_domain = 2                      # http + https
        chunk = max(1, self.criteria.tox_gsb_batch_size // per_domain)
        for start in range(0, len(domains), chunk):
            batch = list(domains)[start:start + chunk]
            hits = self._find(batch)
            for domain, threats in hits.items():
                results[domain] = GsbResult(True, tuple(sorted(threats)), checked_at)
        return results

    def _find(self, batch: Sequence[str]) -> dict:
        entries = [{"url": f"{scheme}://{d}/"} for d in batch for scheme in ("http", "https")]
        body = {
            "client": {"clientId": "domainscout", "clientVersion": "0.1"},
            "threatInfo": {
                # All THREE lists must be present AND non-empty: v4 answers an empty
                # list with NO MATCHES rather than an error - a silent false-clean.
                "threatTypes": list(self.criteria.tox_gsb_threat_types),
                "platformTypes": list(self.criteria.tox_gsb_platform_types),
                "threatEntryTypes": list(self.criteria.tox_gsb_threat_entry_types),
                "threatEntries": entries,
            },
        }
        if not (body["threatInfo"]["threatTypes"] and body["threatInfo"]["platformTypes"]
                and body["threatInfo"]["threatEntryTypes"]):
            raise GsbError("refusing to send an empty threatTypes/platformTypes/"
                           "threatEntryTypes list - it returns no matches, not an error")
        try:
            resp = self.client.post(self.criteria.tox_gsb_base_url,
                                    params={"key": self.api_key}, json=body,
                                    timeout=self.criteria.tox_gsb_timeout)
        except httpx.TransportError as exc:
            raise GsbError(f"safe-browsing transport failure: {exc}") from exc
        if resp.status_code == 403:
            raise GsbError("safe-browsing rejected the API key (403) - key invalid, "
                           "Safe Browsing API not enabled, or quota exhausted")
        if resp.status_code == 400:
            raise GsbError("safe-browsing rejected the request (400) - malformed body, "
                           "which is our bug, not a configuration problem")
        if resp.status_code != 200:
            raise GsbError(f"safe-browsing HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise GsbError(f"safe-browsing returned non-JSON: {exc}") from exc
        # A clean batch is a BARE {} with no 'matches' key. Absent == not listed.
        hits: dict = {}
        for match in payload.get("matches", []) or []:
            url = (match.get("threat") or {}).get("url", "")
            for domain in batch:
                if f"//{domain}/" in url or url.rstrip("/").endswith(f"//{domain}"):
                    hits.setdefault(domain, set()).add(match.get("threatType", "UNKNOWN"))
        return hits
