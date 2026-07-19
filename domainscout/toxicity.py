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
