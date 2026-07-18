"""Phase 5a: NameBio comps grounding.

A cache + lookup library (NOT a pipeline stage): comps are global context keyed by
freshness, not per-candidate state, so nothing here writes `candidates`. 5c calls
lookup() and writes value_range at scoring time.

Network lives ONLY in refresh_cache(); the httpx.Client is injected so tests never hit it.
Read docs/PHASE-5A-DESIGN.md "NameBio gotchas" before touching the refresh path.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import time
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import httpx

from domainscout import filters
from domainscout.models import CompsContext, FileRefreshResult, KeywordComps, RefreshResult

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


log = logging.getLogger(__name__)

META_FILENAME = "namebio_meta.json"


def load_meta(data_dir: str | Path) -> dict:
    """Per-file {retrieved, rows, sha256, bytes}. Missing/corrupt -> {} so refresh falls back
    to first-run rules and load still works (the sidecar is an optimisation + audit record,
    never a hard dependency for READING the cache)."""
    p = Path(data_dir) / META_FILENAME
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("comps: %s is unreadable/corrupt; treating caches as stale", p)
        return {}
    return data if isinstance(data, dict) else {}


def write_meta(data_dir: str | Path, meta: dict) -> None:
    """Atomic tmp+rename so a crash can't leave a half-written sidecar."""
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (META_FILENAME + ".tmp")
    tmp.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(d / META_FILENAME)


def _count_rows(path: Path) -> int:
    with path.open("rb") as fh:
        return max(0, sum(1 for _ in fh) - 1)  # minus header


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_download(tmp_path, *, expected_header, baseline_rows, min_rows,
                      shrink_tolerance) -> tuple[bool, str]:
    """Gate a downloaded file BEFORE it replaces a good cache.

    Atomic rename guarantees a COMPLETE file, not a GOOD one: HTTP 200 with an
    error-page-as-CSV, an empty body, or a truncated export would all atomically install
    garbage. Returns (ok, reason); reason is '' on success."""
    p = Path(tmp_path)
    if not p.is_file():
        return False, "download missing"
    try:
        with p.open("r", encoding="utf-8", newline="") as fh:
            first = fh.readline().rstrip("\n").rstrip("\r")
    except OSError as exc:
        return False, f"unreadable: {exc}"
    if not first:
        return False, "empty file"
    try:
        header = tuple(next(csv.reader([first])))
    except csv.Error as exc:
        return False, f"does not parse as CSV: {exc}"
    if header != tuple(expected_header):
        return False, (
            f"header mismatch (got {header[0]!r}, {len(header)} cols; "
            f"expected {len(expected_header)}) - NameBio may have changed the schema"
        )
    rows = _count_rows(p)
    if baseline_rows:
        floor = int(baseline_rows * shrink_tolerance)
        if rows < floor:
            return False, f"shrink: {rows} rows < {floor} ({shrink_tolerance:.0%} of {baseline_rows})"
    elif rows < min_rows:
        return False, f"below min_rows floor: {rows} < {min_rows}"
    return True, ""


def resolve_cache_path(current: Path, prev: Path) -> tuple[Path, bool]:
    """(path_to_load, used_prev). `current -> .prev` and `tmp -> current` are each atomic but
    NOT jointly atomic: a crash between them leaves no current file. Fall back loudly."""
    current, prev = Path(current), Path(prev)
    if current.is_file():
        return current, False
    if prev.is_file():
        log.warning(
            "comps cache %s missing but %s exists - loading .prev (crash between swap "
            "renames?); run `domainscout comps-refresh --force` to repair",
            current.name, prev.name,
        )
        return prev, True
    raise CompsCacheMissing(
        f"no comps cache at {current} or {prev}; run `domainscout comps-refresh`")


def cache_age_days(meta: dict, name: str, now: datetime) -> float | None:
    """Age in days from the sidecar's `retrieved`; None if unknown."""
    stamp = (meta.get(name) or {}).get("retrieved")
    if not stamp:
        return None
    try:
        return (now - datetime.fromisoformat(stamp)).total_seconds() / 86400.0
    except (TypeError, ValueError):
        return None


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

    # DEEP copy: tld_stats nests {period: {stat: value}}, and 5c reuses one loaded
    # tld_stats across a whole scoring batch. A shallow dict() would alias every
    # context's period dicts to each other and to the source.
    baseline = deepcopy(tld_stats.get(".com") or {})
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


@dataclass(frozen=True)
class FileSpec:
    name: str            # 'retailstats' | 'tldstats'
    path_attr: str       # Criteria attribute holding the URL path
    filename: str
    header: tuple[str, ...]
    min_rows_attr: str


# ORDER MATTERS: retailstats FIRST. If only one file survives the (long, uncharacterized)
# download rate-limit window, it must be the one Tier-2 actually reasons from.
FILE_SPECS: tuple[FileSpec, ...] = (
    FileSpec("retailstats", "comps_retailstats_path", "namebio_retailstats.csv",
             RETAILSTATS_HEADER, "comps_min_rows_retailstats"),
    FileSpec("tldstats", "comps_tldstats_path", "namebio_tldstats.csv",
             None, "comps_min_rows_tldstats"),
)


class RateLimited(Exception):
    """NameBio returned 429. NOT retryable in-run: recovery takes HOURS (design gotcha #3)."""


