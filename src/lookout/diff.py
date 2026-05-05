"""Pure diff engine: prior state + new observation → next state + optional event.

No I/O, no DB. The orchestrator (later) loads state, calls this, then persists the
returned `next_state` and dispatches the optional event.

Transition matrix:

  prior_risk    obs.risk      result
  ----------    --------      ------
  None state    None          no event, no state created (nothing to track yet)
  None state    risk          first_appearance, state created
  risk          None          risk_cleared, state.current_risk → None (peak preserved)
  None (cleared) None         no event (still cleared)
  None (cleared) risk         first_appearance (re-emergence after clear)
  risk          risk(higher)  risk_upgrade (peak updated if exceeded)
  risk          risk(lower)   no event (silent state update; downgrades aren't alert-worthy)
  risk          risk(same)+smaller_day   day_shift_closer
  risk          risk(same)+same/larger_day  no event (re-issuance with no change)

Threshold gating happens in the notifier, not here. This engine reports raw transitions.
"""

from dataclasses import dataclass
from typing import Optional

from lookout.config import AlertEventType, RiskLevel
from lookout.events import AlertEvent, ObservedRisk
from lookout.models import OutlookState


@dataclass(frozen=True)
class DiffResult:
    """Output of `diff_observation`.

    `next_state=None` means "do not persist anything for this (location, target_date)" —
    used only when there's no prior state and no observed risk.
    """

    next_state: Optional[OutlookState]
    event: Optional[AlertEvent]


def diff_observation(
    prior: Optional[OutlookState], obs: ObservedRisk
) -> DiffResult:
    if prior is None:
        return _from_no_state(obs)
    return _from_existing_state(prior, obs)


def _from_no_state(obs: ObservedRisk) -> DiffResult:
    if obs.risk is None:
        return DiffResult(next_state=None, event=None)

    state = OutlookState(
        location_name=obs.location_name,
        target_date=obs.target_date,
        current_risk=obs.risk.value,
        current_day=obs.outlook_day,
        peak_risk=obs.risk.value,
        first_seen_at=obs.observed_at,
        first_seen_day=obs.outlook_day,
        first_seen_risk=obs.risk.value,
        last_changed_at=obs.observed_at,
        last_observed_at=obs.observed_at,
    )
    event = AlertEvent(
        type=AlertEventType.FIRST_APPEARANCE,
        location_name=obs.location_name,
        target_date=obs.target_date,
        new_risk=obs.risk,
        new_day=obs.outlook_day,
        prior_risk=None,
        prior_day=None,
        peak_risk=obs.risk,
        observed_at=obs.observed_at,
    )
    return DiffResult(next_state=state, event=event)


def _from_existing_state(prior: OutlookState, obs: ObservedRisk) -> DiffResult:
    prior_risk = RiskLevel(prior.current_risk) if prior.current_risk else None
    prior_day = prior.current_day
    prior_peak = RiskLevel(prior.peak_risk) if prior.peak_risk else None

    new_peak = _max_risk(prior_peak, obs.risk)
    next_state = prior.model_copy(
        update={
            "last_observed_at": obs.observed_at,
            "peak_risk": new_peak.value if new_peak else None,
        }
    )

    # Case: still cleared
    if prior_risk is None and obs.risk is None:
        return DiffResult(next_state=next_state, event=None)

    # Case: risk cleared
    if prior_risk is not None and obs.risk is None:
        next_state.current_risk = None
        next_state.current_day = None
        next_state.last_changed_at = obs.observed_at
        event = AlertEvent(
            type=AlertEventType.RISK_CLEARED,
            location_name=obs.location_name,
            target_date=obs.target_date,
            new_risk=None,
            new_day=None,
            prior_risk=prior_risk,
            prior_day=prior_day,
            peak_risk=new_peak,
            observed_at=obs.observed_at,
        )
        return DiffResult(next_state=next_state, event=event)

    # Case: re-emergence after a clear
    if prior_risk is None and obs.risk is not None:
        next_state.current_risk = obs.risk.value
        next_state.current_day = obs.outlook_day
        next_state.last_changed_at = obs.observed_at
        event = AlertEvent(
            type=AlertEventType.FIRST_APPEARANCE,
            location_name=obs.location_name,
            target_date=obs.target_date,
            new_risk=obs.risk,
            new_day=obs.outlook_day,
            prior_risk=None,
            prior_day=None,
            peak_risk=new_peak,
            observed_at=obs.observed_at,
        )
        return DiffResult(next_state=next_state, event=event)

    # Both non-None from here.
    assert prior_risk is not None and obs.risk is not None

    if obs.risk > prior_risk:
        next_state.current_risk = obs.risk.value
        next_state.current_day = obs.outlook_day
        next_state.last_changed_at = obs.observed_at
        event = AlertEvent(
            type=AlertEventType.RISK_UPGRADE,
            location_name=obs.location_name,
            target_date=obs.target_date,
            new_risk=obs.risk,
            new_day=obs.outlook_day,
            prior_risk=prior_risk,
            prior_day=prior_day,
            peak_risk=new_peak,
            observed_at=obs.observed_at,
        )
        return DiffResult(next_state=next_state, event=event)

    if obs.risk < prior_risk:
        # Silent downgrade — not alert-worthy on its own.
        next_state.current_risk = obs.risk.value
        next_state.current_day = obs.outlook_day
        next_state.last_changed_at = obs.observed_at
        return DiffResult(next_state=next_state, event=None)

    # Same risk
    if obs.outlook_day < prior_day:
        next_state.current_day = obs.outlook_day
        next_state.last_changed_at = obs.observed_at
        event = AlertEvent(
            type=AlertEventType.DAY_SHIFT_CLOSER,
            location_name=obs.location_name,
            target_date=obs.target_date,
            new_risk=obs.risk,
            new_day=obs.outlook_day,
            prior_risk=prior_risk,
            prior_day=prior_day,
            peak_risk=new_peak,
            observed_at=obs.observed_at,
        )
        return DiffResult(next_state=next_state, event=event)

    # Same risk, same/larger day → re-issuance with no change.
    return DiffResult(next_state=next_state, event=None)


def _max_risk(a: Optional[RiskLevel], b: Optional[RiskLevel]) -> Optional[RiskLevel]:
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b
