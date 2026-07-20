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
from dataclasses import asdict
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
    both mean 'no captures' - a never-archived domain must never look like a failure.

    A row whose timestamp is not a plausible CDX moment (a '-' placeholder, a bare
    year, anything _to_dt cannot parse) is skipped rather than allowed to raise: CDX
    is a live third-party feed and a single malformed row is data noise, not a fatal
    error - the same treatment already given to a too-short row below. Skipping here,
    at the source, means every caller of parse_cdx (not just screen()) is protected,
    rather than relying on each call site to catch _to_dt's ValueError itself."""
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
        timestamp = str(row[ts_i])
        try:
            _to_dt(timestamp)
        except ValueError:
            continue   # malformed timestamp - noise, not a parse failure
        out.append(Capture(timestamp=timestamp, statuscode=str(row[st_i]),
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

    Rules that carry this design:
      1. unknown_error is NEVER written. Not TTL-0 - never persisted, so a transient
         failure cannot be configured into stickiness.
      2. Every entry records the collapse it was computed under, and an entry whose
         collapse differs from the current config is a MISS. That makes 'thresholds
         are calibrated to this sampling' self-enforcing instead of a comment someone
         has to notice.
      3. Every entry also records the (order-independent) GSB threat-type set it was
         screened against. Adding a threat type must invalidate up to
         cache_days['pass'] days of verdicts computed WITHOUT it - a mismatch is a
         MISS, exactly like a collapse mismatch. Recorded as a SORTED list so a mere
         reordering of the config list is never mistaken for a real change.
      4. The cache persists the FULL verdict payload - gsb and history, not just the
         four scalars - because a cache HIT is the primary path this phase is designed
         for (5c re-screens the same ~30 domains repeatedly; `pass` alone has a 14-day
         TTL). A hit that reconstructed gsb=None/history=None would be indistinguishable
         from 'the GSB leg errored', and the whole 5b deliverable (the history shape)
         would silently vanish on every cache hit.

    A further rule, added after a review finding: save() is a no-op unless put() has
    actually stored something since load. See the _dirty flag below."""

    def __init__(self, path, *, cache_days: dict, collapse: str,
                 threat_types: Sequence[str] = (), now: datetime | None = None):
        self.path = Path(path)
        self.cache_days = cache_days
        self.collapse = collapse
        self.threat_types = sorted(threat_types)
        self.now = now or datetime.now()
        self._entries: dict = {}
        self._dirty = False   # set by put(); save() no-ops while this is False
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._entries = loaded
            except (OSError, ValueError):
                self._entries = {}   # a corrupt cache is a COLD cache, never a crash

    def get(self, domain: str) -> ToxicityVerdict | None:
        """Returns the cached verdict WITH its full gsb/history payload restored, or
        None on any miss - expired TTL, a collapse/threat-type mismatch, or a
        malformed/partial stored entry. A malformed entry degrades to a clean miss
        (re-screened), never an exception, consistent with every other degradation
        path here."""
        entry = self._entries.get(domain)
        if not isinstance(entry, dict):
            return None
        if entry.get("collapse") != self.collapse:
            return None
        # Missing key (a pre-FIX-4 entry, or a hand-written test fixture) defaults to
        # [] - the SAME default self.threat_types gets when a caller doesn't pass
        # threat_types - so an old entry is comparable to a cache opened with no
        # explicit config, while still correctly missing against any REAL non-empty
        # criteria.tox_gsb_threat_types (the conservative, invalidate-when-uncertain
        # outcome an actual deploy should have).
        if entry.get("gsb_threat_types_config", []) != self.threat_types:
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
        try:
            gsb = _gsb_from_dict(entry.get("gsb"))
            history = _shape_from_dict(entry.get("history"))
        except (KeyError, TypeError, ValueError):
            return None   # corrupt nested payload - treat exactly like any other miss
        return ToxicityVerdict(
            domain=domain, verdict=entry["verdict"], reason=entry.get("reason", ""),
            gsb=gsb, history=history, screened_at=entry["screened_at"],
            collapse=entry["collapse"])

    def put(self, verdict: ToxicityVerdict) -> None:
        """Persists the FULL verdict - including gsb and history, not just the four
        scalars - so a subsequent get() can restore them (see class docstring rule 4)."""
        if verdict.verdict == VERDICT_UNKNOWN_ERROR:
            return   # see rule 1 - and note: must NOT mark the cache dirty either
        self._entries[verdict.domain] = {
            "verdict": verdict.verdict, "reason": verdict.reason,
            "screened_at": verdict.screened_at, "collapse": verdict.collapse,
            "gsb": (_gsb_to_dict(verdict.gsb) if verdict.gsb else None),
            "history": (_shape_to_dict(verdict.history) if verdict.history else None),
            "gsb_threat_types_config": self.threat_types,
        }
        self._dirty = True

    def save(self) -> None:
        """Temp-file + os.replace. The OSError catch is not theoretical: 5a hit a real
        Windows AV file-lock during exactly this rename.

        No-ops (no mkdir, no temp-write, no rename) unless put() has stored something
        since load/construction - a review finding after a run where every requested
        domain was a live cache hit still re-exercised that same rename for zero
        benefit, needlessly re-exposing the exact failure surface above. This makes the
        guarantee hold for every caller, not just screen()."""
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self._entries, indent=1, sort_keys=True),
                           encoding="utf-8")
            os.replace(tmp, self.path)
            self._dirty = False
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
        self.sleep = sleep   # injected so retry/pacing tests do not actually wait
        self._last_request_at: float | None = None   # monotonic; None until the first GET

    def _params(self, host: str) -> dict:
        return {
            "url": host,
            "output": "json",
            "matchType": self.criteria.tox_cdx_match_type,   # 'exact' - one urlkey
            "collapse": self.criteria.tox_cdx_collapse,      # server-side; legit on one urlkey
            "fl": "timestamp,statuscode,mimetype,digest",
            "limit": self.criteria.tox_cdx_limit,            # runaway guard; never engages
        }

    def _pace(self) -> None:
        """Keep the SUCCESS path under tox_cdx_max_requests_per_sec too. Before this
        fix the config value was used ONLY as the failure-path backoff divisor, so a
        clean run issued its whole request burst with zero pacing sleeps - measured at
        30 domains = 60 requests, 0 sleeps, 0.006s. Sleeps for whatever remains of the
        minimum inter-request interval since the last GET, using a MONOTONIC clock (a
        wall-clock adjustment must not corrupt the interval). Called once per attempt in
        _get, so it also paces between retries, not just between domains."""
        min_interval = 1.0 / max(self.criteria.tox_cdx_max_rps, 0.1)
        now = time.monotonic()
        if self._last_request_at is not None:
            remaining = min_interval - (now - self._last_request_at)
            if remaining > 0:
                self.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _get(self, params: dict) -> list:
        last: Exception | None = None
        for attempt in range(self.criteria.tox_cdx_max_retries):
            self._pace()
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
            # Skip the backoff sleep after the FINAL attempt: sleeping right before the
            # loop exits and raises anyway wastes it - a full outage burned
            # sum(min(2**a, 8) for a in range(max_retries-1)) seconds for nothing, up to
            # 240s of a 420s worst case at the default max_retries=4-ish settings.
            if attempt < self.criteria.tox_cdx_max_retries - 1:
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
        if failures:
            # One host failed but the OTHER succeeded, so the domain still gets an
            # unqualified `pass`-eligible shape from a single host - and nothing in the
            # verdict or logs would otherwise record that half the query failed. A
            # systematic www.-side outage would quietly thin every shape while looking
            # perfectly healthy. Surface it; ASCII-only (this runs under Task
            # Scheduler's redirected cp1252 stdout, same as VerdictCache.save's warning).
            print(f"toxicity: WARNING - partial CDX failure for {domain}: "
                  + "; ".join(failures))
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
        """Returns a GsbResult ONLY for domains whose chunk was successfully checked.

        A domain ABSENT from the returned dict was NEVER CHECKED - callers must read
        that as "unknown", never as "not listed". This matters because chunk failures
        are partial: if chunk 2 of N raises GsbError, chunks 1..N (other than 2) still
        return their real results, mirroring CdxClient.fetch's "one host failing does
        NOT fail the domain" rule. The tempting shortcut - keep pre-populating every
        domain as not-listed up front, then just wrap _find in try/except - would leave
        the failed chunk's domains sitting in the dict with a not-listed GsbResult they
        never earned: a false negative on the one leg here that is a HARD reject, i.e.
        the single most damaging failure mode this client has. So the dict is built up
        chunk-by-chunk instead, and a chunk that raises contributes nothing.

        Raises GsbError only when EVERY chunk failed (we then genuinely know nothing).
        If at least one chunk succeeded, the partial dict is returned instead of
        raising - the caller (screen()) is responsible for treating any domain missing
        from this dict as an error, not as a pass."""
        checked_at = datetime.now().isoformat(timespec="seconds")
        if not domains:
            return {}
        per_domain = 2                      # http + https
        chunk = max(1, self.criteria.tox_gsb_batch_size // per_domain)
        domains = list(domains)
        results: dict = {}
        failures: list[str] = []
        for start in range(0, len(domains), chunk):
            batch = domains[start:start + chunk]
            try:
                hits = self._find(batch)
            except GsbError as exc:
                failures.append(str(exc))
                continue
            for domain in batch:
                results[domain] = GsbResult(False, (), checked_at)
            for domain, threats in hits.items():
                results[domain] = GsbResult(True, tuple(sorted(threats)), checked_at)
        if failures and not results:
            raise GsbError(f"safe-browsing failed for every chunk: {'; '.join(failures)}")
        return results

    def _find(self, batch: Sequence[str]) -> dict:
        # HOST-LEVEL CHECK ONLY - measured against the live API 2026-07-20, owner-accepted.
        # v4 expands a lookup URL into host-suffix/path-prefix combinations, and these bare
        # forms expand to just `d/` and `d`. A blocklist entry stored at `d/some/path/`
        # therefore CANNOT match: a real host with an active MALWARE listing at a path
        # returned no match for its bare forms. `threatMatches:find` takes URLs, not hosts -
        # there is no "anything under this host?" query - so this is an API boundary, not a
        # bug here. Consequence: `currently_listed=False` means "this host is not itself
        # listed right now", NOT "nothing under this host is listed". Path-scoped listings
        # (the usual shape for COMPROMISED legitimate sites) fall to the CDX shape leg and
        # Tier-2. Do not "fix" this by widening the probe without re-reading
        # PHASE-5B-DESIGN.md - injecting CDX-observed paths was considered and rejected for
        # 5b (it couples the two deliberately-independent legs and re-budgets the 500 cap).
        entries = [{"url": f"{scheme}://{d}/"} for d in batch for scheme in ("http", "https")]
        body = {
            "client": {"clientId": "domainscout", "clientVersion": "0.1"},
            "threatInfo": {
                # All THREE lists must be present AND non-empty: v4 answers an empty
                # list with NO MATCHES rather than an error - a silent false-clean.
                # Measured 2026-07-20 against a URL known to be listed: empty
                # threatTypes -> 0 matches, empty platformTypes -> 0 matches (both
                # false-cleans), while empty threatEntryTypes still matched (v4 appears
                # to default it). We refuse all three regardless - refusing the harmless
                # case is free, and that defaulting is undocumented and could change.
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


def screen(domains: Sequence[str], *, cdx, gsb, criteria,
           cache: VerdictCache | None = None,
           now: datetime | None = None) -> list[ToxicityVerdict]:
    """Screen domains. Returns ONE verdict per input domain, IN INPUT ORDER.

    A live cache hit short-circuits BOTH legs - no CDX call, and the domain is excluded
    from the GSB batch entirely."""
    moment = now or datetime.now()
    stamp = moment.isoformat(timespec="seconds")
    verdicts: dict = {}

    pending = []
    for domain in domains:
        hit = cache.get(domain) if cache else None
        if hit is not None:
            verdicts[domain] = hit
        else:
            pending.append(domain)

    # CDX first: a per-domain failure is captured, never raised out of the batch.
    shapes: dict = {}
    errors: dict = {d: [] for d in pending}
    for domain in pending:
        try:
            shapes[domain] = compute_shape(
                cdx.fetch(domain),
                tail_window_months=criteria.tox_tail_window_months,
                tail_min_captures=criteria.tox_tail_min_captures)
        except (CdxError, ValueError) as exc:
            # ValueError is defence in depth: parse_cdx already skips a malformed
            # timestamp rather than raising, but a future path that still raises one
            # (e.g. a _to_dt call added elsewhere) must degrade only THIS domain to
            # unknown_error, never escape and abort verdicts for the rest of the batch.
            shapes[domain] = None
            errors[domain].append(f"cdx: {exc}")

    # GSB second, one batched call. GsbClient.check() (Task 9, fixed post-review) raises
    # GsbError only when EVERY chunk failed; a PARTIAL chunk failure instead returns a
    # dict that simply OMITS the domains whose chunk failed - it does not raise in that
    # case. Either way, a domain missing from gsb_results must become an error here, NOT
    # a silent pass: absence means "GSB never checked this domain," never "checked, not
    # listed." Skipping this second loop - e.g. leaving `result = gsb_results.get(domain)`
    # as None with no error appended - would let decide(None, shape, []) return pass (or
    # unknown_no_history) for a domain GSB never actually screened: a false negative on
    # the hard-reject leg, exactly the failure mode Task 9's fix exists to prevent.
    gsb_results: dict = {}
    if pending:
        try:
            gsb_results = gsb.check(pending)
        except GsbError as exc:
            for domain in pending:
                errors[domain].append(f"safe-browsing: {exc}")
        else:
            for domain in pending:
                if domain not in gsb_results:
                    errors[domain].append(
                        "safe-browsing: not checked (its GSB chunk failed - see "
                        "GsbClient.check)")

    for domain in pending:
        result = gsb_results.get(domain)
        verdict, reason = decide(result, shapes.get(domain), errors[domain])
        built = ToxicityVerdict(
            domain=domain, verdict=verdict, reason=reason, gsb=result,
            history=shapes.get(domain), screened_at=stamp,
            collapse=criteria.tox_cdx_collapse)
        verdicts[domain] = built
        if cache:
            cache.put(built)
    if cache:
        cache.save()
    return [verdicts[d] for d in domains]


def verdict_to_json(verdict: ToxicityVerdict) -> str:
    """The 5c prompt payload. The Safe Browsing field is gsb_currently_listed - never
    'clean', never 'safe'. GSB lists CURRENTLY flagged URLs, so a False means 'not
    presently listed', and a dropped domain that served malware years ago may well have
    aged off. Naming it defensively is what stops a future prompt from presenting a
    snapshot as verified safety."""
    return json.dumps({
        "domain": verdict.domain,
        "verdict": verdict.verdict,
        "reason": verdict.reason,
        "gsb_currently_listed": (verdict.gsb.currently_listed if verdict.gsb else None),
        "gsb_threat_types": (list(verdict.gsb.threat_types) if verdict.gsb else []),
        "gsb_checked_at": (verdict.gsb.checked_at if verdict.gsb else None),
        "history": (_shape_to_dict(verdict.history) if verdict.history else None),
        "screened_at": verdict.screened_at,
        "collapse": verdict.collapse,
    })


def _shape_to_dict(shape: HistoryShape) -> dict:
    return {
        "lifetime": asdict(shape.lifetime),
        "tail": asdict(shape.tail) if shape.tail else None,
        "divergence": asdict(shape.divergence) if shape.divergence else None,
    }


def _gsb_to_dict(gsb: GsbResult) -> dict:
    """The cache-persistence counterpart of _shape_to_dict, for VerdictCache.put -
    FIX 1: a cache hit used to reconstruct gsb=None unconditionally, discarding this
    leg's result even though it succeeded."""
    return {
        "currently_listed": gsb.currently_listed,
        "threat_types": list(gsb.threat_types),
        "checked_at": gsb.checked_at,
    }


def _gsb_from_dict(data) -> GsbResult | None:
    """Inverse of _gsb_to_dict. Raises (KeyError/TypeError) on a malformed payload -
    callers (VerdictCache.get) must treat that as a cache MISS, never let it escape."""
    if data is None:
        return None
    return GsbResult(currently_listed=bool(data["currently_listed"]),
                     threat_types=tuple(data["threat_types"]),
                     checked_at=data["checked_at"])


def _block_from_dict(data: dict) -> ShapeBlock:
    """One ShapeBlock leg of _shape_from_dict. Raises on a malformed payload (e.g. a
    non-dict 'lifetime') - the caller treats that as a cache MISS."""
    return ShapeBlock(
        first_capture=data["first_capture"], last_capture=data["last_capture"],
        span_years=data["span_years"], capture_count=data["capture_count"],
        distinct_years=data["distinct_years"], max_gap_years=data["max_gap_years"],
        digest_churn=data["digest_churn"], captures_per_year=data["captures_per_year"],
        status_mix=dict(data["status_mix"]), mime_mix=dict(data["mime_mix"]))


def _divergence_from_dict(data) -> Divergence | None:
    if data is None:
        return None
    return Divergence(churn_ratio=data["churn_ratio"], status_shift=data["status_shift"],
                      mime_shift=data["mime_shift"],
                      captures_per_year_ratio=data["captures_per_year_ratio"])


def _shape_from_dict(data) -> HistoryShape | None:
    """Inverse of _shape_to_dict. HistoryShape/ShapeBlock/Divergence are FROZEN
    dataclasses, so this constructs fresh instances rather than mutating anything.
    Raises (KeyError/TypeError) on a malformed payload - VerdictCache.get catches that
    and treats it as a clean cache MISS, consistent with every other degradation path
    there (never a crash, never a half-built shape)."""
    if data is None:
        return None
    lifetime = _block_from_dict(data["lifetime"])
    tail = _block_from_dict(data["tail"]) if data.get("tail") is not None else None
    divergence = _divergence_from_dict(data.get("divergence"))
    return HistoryShape(lifetime=lifetime, tail=tail, divergence=divergence)