def _get_with_retry(client, url: str, dest: Path, *, retries: int = 2, sleep=time.sleep) -> int:
    """Stream url -> dest. Returns bytes written.

    Deliberately NOT ratelimit.with_backoff: that is async, its RETRYABLE is whodap-specific,
    and a comps 429 arrives as a STATUS CODE not an exception. Retries httpx.TransportError
    ONLY; a 429 raises RateLimited immediately and is never retried (gotcha #4)."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            n = 0
            with client.stream("GET", url) as resp:
                if resp.status_code == 429:
                    raise RateLimited("429 rate limited")
                resp.raise_for_status()
                with dest.open("wb") as fh:
                    for chunk in resp.iter_bytes():
                        n += len(chunk)
                        fh.write(chunk)
            return n
        except httpx.TransportError as exc:      # transient: worth one more try
            last = exc
            dest.unlink(missing_ok=True)
            if attempt >= retries:
                raise
            sleep(min(30.0, 2.0 * (2 ** attempt)))
    raise last if last else RuntimeError("unreachable")


def refresh_one(client, spec: FileSpec, criteria, data_dir: Path, meta: dict, *,
                force: bool, now: datetime, sleep=time.sleep) -> FileRefreshResult:
    """One file's INDEPENDENT freshness check -> download -> validate -> swap -> sidecar.
    A failure here must never affect the sibling file."""
    current = data_dir / spec.filename
    prev = data_dir / (spec.filename + ".prev")
    entry = meta.get(spec.name) or {}

    age = cache_age_days(meta, spec.name, now)
    if not force and current.is_file() and age is not None and age < criteria.comps_refresh_days:
        return FileRefreshResult(spec.name, "skipped_fresh",
                                 f"fresh, {age:.0f}d < {criteria.comps_refresh_days}d",
                                 rows=entry.get("rows"))

    url = criteria.comps_base_url.rstrip("/") + getattr(criteria, spec.path_attr)
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = data_dir / (spec.filename + ".tmp")
    try:
        nbytes = _get_with_retry(client, url, tmp, sleep=sleep)
    except RateLimited:
        tmp.unlink(missing_ok=True)
        return FileRefreshResult(spec.name, "refused",
                                 "429; cache intact, next daily run retries")
    except (httpx.HTTPError, OSError) as exc:
        tmp.unlink(missing_ok=True)
        return FileRefreshResult(spec.name, "refused", f"{type(exc).__name__}: {exc}")

    header = spec.header
    if header is None:   # tldstats: NameBio may add periods; key column is what matters
        with tmp.open("r", encoding="utf-8", newline="") as fh:
            first = fh.readline().rstrip("\n").rstrip("\r")
        try:
            cols = tuple(next(csv.reader([first])))
        except csv.Error:
            cols = ()
        if not cols or cols[0] != TLDSTATS_KEY_COL:
            tmp.unlink(missing_ok=True)
            return FileRefreshResult(
                spec.name, "refused",
                f"header mismatch (expected first column {TLDSTATS_KEY_COL!r})")
        header = cols

    # --force bypasses the shrink check (a legitimate >20% shrink needs it) but NEVER
    # the parse/header/min_rows checks: no flag may install an error page.
    baseline = None if force else entry.get("rows")
    ok, reason = validate_download(
        tmp, expected_header=header, baseline_rows=baseline,
        min_rows=getattr(criteria, spec.min_rows_attr),
        shrink_tolerance=criteria.comps_shrink_tolerance,
    )
    if not ok:
        tmp.unlink(missing_ok=True)
        log.warning("comps: %s refused - %s; cache left intact", spec.name, reason)
        return FileRefreshResult(spec.name, "refused", reason)

    try:
        rows, sha = _count_rows(tmp), _sha256(tmp)
        if current.is_file():
            current.replace(prev)      # atomic; keeps exactly ONE predecessor
        tmp.replace(current)           # atomic
    except OSError as exc:
        # A lock/IO error mid-swap (e.g. AV holding the file on Windows) must not
        # abort the sibling file's refresh or lose its meta. Refuse THIS file only;
        # if current was already moved to .prev, resolve_cache_path recovers on read.
        tmp.unlink(missing_ok=True)
        log.warning("comps: %s swap failed - %s; cache left at .prev if mid-swap "
                    "(resolve_cache_path recovers on read)", spec.name, exc)
        return FileRefreshResult(spec.name, "refused", f"swap failed: {exc}")
    meta[spec.name] = {"retrieved": now.isoformat(), "rows": rows,
                       "sha256": sha, "bytes": nbytes}
    return FileRefreshResult(spec.name, "swapped", "", rows=rows, bytes=nbytes)


def refresh_cache(client, criteria, data_dir, *, force: bool = False,
                  now: datetime | None = None, sleep=time.sleep) -> RefreshResult:
    """Refresh both NameBio caches, PER-FILE and INDEPENDENTLY (design doc: per-file
    independence). One file's 429 must never discard the other's validated download."""
    now = now or datetime.now()
    d = Path(data_dir)
    meta = load_meta(d)
    results = []
    for spec in FILE_SPECS:
        results.append(refresh_one(client, spec, criteria, d, meta,
                                   force=force, now=now, sleep=sleep))
    if any(r.action == "swapped" for r in results):
        write_meta(d, meta)
    return RefreshResult(files=tuple(results))


def summary_line(result: RefreshResult) -> str:
    parts = []
    for f in result.files:
        if f.action == "swapped":
            size = f", {f.bytes/1e6:.1f} MB" if f.bytes else ""
            parts.append(f"{f.name} swapped ({f.rows:,} rows{size})")
        elif f.action == "skipped_fresh":
            parts.append(f"{f.name} skipped ({f.reason})")
        else:
            parts.append(f"{f.name} REFUSED ({f.reason})")
    return "comps-refresh: " + " | ".join(parts)
