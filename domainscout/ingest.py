"""Ingestion: pure gate + network download + orchestration.

Only survivors of the hard-invariant gate land in the permanent DB. Network
lives solely in download(); the httpx.Client is injected so tests never hit it."""

from __future__ import annotations

import re
import ssl
from datetime import date
from pathlib import Path
from typing import Callable

import httpx
import truststore

from domainscout import db
from domainscout.config import Criteria
from domainscout.models import Candidate, IngestCounts
from domainscout.sources.base import FeedFile, FeedSource
from domainscout.sources.dynadot import DynadotSource
from domainscout.sources.whoisfreaks import WhoisFreaksSource

DEFAULT_FEEDS_DIR = "data/feeds"

SOURCE_FACTORIES: "dict[str, Callable[[Criteria], FeedSource]]" = {
    "whoisfreaks": WhoisFreaksSource.from_criteria,
    "dynadot": DynadotSource.from_criteria,
}


def gate(domain: str, criteria: Criteria) -> tuple[bool, str | None]:
    """Apply the hard invariant. First failure wins; buckets are mutually
    exclusive. Length is measured on the label (name without the .com suffix)."""
    name = domain.strip().lower()
    if not name.endswith(".com"):
        return (False, "rejected_tld")
    label = name[:-4]  # strip ".com"
    if not re.match(criteria.charset, label):
        return (False, "rejected_charset")
    if len(label) > criteria.ingest_max_length:
        return (False, "rejected_length")
    return (True, None)


def make_client(timeout: float = 30.0) -> httpx.Client:
    """Build an httpx.Client that verifies TLS against the OS trust store.

    This box (and many Windows machines) runs an AV/proxy that intercepts HTTPS
    with a private root CA present in the OS store but absent from certifi;
    truststore makes verification use the OS store. Portable: the Windows store
    here, the system CA store on a future Linux VPS."""
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return httpx.Client(verify=ctx, follow_redirects=True, timeout=timeout)


def download(feed_file: FeedFile, feeds_dir: str | Path, client: httpx.Client) -> Path:
    """GET the feed file to feeds_dir/local_name; skip if it already exists."""
    dest = Path(feeds_dir) / feed_file.local_name
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = client.get(feed_file.remote_url)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest


def ingest_file(
    conn,
    source: FeedSource,
    *,
    path: Path,
    feed_category: str,
    feed_file_name: str,
    run_date: date,
    criteria: Criteria,
    dry_run: bool = False,
) -> IngestCounts:
    """Gate every name in one local feed file; upsert survivors and log counts."""
    counts = IngestCounts(source=source.name, feed_file=feed_file_name, run_date=run_date)
    for raw in source.iter_domains(Path(path)):
        counts.seen += 1
        ok, reason = gate(raw, criteria)
        if ok:
            counts.landed += 1
            if not dry_run:
                db.upsert_candidate(
                    conn,
                    Candidate(
                        domain=raw.strip().lower(),
                        source=source.name,
                        feed_category=feed_category,
                    ),
                )
        elif reason == "rejected_tld":
            counts.rejected_tld += 1
        elif reason == "rejected_charset":
            counts.rejected_charset += 1
        else:  # "rejected_length"
            counts.rejected_length += 1
    if not dry_run:
        db.record_ingest(conn, counts)
    return counts


def build_source(name: str, criteria: Criteria) -> FeedSource:
    try:
        factory = SOURCE_FACTORIES[name]
    except KeyError:
        raise ValueError(f"unknown source: {name!r}") from None
    return factory(criteria)


def infer_feed_category(filename: str) -> str | None:
    low = filename.lower()
    if "expired" in low:
        return "expired"
    if "dropped" in low:
        return "dropped"
    return None


def ingest_source(
    conn,
    source: FeedSource,
    run_date: date,
    criteria: Criteria,
    feeds_dir: str | Path,
    client: httpx.Client,
    *,
    dry_run: bool = False,
) -> list[IngestCounts]:
    """Download + ingest each of the source's feed files. A file that is not
    published yet (404 during the ~1-day lag) is a warning + skip, not a crash."""
    results: list[IngestCounts] = []
    for feed_file in source.feed_files(run_date):
        try:
            path = download(feed_file, feeds_dir, client)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                print(f"warning: {feed_file.remote_url} not published yet (404) - skipping")
                continue
            raise
        results.append(
            ingest_file(
                conn,
                source,
                path=path,
                feed_category=feed_file.feed_category,
                feed_file_name=feed_file.local_name,
                run_date=run_date,
                criteria=criteria,
                dry_run=dry_run,
            )
        )
    return results


def ingest_local_file(
    conn,
    *,
    path: str | Path,
    criteria: Criteria,
    run_date: date,
    source_name: str = "whoisfreaks",
    feed_category: str | None = None,
    dry_run: bool = False,
) -> IngestCounts:
    """Ingest a LOCAL feed file (offline/TDD + re-ingest-from-retained path)."""
    source = build_source(source_name, criteria)
    category = feed_category or infer_feed_category(Path(path).name)
    if category is None:
        raise ValueError(
            f"cannot infer feed_category from {Path(path).name!r}; pass --feed-category"
        )
    return ingest_file(
        conn,
        source,
        path=Path(path),
        feed_category=category,
        feed_file_name=Path(path).name,
        run_date=run_date,
        criteria=criteria,
        dry_run=dry_run,
    )


def run_ingest(
    conn,
    *,
    criteria: Criteria,
    run_date: date,
    source_names: "list[str]",
    feeds_dir: str | Path,
    client: httpx.Client,
    dry_run: bool = False,
) -> list[IngestCounts]:
    """Ingest every requested source. Not-yet-implemented sources (the Dynadot
    stub, which raises NotImplementedError) are skipped with a notice."""
    results: list[IngestCounts] = []
    for name in source_names:
        source = build_source(name, criteria)
        try:
            results.extend(
                ingest_source(conn, source, run_date, criteria, feeds_dir, client,
                              dry_run=dry_run)
            )
        except NotImplementedError as exc:
            print(f"skipping source {name!r}: {exc}")
    return results


def summary_line(counts: IngestCounts) -> str:
    return (
        f"{counts.source} {counts.feed_file}: seen={counts.seen} "
        f"tld={counts.rejected_tld} charset={counts.rejected_charset} "
        f"length={counts.rejected_length} landed={counts.landed}"
    )
