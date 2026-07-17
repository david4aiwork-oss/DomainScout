"""Phase 5a: NameBio comps grounding.

A cache + lookup library (NOT a pipeline stage): comps are global context keyed by
freshness, not per-candidate state, so nothing here writes `candidates`. 5c calls
lookup() and writes value_range at scoring time.

Network lives ONLY in refresh_cache(); the httpx.Client is injected so tests never hit it.
Read docs/PHASE-5A-DESIGN.md "NameBio gotchas" before touching the refresh path.
"""

from __future__ import annotations

import csv
from pathlib import Path

from domainscout.models import KeywordComps

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
