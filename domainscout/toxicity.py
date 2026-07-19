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
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

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
