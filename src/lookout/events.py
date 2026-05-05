"""Event types produced by the diff engine and consumed by the notifier.

These are pure data shapes — no I/O, no DB. The diff engine constructs them; the
notifier filters/dispatches them; tests can assert against them directly.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from lookout.config import AlertEventType, RiskLevel


@dataclass(frozen=True)
class ObservedRisk:
    """Result of evaluating one location against one outlook product's polygons.

    `risk=None` means the location was outside every risk polygon for this product
    (i.e. no risk for this target_date according to this product's current state).
    """

    location_name: str
    target_date: date
    risk: Optional[RiskLevel]
    outlook_day: int  # 1-8, the Day-N product the observation came from
    observed_at: datetime


@dataclass(frozen=True)
class AlertEvent:
    """A user-facing event produced by the diff engine."""

    type: AlertEventType
    location_name: str
    target_date: date

    # State after this observation
    new_risk: Optional[RiskLevel]
    new_day: Optional[int]

    # State before this observation (None on first_appearance)
    prior_risk: Optional[RiskLevel]
    prior_day: Optional[int]

    # Highest risk ever observed for this (location, target_date). Used by the
    # notifier to threshold-gate `risk_cleared` events: we suppress the clear
    # alert if the user never had reason to know about the risk in the first place.
    peak_risk: Optional[RiskLevel]

    observed_at: datetime
