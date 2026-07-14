"""Shared feed-source interface: isolates source-specific format knowledge
(URLs, file parsing) from the generic ingestion orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable


@dataclass(frozen=True)
class FeedFile:
    """One downloadable feed file for a given run date."""

    source: str
    feed_category: str  # 'expired' | 'dropped'
    remote_url: str
    local_name: str


@runtime_checkable
class FeedSource(Protocol):
    """A data source. All source-specific format knowledge lives in the adapter."""

    name: str

    def feed_files(self, run_date: date) -> list[FeedFile]:
        """Which files to pull for run_date."""
        ...

    def iter_domains(self, path: Path) -> Iterator[str]:
        """Parse a local feed file into raw (un-gated) domain strings."""
        ...
