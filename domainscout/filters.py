"""Phase 3 rules filter: track classification + graded dictionary + pronounceability
gates. Pure scoring functions + one DB loop. No network."""

from __future__ import annotations

from datetime import datetime

from wordfreq import zipf_frequency

from domainscout import db, pronounce
from domainscout.config import Criteria
from domainscout.models import FilterCounts


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


_OPEN_PREDICATE = "lifecycle_status NOT IN ('renewed','reregistered','dismissed')"


def decide(
    track: str,
    dict_score_val: float,
    seg: str,
    pronounce_score_val: float,
    criteria: Criteria,
) -> tuple[bool, str]:
    """Track-specific pass/fail. Reason names the admitting/failing gate."""
    dict_ok = dict_score_val >= criteria.zipf_min
    pron_ok = pronounce_score_val >= criteria.pronounce_min_score
    if track == "primary":
        passed = dict_ok or (pron_ok if criteria.primary_allow_invented else False)
    else:
        passed = pron_ok or dict_ok
    if passed:
        if dict_ok:  # dict takes precedence in the label when both pass
            return True, f"{track} dict={dict_score_val:.2f} {seg}"
        return True, f"{track} pronounce={pronounce_score_val:.2f}"
    if track == "primary" and not criteria.primary_allow_invented:
        return False, (
            f"reject primary: not dictionary "
            f"(dict={dict_score_val:.2f}<{criteria.zipf_min})"
        )
    return False, (
        f"reject {track}: dict={dict_score_val:.2f}<{criteria.zipf_min}, "
        f"pronounce={pronounce_score_val:.2f}<{criteria.pronounce_min_score}"
    )


def _label(domain: str) -> str:
    return domain[:-4] if domain.endswith(".com") else domain


def filter_candidates(
    conn,
    criteria: Criteria,
    *,
    recompute: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
) -> FilterCounts:
    """Classify + score + decide each open candidate; write the 6 filter columns
    (unless dry_run). Default processes filtered_at IS NULL; recompute = all open."""
    where = _OPEN_PREDICATE if recompute else f"{_OPEN_PREDICATE} AND filtered_at IS NULL"
    sql = f"SELECT id, domain FROM candidates WHERE {where} ORDER BY id"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()

    counts = FilterCounts()
    stamp = datetime.now().isoformat(timespec="seconds")
    for row in rows:
        counts.processed += 1
        label = _label(row["domain"])
        track = classify(label, criteria)
        d_score, seg = dict_score(label, criteria)
        p_score = pronounce.score(label)
        passed, reason = decide(track, d_score, seg, p_score, criteria)
        if passed:
            counts.passed += 1
            counts.primary += track == "primary"
            counts.secondary += track == "secondary"
        else:
            counts.rejected += 1
        if not dry_run:
            db.set_filter_result(
                conn, row["id"], track=track, dict_score=d_score,
                pronounce_score=p_score, filter_pass=passed, filter_reason=reason,
                filtered_at=stamp,
            )
    return counts
