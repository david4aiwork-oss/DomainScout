"""N-gram phonotactic pronounceability scorer.

Boundary-padded trigram model, scored in LOG space (mean log conditional
probability) for a single length-consistent threshold scale. Tables are stored
as INTEGER COUNTS (byte-deterministic in git); add-one smoothing is applied at
load. No network at scoring time."""

from __future__ import annotations

import json
import math
import re
from datetime import date
from pathlib import Path

DEFAULT_TABLES_PATH = Path(__file__).parent / "pronounce_tables.json"

_WORD_RE = re.compile(r"^[a-z]+$")
V = 27  # smoothing vocabulary: 26 letters + end marker '$' (start '^' is context-only)


def build_tables(top_n: int = 50000, words: list[str] | None = None) -> dict:
    """Count boundary-padded trigrams over English word TYPES (unweighted)."""
    if words is None:
        from wordfreq import top_n_list  # local import: not needed for tests that pass words=
        words = top_n_list("en", top_n)
    trigram_counts: dict[str, int] = {}
    context2_totals: dict[str, int] = {}
    kept = 0
    for w in words:
        if not _WORD_RE.match(w):
            continue
        kept += 1
        padded = f"^^{w}$"
        for i in range(len(padded) - 2):
            tri = padded[i:i + 3]
            ctx = padded[i:i + 2]
            trigram_counts[tri] = trigram_counts.get(tri, 0) + 1
            context2_totals[ctx] = context2_totals.get(ctx, 0) + 1
    try:
        from importlib.metadata import version as _pkg_version
        wf_version = _pkg_version("wordfreq")  # wordfreq exposes no __version__ attr
    except Exception:
        wf_version = "unknown"
    meta = {
        "top_n": top_n,
        "words_kept": kept,
        "wordfreq_version": wf_version,
        "built": date.today().isoformat(),
        "alphabet": "a-z + '^' start (context-only) + '$' end",
        "smoothing": f"add-one at load, V={V}",
        "scoring": "mean log P(c3|c1c2), boundary-padded '^^label$', trigram-uniform",
    }
    return {
        "_meta": meta,
        "trigram_counts": trigram_counts,
        "context2_totals": context2_totals,
    }


def save_tables(tables: dict, path: str | Path) -> None:
    Path(path).write_text(
        json.dumps(tables, sort_keys=True, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )


class Model:
    """Add-one smoothed trigram log-probabilities over the built counts."""

    def __init__(self, trigram_counts: dict[str, int], context2_totals: dict[str, int]) -> None:
        self._tri = trigram_counts
        self._ctx = context2_totals

    @classmethod
    def from_tables(cls, tables: dict) -> "Model":
        return cls(tables["trigram_counts"], tables["context2_totals"])

    def logp(self, trigram: str) -> float:
        num = self._tri.get(trigram, 0) + 1
        den = self._ctx.get(trigram[:2], 0) + V
        return math.log(num / den)


def load_tables(path: str | Path = DEFAULT_TABLES_PATH) -> Model:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Model.from_tables(data)


_DEFAULT_MODEL: Model | None = None


def default_model() -> Model:
    global _DEFAULT_MODEL
    if _DEFAULT_MODEL is None:
        _DEFAULT_MODEL = load_tables()
    return _DEFAULT_MODEL


def score(label: str, model: Model | None = None) -> float:
    """Mean log P(c3|c1c2) over the boundary-padded trigrams of the label.
    Log space => always <= 0, finite (smoothing). Trigram-uniform for all lengths."""
    m = model if model is not None else default_model()
    padded = f"^^{label}$"
    trigrams = [padded[i:i + 3] for i in range(len(padded) - 2)]
    return sum(m.logp(t) for t in trigrams) / len(trigrams)
