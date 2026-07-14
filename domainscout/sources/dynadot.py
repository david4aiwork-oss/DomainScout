"""Dynadot expired-auction adapter — interface stub only.

Locks the FeedSource contract so a second source drops in later. Real wiring
(auction CSV: prices, auction-end dates) is deferred to a Phase 2b spec."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Iterator

from domainscout.sources.base import FeedFile

_MSG = "Dynadot ingestion is Phase 2b"


class DynadotSource:
    name = "dynadot"

    @classmethod
    def from_criteria(cls, criteria) -> "DynadotSource":
        return cls()

    def feed_files(self, run_date: date) -> list[FeedFile]:
        raise NotImplementedError(_MSG)

    def iter_domains(self, path: Path) -> Iterator[str]:
        raise NotImplementedError(_MSG)
