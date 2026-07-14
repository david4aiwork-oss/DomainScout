"""WhoisFreaks free-feed adapter.

The feed is a date-stamped, newline-delimited list of domain NAMES (no header,
one per line) despite the .csv extension. Lifecycle comes from RDAP (Phase 4);
this adapter only yields raw names — the gate does the rejecting."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Iterator

from domainscout.config import ConfigError, Criteria
from domainscout.sources.base import FeedFile


class WhoisFreaksSource:
    name = "whoisfreaks"

    def __init__(self, base_url: str, expired_filename: str, dropped_filename: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._expired_filename = expired_filename
        self._dropped_filename = dropped_filename

    @classmethod
    def from_criteria(cls, criteria: Criteria) -> "WhoisFreaksSource":
        cfg = criteria.whoisfreaks
        if cfg is None:
            raise ConfigError(
                "criteria.toml: [sources.whoisfreaks] is required to run the "
                "whoisfreaks source"
            )
        return cls(cfg.base_url, cfg.expired_filename, cfg.dropped_filename)

    def feed_files(self, run_date: date) -> list[FeedFile]:
        stamp = run_date.isoformat()
        return [
            self._feed_file("expired", self._expired_filename, stamp),
            self._feed_file("dropped", self._dropped_filename, stamp),
        ]

    def _feed_file(self, category: str, template: str, stamp: str) -> FeedFile:
        name = template.format(date=stamp)
        return FeedFile(
            source=self.name,
            feed_category=category,
            remote_url=f"{self._base_url}/{name}",
            local_name=name,
        )

    def iter_domains(self, path: Path) -> Iterator[str]:
        with Path(path).open("r", encoding="utf-8") as fh:
            for line in fh:
                name = line.strip()
                if name:
                    yield name
