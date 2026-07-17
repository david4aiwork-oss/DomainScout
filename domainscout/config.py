"""Load and validate criteria.toml into a frozen Criteria object."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
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
    primary_allow_invented: bool = True
    dictionary_combine: str = "min"
    rdap_concurrency: int = 5
    rdap_max_retries: int = 4
    rdap_timeout: float = 15.0
    rdap_user_agent: str = "DomainScout/0.1 (personal expired-domain research)"
    rdap_recheck_days: dict = field(
        default_factory=lambda: {"pending_delete": 1, "redemption": 2, "grace": 7, "dropped": 7}
    )
    comps_base_url: str = "https://api.namebio.com"
    comps_retailstats_path: str = "/retailstats-download"
    comps_tldstats_path: str = "/tldstats-download"
    comps_refresh_days: int = 7
    comps_shrink_tolerance: float = 0.8
    comps_min_rows_retailstats: int = 1000
    comps_min_rows_tldstats: int = 100
    comps_stale_warn_factor: int = 3

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


def _as_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"criteria.toml: {where} must be a boolean, got {value!r}")
    return value


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

    allow_invented = _as_bool(
        data["primary"].get("allow_invented", True), "[primary].allow_invented"
    )
    combine = str(data["dictionary"].get("combine", "min"))
    if combine not in ("min", "mean"):
        raise ConfigError(
            f"criteria.toml: [dictionary].combine must be 'min' or 'mean', got {combine!r}"
        )

    rdap_tbl = data.get("rdap", {})
    _DEFAULT_RECHECK = {"pending_delete": 1, "redemption": 2, "grace": 7, "dropped": 7}
    recheck_tbl = rdap_tbl.get("recheck_days", {})
    if not isinstance(recheck_tbl, dict):
        raise ConfigError("criteria.toml: [rdap.recheck_days] must be a table")
    rdap_recheck_days = {
        **_DEFAULT_RECHECK,
        **{str(k): _as_int(v, f"[rdap.recheck_days].{k}") for k, v in recheck_tbl.items()},
    }
    rdap_concurrency = _as_int(rdap_tbl.get("concurrency", 5), "[rdap].concurrency")
    rdap_max_retries = _as_int(rdap_tbl.get("max_retries", 4), "[rdap].max_retries")
    rdap_timeout = _as_float(rdap_tbl.get("timeout", 15.0), "[rdap].timeout")
    rdap_user_agent = str(rdap_tbl.get("user_agent", "DomainScout/0.1 (personal expired-domain research)"))

    comps_tbl = data.get("comps", {})
    if not isinstance(comps_tbl, dict):
        raise ConfigError("criteria.toml: [comps] must be a table")
    comps_base_url = str(comps_tbl.get("base_url", "https://api.namebio.com"))
    comps_retailstats_path = str(comps_tbl.get("retailstats_path", "/retailstats-download"))
    comps_tldstats_path = str(comps_tbl.get("tldstats_path", "/tldstats-download"))
    comps_refresh_days = _as_int(comps_tbl.get("refresh_days", 7), "[comps].refresh_days")
    comps_shrink_tolerance = _as_float(
        comps_tbl.get("shrink_tolerance", 0.8), "[comps].shrink_tolerance")
    comps_min_rows_retailstats = _as_int(
        comps_tbl.get("min_rows_retailstats", 1000), "[comps].min_rows_retailstats")
    comps_min_rows_tldstats = _as_int(
        comps_tbl.get("min_rows_tldstats", 100), "[comps].min_rows_tldstats")
    comps_stale_warn_factor = _as_int(
        comps_tbl.get("stale_warn_factor", 3), "[comps].stale_warn_factor")

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
        primary_allow_invented=allow_invented,
        dictionary_combine=combine,
        rdap_concurrency=rdap_concurrency,
        rdap_max_retries=rdap_max_retries,
        rdap_timeout=rdap_timeout,
        rdap_user_agent=rdap_user_agent,
        rdap_recheck_days=rdap_recheck_days,
        comps_base_url=comps_base_url,
        comps_retailstats_path=comps_retailstats_path,
        comps_tldstats_path=comps_tldstats_path,
        comps_refresh_days=comps_refresh_days,
        comps_shrink_tolerance=comps_shrink_tolerance,
        comps_min_rows_retailstats=comps_min_rows_retailstats,
        comps_min_rows_tldstats=comps_min_rows_tldstats,
        comps_stale_warn_factor=comps_stale_warn_factor,
    )
