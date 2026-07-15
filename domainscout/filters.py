"""Phase 3 rules filter: track classification + graded dictionary + pronounceability
gates. Pure scoring functions + one DB loop. No network."""

from __future__ import annotations

from wordfreq import zipf_frequency

from domainscout.config import Criteria


def classify(label: str, criteria: Criteria) -> str:
    return "primary" if len(label) <= criteria.primary_max_length else "secondary"


def dict_score(label: str, criteria: Criteria) -> tuple[float, str]:
    """Best of the whole label and every 2-way split (both parts >= 3 chars),
    parts combined by criteria.dictionary_combine ('min'|'mean'). Returns
    (score, winning_segmentation).

    Split parts are floored at 3 chars, not 2: wordfreq assigns 2-letter
    fragments substantial zipf (th=4.2, ng=3.9, aa=4.01), so a 2-char floor
    would let consonant-mash (thng -> th+ng, min 3.9) falsely clear the
    dictionary gate. Real multi-word targets (red+fox, plano+hvac) use >=3-char
    words, so the 3-char floor loses no genuine combo while killing the noise."""
    best = zipf_frequency(label, "en")
    best_seg = label
    for i in range(3, len(label) - 2):  # both parts length >= 3
        left, right = label[:i], label[i:]
        lz, rz = zipf_frequency(left, "en"), zipf_frequency(right, "en")
        combined = min(lz, rz) if criteria.dictionary_combine == "min" else (lz + rz) / 2
        if combined > best:
            best, best_seg = combined, f"{left}+{right}"
    return best, best_seg
