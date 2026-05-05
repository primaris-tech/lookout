"""Tests for the pure diff engine. One test per branch of the transition matrix."""

from datetime import date, datetime
from typing import Optional

import pytest

from lookout.config import AlertEventType, RiskLevel
from lookout.diff import diff_observation
from lookout.events import ObservedRisk
from lookout.models import OutlookState

LOC = "home"
TARGET = date(2026, 5, 7)
T0 = datetime(2026, 5, 4, 12, 0, 0)
T1 = datetime(2026, 5, 4, 13, 0, 0)


def _obs(risk: Optional[RiskLevel], day: int, at: datetime = T1) -> ObservedRisk:
    return ObservedRisk(
        location_name=LOC,
        target_date=TARGET,
        risk=risk,
        outlook_day=day,
        observed_at=at,
    )


def _state(
    *,
    current_risk: Optional[RiskLevel],
    current_day: Optional[int],
    peak_risk: Optional[RiskLevel] = None,
    first_seen_risk: RiskLevel = RiskLevel.MRGL,
    first_seen_day: int = 5,
) -> OutlookState:
    """Helper for building a prior state row inline."""
    if peak_risk is None:
        peak_risk = current_risk if current_risk else first_seen_risk
    return OutlookState(
        location_name=LOC,
        target_date=TARGET,
        current_risk=current_risk.value if current_risk else None,
        current_day=current_day,
        peak_risk=peak_risk.value if peak_risk else None,
        first_seen_at=T0,
        first_seen_day=first_seen_day,
        first_seen_risk=first_seen_risk.value,
        last_changed_at=T0,
        last_observed_at=T0,
    )


def test_no_state_no_risk_does_nothing():
    result = diff_observation(None, _obs(risk=None, day=5))
    assert result.next_state is None
    assert result.event is None


def test_no_state_with_risk_emits_first_appearance():
    result = diff_observation(None, _obs(risk=RiskLevel.MRGL, day=5))
    assert result.event is not None
    assert result.event.type == AlertEventType.FIRST_APPEARANCE
    assert result.event.new_risk == RiskLevel.MRGL
    assert result.event.new_day == 5
    assert result.event.prior_risk is None
    assert result.next_state is not None
    assert result.next_state.current_risk == "MRGL"
    assert result.next_state.peak_risk == "MRGL"
    assert result.next_state.first_seen_day == 5


def test_same_risk_same_day_is_silent():
    prior = _state(current_risk=RiskLevel.MRGL, current_day=5)
    result = diff_observation(prior, _obs(risk=RiskLevel.MRGL, day=5))
    assert result.event is None
    assert result.next_state is not None
    assert result.next_state.last_observed_at == T1
    assert result.next_state.current_risk == "MRGL"


def test_same_risk_smaller_day_emits_day_shift_closer():
    prior = _state(current_risk=RiskLevel.MRGL, current_day=5)
    result = diff_observation(prior, _obs(risk=RiskLevel.MRGL, day=3))
    assert result.event is not None
    assert result.event.type == AlertEventType.DAY_SHIFT_CLOSER
    assert result.event.prior_day == 5
    assert result.event.new_day == 3
    assert result.event.new_risk == RiskLevel.MRGL
    assert result.next_state.current_day == 3


def test_higher_risk_emits_upgrade_and_updates_peak():
    prior = _state(current_risk=RiskLevel.MRGL, current_day=4, peak_risk=RiskLevel.MRGL)
    result = diff_observation(prior, _obs(risk=RiskLevel.SLGT, day=3))
    assert result.event is not None
    assert result.event.type == AlertEventType.RISK_UPGRADE
    assert result.event.prior_risk == RiskLevel.MRGL
    assert result.event.new_risk == RiskLevel.SLGT
    assert result.event.peak_risk == RiskLevel.SLGT
    assert result.next_state.current_risk == "SLGT"
    assert result.next_state.peak_risk == "SLGT"


def test_lower_risk_silently_updates_state_but_preserves_peak():
    prior = _state(current_risk=RiskLevel.SLGT, current_day=3, peak_risk=RiskLevel.SLGT)
    result = diff_observation(prior, _obs(risk=RiskLevel.MRGL, day=3))
    assert result.event is None
    assert result.next_state.current_risk == "MRGL"
    assert result.next_state.peak_risk == "SLGT"  # peak preserved


def test_risk_cleared_when_state_had_risk():
    prior = _state(current_risk=RiskLevel.SLGT, current_day=3, peak_risk=RiskLevel.ENH)
    result = diff_observation(prior, _obs(risk=None, day=3))
    assert result.event is not None
    assert result.event.type == AlertEventType.RISK_CLEARED
    assert result.event.prior_risk == RiskLevel.SLGT
    assert result.event.new_risk is None
    assert result.event.peak_risk == RiskLevel.ENH  # peak surfaced for threshold gating
    assert result.next_state.current_risk is None
    assert result.next_state.current_day is None
    assert result.next_state.peak_risk == "ENH"


def test_already_cleared_with_no_risk_is_silent():
    prior = _state(
        current_risk=None, current_day=None, peak_risk=RiskLevel.SLGT
    )
    result = diff_observation(prior, _obs(risk=None, day=3))
    assert result.event is None
    assert result.next_state.current_risk is None


def test_re_emergence_after_clear_is_first_appearance():
    prior = _state(
        current_risk=None, current_day=None, peak_risk=RiskLevel.MRGL
    )
    result = diff_observation(prior, _obs(risk=RiskLevel.SLGT, day=2))
    assert result.event is not None
    assert result.event.type == AlertEventType.FIRST_APPEARANCE
    assert result.event.prior_risk is None
    assert result.event.new_risk == RiskLevel.SLGT
    assert result.event.peak_risk == RiskLevel.SLGT  # peak escalated
    assert result.next_state.current_risk == "SLGT"
    assert result.next_state.peak_risk == "SLGT"
