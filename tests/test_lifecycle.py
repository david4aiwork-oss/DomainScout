from datetime import datetime

from domainscout import lifecycle
from domainscout.models import RdapObservation


def _obs(status=(), available=False, events=None, expiry=None):
    return RdapObservation(available=available, status=tuple(status),
                           events=events or {}, expiry_date=expiry, status_json="[]")


def test_unmatched_statuses_filters_known_noise():
    obs = _obs(status=("client transfer prohibited", "redemption period", "weird new thing"))
    assert lifecycle.unmatched_statuses(obs) == ("weird new thing",)


def test_unmatched_statuses_empty_when_all_known():
    obs = _obs(status=("active", "client delete prohibited"))
    assert lifecycle.unmatched_statuses(obs) == ()


def test_is_due_never_verified():
    now = datetime(2026, 7, 15, 12, 0, 0)
    assert lifecycle._is_due("redemption", None, now, {"redemption": 2}) is True


def test_is_due_within_cadence_is_false():
    now = datetime(2026, 7, 15, 12, 0, 0)
    va = datetime(2026, 7, 14, 12, 0, 0)  # 1 day ago, cadence 2
    assert lifecycle._is_due("redemption", va, now, {"redemption": 2}) is False


def test_is_due_past_cadence_is_true():
    now = datetime(2026, 7, 15, 12, 0, 0)
    va = datetime(2026, 7, 12, 12, 0, 0)  # 3 days ago, cadence 2
    assert lifecycle._is_due("redemption", va, now, {"redemption": 2}) is True


def test_is_due_missing_status_always_due():
    now = datetime(2026, 7, 15, 12, 0, 0)
    va = datetime(2026, 7, 15, 11, 0, 0)  # 1 hour ago
    # 'unknown' and 'expiring' are not in the cadence map -> 0 days -> always due
    assert lifecycle._is_due("unknown", va, now, {"redemption": 2}) is True
    assert lifecycle._is_due("expiring", va, now, {"redemption": 2}) is True
