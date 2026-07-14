"""Load and validate criteria.toml into a frozen Criteria object."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """Raised when criteria.toml is missing, malformed, or violates an invariant."""


@dataclass(frozen=True)
class WhoisFreaksConfig:
    base_url: str
    expired_filename: str  # template containing "{date}"
    dropped_filename: str  # template containing "{date}"


@dataclass(frozen=True)
class Criteria:
    tld: str
    charset: str
    sources: tuple[str, ...]
    schedule_hint: str
    primary_max_length: int
    primary_max_words: int
    secondary_min_length: int
    secondary_max_length: int
    zipf_min: float
    pronounce_min_score: float
    tier2_cutoff: int
    digest_top_n: int
    rdap_endpoint: str
    rdap_max_rps: float
    retention_days: int
    whoisfreaks: WhoisFreaksConfig | None = None

    @property
    def ingest_max_length(self) -> int:
        """Charset+length gate ceiling = widest target (TDD §4.2). Derived, not stored."""
        return max(self.primary_max_length, self.secondary_max_length)


def _require(data: dict[str, Any], section: str, key: str) -> Any:
    if section not in data:
        raise ConfigError(f"criteria.toml: missing [{section}] section")
    if key not in data[section]:
        raise ConfigError(f"criteria.toml: missing '{key}' in [{section}]")
    return data[section][key]


def _as_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"criteria.toml: {where} must be an integer, got {value!r}")
    return value


def _as_float(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"criteria.toml: {where} must be a number, got {value!r}")
    return float(value)


def load_criteria(path: str | Path = "criteria.toml") -> Criteria:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"criteria.toml not found at {p}")
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"criteria.toml is not valid TOML: {exc}") from exc

    tld = _require(data, "ingestion", "tld")
    if tld != "com":
        raise ConfigError(
            f"criteria.toml: [ingestion].tld must be 'com' (.com only, ever), got {tld!r}"
        )

    charset = _require(data, "ingestion", "charset")
    try:
        re.compile(charset)
    except re.error as exc:
        raise ConfigError(
            f"criteria.toml: [ingestion].charset is not a valid regex: {exc}"
        ) from exc

    sources = _require(data, "ingestion", "sources")
    if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        raise ConfigError("criteria.toml: [ingestion].sources must be a list of strings")

    whoisfreaks = None
    sources_tbl = data.get("sources")
    if isinstance(sources_tbl, dict) and "whoisfreaks" in sources_tbl:
        wf = sources_tbl["whoisfreaks"]
        if not isinstance(wf, dict):
            raise ConfigError("criteria.toml: [sources.whoisfreaks] must be a table")
        for key in ("base_url", "expired_filename", "dropped_filename"):
            if key not in wf:
                raise ConfigError(
                    f"criteria.toml: missing '{key}' in [sources.whoisfreaks]"
                )
        whoisfreaks = WhoisFreaksConfig(
            base_url=str(wf["base_url"]),
            expired_filename=str(wf["expired_filename"]),
            dropped_filename=str(wf["dropped_filename"]),
        )

    return Criteria(
        tld=tld,
        charset=charset,
        sources=tuple(sources),
        schedule_hint=str(_require(data, "ingestion", "schedule_hint")),
        primary_max_length=_as_int(_require(data, "primary", "max_length"), "[primary].max_length"),
        primary_max_words=_as_int(_require(data, "primary", "max_words"), "[primary].max_words"),
        secondary_min_length=_as_int(_require(data, "secondary", "min_length"), "[secondary].min_length"),
        secondary_max_length=_as_int(_require(data, "secondary", "max_length"), "[secondary].max_length"),
        zipf_min=_as_float(_require(data, "dictionary", "zipf_min"), "[dictionary].zipf_min"),
        pronounce_min_score=_as_float(_require(data, "pronounceability", "min_score"), "[pronounceability].min_score"),
        tier2_cutoff=_as_int(_require(data, "scoring", "tier2_cutoff"), "[scoring].tier2_cutoff"),
        digest_top_n=_as_int(_require(data, "scoring", "digest_top_n"), "[scoring].digest_top_n"),
        rdap_endpoint=str(_require(data, "rdap", "endpoint")),
        rdap_max_rps=_as_float(_require(data, "rdap", "max_requests_per_sec"), "[rdap].max_requests_per_sec"),
        retention_days=_as_int(_require(data, "retention", "days"), "[retention].days"),
        whoisfreaks=whoisfreaks,
    )
