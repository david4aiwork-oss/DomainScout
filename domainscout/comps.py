"""Phase 5a: NameBio comps grounding.

A cache + lookup library (NOT a pipeline stage): comps are global context keyed by
freshness, not per-candidate state, so nothing here writes `candidates`. 5c calls
lookup() and writes value_range at scoring time.

Network lives ONLY in refresh_cache(); the httpx.Client is injected so tests never hit it.
Read docs/PHASE-5A-DESIGN.md "NameBio gotchas" before touching the refresh path.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

from domainscout import filters
from domainscout.models import CompsContext, KeywordComps

# The real 21-column header from GET /retailstats-download (verified live 2026-07-16).
# Matched EXACTLY before a swap: a NameBio column change must brick the refresh rather
# than silently shift our column reads. Task 6 gates on this; Task 7 surfaces the staleness.
RETAILSTATS_HEADER: tuple[str, ...] = (
    "keyword",
    "exact_sale_count", "exact_price_sum", "exact_price_avg", "exact_price_max", "exact_price_stddev",
    "start_sale_count", "start_price_sum", "start_price_avg", "start_price_max", "start_price_stddev",
    "end_sale_count", "end_price_sum", "end_price_avg", "end_price_max", "end_price_stddev",
    "middle_sale_count", "middle_price_sum", "middle_price_avg", "middle_price_max", "middle_price_stddev",
)
TLDSTATS_KEY_COL = "extension"
PLACEMENTS = ("exact", "start", "end", "middle")


class CompsCacheMissing(FileNotFoundError):
    """No comps cache (and no .prev) — run `domainscout comps-refresh`."""


def load_index(path: str | Path) -> dict[str, str]:
    """keyword -> raw CSV line. Raw lines (not parsed rows) keep this ~15 MB instead of
    hundreds of MB: we touch ~60 of the ~2M cells per run."""
    p = Path(path)
    if not p.is_file():
        raise CompsCacheMissing(f"no comps cache at {p}; run `domainscout comps-refresh`")
    index: dict[str, str] = {}
    with p.open("r", encoding="utf-8", newline="") as fh:
        fh.readline()  # header; validated at swap time, not on every load
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            kw = line.split(",", 1)[0].strip().lower()
            if kw:
                index[kw] = line
    return index


def parse_placement(line: str, placement: str) -> KeywordComps | None:
    """Pull one placement's 5 stats out of a raw retailstats line.
    Returns None when the keyword has 0 sales at that placement — absence of data, which
    lookup() reports as 'no comparable sales' rather than as a zero-valued comp."""
    if placement not in PLACEMENTS:
        raise ValueError(f"unknown placement {placement!r}; expected one of {PLACEMENTS}")
    cells = next(csv.reader([line]))
    row = dict(zip(RETAILSTATS_HEADER, cells))
    try:
        sale_count = int(float(row[f"{placement}_sale_count"] or 0))
    except (KeyError, ValueError):
        return None
    if sale_count <= 0:
        return None

    def num(col: str) -> float:
        try:
            return float(row.get(col) or 0.0)
        except ValueError:
            return 0.0

    return KeywordComps(
        keyword=row["keyword"].strip().lower(),
        placement=placement,
        sale_count=sale_count,
        price_avg=num(f"{placement}_price_avg"),
        price_max=num(f"{placement}_price_max"),
        price_stddev=num(f"{placement}_price_stddev"),
    )


def load_tld_stats(path: str | Path) -> dict[str, dict]:
    """extension -> {period: {stat: value}}. Columns are read BY NAME (`<period>_<stat>`),
    never by index, so NameBio adding a period does not shift our reads."""
    p = Path(path)
    if not p.is_file():
        raise CompsCacheMissing(f"no comps cache at {p}; run `domainscout comps-refresh`")
    out: dict[str, dict] = {}
    with p.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            ext = (row.get(TLDSTATS_KEY_COL) or "").strip().lower()
            if not ext:
                continue
            periods: dict[str, dict] = {}
            for col, raw in row.items():
                if not col or col == TLDSTATS_KEY_COL or raw is None:
                    continue
                for stat in ("_sale_count", "_price_sum", "_price_avg", "_price_max", "_price_stddev"):
                    if col.endswith(stat):
                        period = col[: -len(stat)]
                        try:
                            val = float(raw or 0.0)
                        except ValueError:
                            val = 0.0
                        key = stat.lstrip("_")
                        periods.setdefault(period, {})[key] = (
                            int(val) if key == "sale_count" else val
                        )
                        break
            out[ext] = periods
    return out


def lookup(domain, index, tld_stats, criteria, *, retrieved: str | None = None) -> CompsContext:
    """Comps for one .com domain. Placement is chosen by word POSITION, which is exactly
    what NameBio's exact/start/end placements mean:
      1 part  -> `exact` for the label
      2 parts -> `start` for the left word, `end` for the right
    Segmentation is REUSED from filters.dict_score (Phase 3) - the single source of truth
    for splitting a label; a second splitter would drift from the dictionary gate.
    A missing keyword yields no entry: absence of evidence, NOT a zero-valued comp."""
    label = domain[:-4] if domain.endswith(".com") else domain
    _score, seg = filters.dict_score(label, criteria)

    found: list[KeywordComps] = []
    if "+" in seg:
        left, right = seg.split("+", 1)
        for word, placement in ((left, "start"), (right, "end")):
            line = index.get(word)
            if line:
                kc = parse_placement(line, placement)
                if kc:
                    found.append(kc)
    else:
        line = index.get(seg)
        if line:
            kc = parse_placement(line, "exact")
            if kc:
                found.append(kc)

    # Always also try the WHOLE label as an exact keyword (catches e.g. a known compound).
    exact = None
    whole = index.get(label)
    if whole:
        exact = parse_placement(whole, "exact")
    if exact is not None and any(
        k.keyword == exact.keyword and k.placement == "exact" for k in found
    ):
        exact = None  # already reported in `keywords`; don't duplicate

    baseline = dict(tld_stats.get(".com") or {})
    baseline["extension"] = ".com"
    return CompsContext(
        domain=domain, segmentation=seg, keywords=tuple(found), exact=exact,
        tld_baseline=baseline, retrieved=retrieved,
    )


def context_to_json(ctx: CompsContext) -> str:
    """Serialize to the candidates.value_range payload (5c writes it).
    `modeled` is ALWAYS emitted as null - the reserved ValuationProvider slot."""
    return json.dumps({
        "source": "namebio-free",
        "retrieved": ctx.retrieved,
        "segmentation": ctx.segmentation,
        "keywords": [asdict(k) for k in ctx.keywords],
        "exact": asdict(ctx.exact) if ctx.exact else None,
        "tld_baseline": ctx.tld_baseline,
        "modeled": ctx.modeled,
        "attribution": ctx.attribution,
    })
