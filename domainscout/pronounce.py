"""N-gram phonotactic pronounceability scorer.

Boundary-padded trigram model, scored in LOG space (mean log conditional
probability) for a single length-consistent threshold scale. Tables are stored
as INTEGER COUNTS (byte-deterministic in git); add-one smoothing is applied at
load. No network at scoring time."""

from __future__ import annotations

import json
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
        import wordfreq
        wf_version = getattr(wordfreq, "__version__", "unknown")
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
