from datetime import date, datetime

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


TODAY = date(2026, 7, 15)


def test_available_becomes_dropped_and_sets_actual():
    upd = lifecycle.next_state("redemption", _obs(available=True), TODAY)
    assert upd.lifecycle_status == "dropped"
    assert upd.drop_date_actual == TODAY
    assert upd.drop_date_est is None


def test_pending_delete_estimate_today_plus_5():
    upd = lifecycle.next_state("redemption", _obs(status=("pending delete",)), TODAY)
    assert upd.lifecycle_status == "pending_delete"
    assert upd.drop_date_est == date(2026, 7, 20)


def test_redemption_estimate_today_plus_35():
    upd = lifecycle.next_state("unknown", _obs(status=("redemption period",)), TODAY)
    assert upd.lifecycle_status == "redemption"
    assert upd.drop_date_est == date(2026, 8, 19)


def test_pending_restore_kept_open_as_redemption():
    upd = lifecycle.next_state("redemption", _obs(status=("pending restore",)), TODAY)
    assert upd.lifecycle_status == "redemption"
    assert upd.drop_date_est == date(2026, 8, 19)


def test_auto_renew_becomes_grace_anchored_on_today_not_expiry():
    # expiry is ~13 months out (auto-renewed); grace must NOT use it
    obs = _obs(status=("auto renew period",), expiry=date(2027, 6, 1))
    upd = lifecycle.next_state("unknown", obs, TODAY)
    assert upd.lifecycle_status == "grace"
    assert upd.drop_date_est == date(2026, 8, 29)  # today + 45, not near 2027


def test_hold_without_rgp_kept_open_as_grace():
    upd = lifecycle.next_state("unknown", _obs(status=("client hold",)), TODAY)
    assert upd.lifecycle_status == "grace"
    assert upd.drop_date_est == date(2026, 8, 29)


def test_dropped_then_registered_closes_reregistered():
    upd = lifecycle.next_state("dropped", _obs(status=("active",)), TODAY)
    assert upd.lifecycle_status == "reregistered"
    assert upd.drop_date_est is None


def test_dropped_then_registered_on_hold_still_reregistered():
    # row 5 (dropped->reregistered) beats row 6 (hold->grace)
    upd = lifecycle.next_state("dropped", _obs(status=("client hold",)), TODAY)
    assert upd.lifecycle_status == "reregistered"


def test_plain_registered_unknown_closes_renewed():
    obs = _obs(status=("active", "client transfer prohibited"), expiry=date(2027, 6, 1))
    upd = lifecycle.next_state("unknown", obs, TODAY)
    assert upd.lifecycle_status == "renewed"
    assert upd.drop_date_est is None
    assert upd.expiry_date == date(2027, 6, 1)


def test_drop_offset_prefers_event_date_when_present():
    obs = _obs(status=("redemption period",), events={"redemption period": date(2026, 7, 10)})
    upd = lifecycle.next_state("unknown", obs, TODAY)
    assert upd.drop_date_est == date(2026, 8, 14)  # 2026-07-10 + 35, not today + 35
