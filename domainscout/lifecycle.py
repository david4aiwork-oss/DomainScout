"""Pure open-cycle lifecycle logic: status classification, cadence, and the drop-date
transition table. NO I/O — fully fixture-testable. See docs/PHASE-4-DESIGN.md."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from domainscout.models import LifecycleUpdate, RdapObservation

REDEMPTION_TAIL_DAYS = 35   # ICANN RGP redemption 30 d + pendingDelete 5 d
PENDING_DELETE_DAYS = 5
GRACE_EST_DAYS = 45         # low-confidence, anchored on TODAY (never expiry). HARD FLOOR 35:
                            # an autoRenewPeriod domain cannot drop sooner than the fixed 35 d tail.

# RDAP status strings we act on (lowercased), in next_state's documented order.
S_PENDING_DELETE = "pending delete"
S_REDEMPTION = "redemption period"
S_PENDING_RESTORE = "pending restore"        # filed restore -> kept OPEN as redemption
S_AUTO_RENEW = "auto renew period"
S_HOLDS = ("client hold", "server hold")     # hold + no RGP -> kept OPEN as grace (not a closure)

# Statuses we understand (decision-relevant + expected registry noise). Anything OUTSIDE this set
# is tallied as "unmatched" in the run summary, surfacing novel Verisign strings as a number.
KNOWN_STATUSES = frozenset({
    S_PENDING_DELETE, S_REDEMPTION, S_PENDING_RESTORE, S_AUTO_RENEW, *S_HOLDS,
    "active", "ok", "inactive",
    "client transfer prohibited", "server transfer prohibited",
    "client delete prohibited", "server delete prohibited",
    "client update prohibited", "server update prohibited",
    "client renew prohibited", "server renew prohibited",
})


def unmatched_statuses(obs: RdapObservation) -> tuple[str, ...]:
    """Status strings not in KNOWN_STATUSES — counted per run to catch registry surprises."""
    return tuple(s for s in obs.status if s not in KNOWN_STATUSES)


def _is_due(status: str, verified_at: datetime | None, now: datetime,
            recheck_days: dict[str, int]) -> bool:
    """True if never verified, or verified longer ago than the status's cadence (missing -> 0 d)."""
    if verified_at is None:
        return True
    return (now - verified_at) >= timedelta(days=recheck_days.get(status, 0))


def _drop_after(events: dict, action: str, today: date, offset_days: int) -> date:
    """Anchor a drop estimate on the RGP phase event date if present, else on today."""
    base = events.get(action, today)
    return base + timedelta(days=offset_days)


def next_state(current: str, obs: RdapObservation, today: date) -> LifecycleUpdate:
    """Map (current lifecycle_status, RDAP observation) -> the new cycle state + drop dates.
    First matching rule wins; see docs/PHASE-4-DESIGN.md transition table."""
    if obs.available:  # RDAP 404
        return LifecycleUpdate("dropped", None, today, None)

    st = set(obs.status)
    if S_PENDING_DELETE in st:
        est = _drop_after(obs.events, S_PENDING_DELETE, today, PENDING_DELETE_DAYS)
        return LifecycleUpdate("pending_delete", est, None, obs.expiry_date)
    if S_REDEMPTION in st or S_PENDING_RESTORE in st:
        est = _drop_after(obs.events, S_REDEMPTION, today, REDEMPTION_TAIL_DAYS)
        return LifecycleUpdate("redemption", est, None, obs.expiry_date)
    if S_AUTO_RENEW in st:
        return LifecycleUpdate("grace", today + timedelta(days=GRACE_EST_DAYS), None, obs.expiry_date)
    if current == "dropped":  # was available, now registered (even if on hold) -> re-registered
        return LifecycleUpdate("reregistered", None, None, obs.expiry_date)
    if any(h in st for h in S_HOLDS):  # hold + no RGP + not dropped -> mid-expiry-flow park
        return LifecycleUpdate("grace", today + timedelta(days=GRACE_EST_DAYS), None, obs.expiry_date)
    return LifecycleUpdate("renewed", None, None, obs.expiry_date)  # plainly registered -> recovered
