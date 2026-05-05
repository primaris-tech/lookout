"""Tests for the notifier's pure functions: rule matching, threshold gating, formatting."""

from datetime import date, datetime
from typing import Optional

import pytest

from lookout.config import (
    AlertEventType,
    Location,
    LookoutConfig,
    NotificationRule,
    PollingConfig,
    ProductKind,
    ProductsConfig,
    RiskLevel,
)
from lookout.events import AlertEvent
from lookout.notifier import format_event, match_rules


# --- helpers ---

def _config(
    *,
    alert_threshold: RiskLevel = RiskLevel.MRGL,
    locations: Optional[dict] = None,
    channels: Optional[dict] = None,
    rules: Optional[list] = None,
) -> LookoutConfig:
    return LookoutConfig(
        user_agent_contact="tester@example.com",
        alert_threshold=alert_threshold,
        locations=locations or {
            "home": Location(lat=35.5, lon=-97.0),
        },
        notification_channels=channels or {
            "phone": "ntfys://test-topic",
        },
        notification_rules=rules or [],
        products=ProductsConfig(),
        polling=PollingConfig(),
    )


def _event(
    *,
    type: AlertEventType,
    location: str = "home",
    new_risk: Optional[RiskLevel] = RiskLevel.MRGL,
    new_day: Optional[int] = 3,
    prior_risk: Optional[RiskLevel] = None,
    prior_day: Optional[int] = None,
    peak_risk: Optional[RiskLevel] = None,
) -> AlertEvent:
    if peak_risk is None:
        peak_risk = new_risk
    return AlertEvent(
        type=type,
        location_name=location,
        target_date=date(2026, 5, 7),
        new_risk=new_risk,
        new_day=new_day,
        prior_risk=prior_risk,
        prior_day=prior_day,
        peak_risk=peak_risk,
        observed_at=datetime(2026, 5, 5, 12, 0),
    )


# --- rule matching: location filter ---

def test_rule_skipped_when_location_not_in_rule():
    cfg = _config(
        rules=[NotificationRule(name="parents-only", locations=["parents"], channels=["phone"])],
        locations={"home": Location(lat=0, lon=0), "parents": Location(lat=1, lon=1)},
    )
    targets = match_rules(cfg, _event(type=AlertEventType.FIRST_APPEARANCE, location="home"))
    assert targets == []


# --- rule matching: threshold gating ---

def test_below_location_threshold_does_not_fire():
    cfg = _config(
        alert_threshold=RiskLevel.SLGT,
        rules=[NotificationRule(name="all", locations=["home"], channels=["phone"])],
    )
    event = _event(type=AlertEventType.FIRST_APPEARANCE, new_risk=RiskLevel.MRGL)
    assert match_rules(cfg, event) == []


def test_at_location_threshold_fires():
    cfg = _config(
        alert_threshold=RiskLevel.MRGL,
        rules=[NotificationRule(name="all", locations=["home"], channels=["phone"])],
    )
    event = _event(type=AlertEventType.FIRST_APPEARANCE, new_risk=RiskLevel.MRGL)
    assert {t.channel_name for t in match_rules(cfg, event)} == {"phone"}


def test_per_location_threshold_used_when_set():
    cfg = _config(
        alert_threshold=RiskLevel.MRGL,
        locations={"home": Location(lat=0, lon=0, threshold=RiskLevel.SLGT)},
        rules=[NotificationRule(name="all", locations=["home"], channels=["phone"])],
    )
    # MRGL is below the location's SLGT override → no fire.
    event = _event(type=AlertEventType.FIRST_APPEARANCE, new_risk=RiskLevel.MRGL)
    assert match_rules(cfg, event) == []


# --- rule min_threshold can raise but not lower ---

def test_rule_min_threshold_raises_above_location():
    cfg = _config(
        alert_threshold=RiskLevel.MRGL,
        rules=[
            NotificationRule(
                name="strict",
                locations=["home"],
                channels=["phone"],
                min_threshold=RiskLevel.ENH,
            )
        ],
    )
    event_slgt = _event(type=AlertEventType.FIRST_APPEARANCE, new_risk=RiskLevel.SLGT)
    event_enh = _event(type=AlertEventType.FIRST_APPEARANCE, new_risk=RiskLevel.ENH)
    assert match_rules(cfg, event_slgt) == []
    assert {t.channel_name for t in match_rules(cfg, event_enh)} == {"phone"}


def test_rule_min_threshold_cannot_lower_below_location():
    cfg = _config(
        alert_threshold=RiskLevel.SLGT,
        rules=[
            NotificationRule(
                name="loose",
                locations=["home"],
                channels=["phone"],
                min_threshold=RiskLevel.MRGL,  # lower than location's SLGT
            )
        ],
    )
    # MRGL ≥ rule.min_threshold but < location threshold; must NOT fire.
    event = _event(type=AlertEventType.FIRST_APPEARANCE, new_risk=RiskLevel.MRGL)
    assert match_rules(cfg, event) == []


# --- risk_cleared peak gating ---

def test_risk_cleared_fires_when_peak_meets_threshold():
    cfg = _config(
        alert_threshold=RiskLevel.SLGT,
        rules=[NotificationRule(name="all", locations=["home"], channels=["phone"])],
    )
    event = _event(
        type=AlertEventType.RISK_CLEARED,
        new_risk=None,
        new_day=None,
        prior_risk=RiskLevel.SLGT,
        prior_day=3,
        peak_risk=RiskLevel.SLGT,
    )
    assert {t.channel_name for t in match_rules(cfg, event)} == {"phone"}


def test_risk_cleared_suppressed_when_peak_below_threshold():
    cfg = _config(
        alert_threshold=RiskLevel.SLGT,
        rules=[NotificationRule(name="all", locations=["home"], channels=["phone"])],
    )
    # Peak only ever reached MRGL — user never wanted MRGL alerts, so don't tell
    # them about the clear either.
    event = _event(
        type=AlertEventType.RISK_CLEARED,
        new_risk=None,
        new_day=None,
        prior_risk=RiskLevel.MRGL,
        prior_day=3,
        peak_risk=RiskLevel.MRGL,
    )
    assert match_rules(cfg, event) == []


# --- products filter ---

def test_rule_products_filter_excludes_other_products():
    cfg = _config(
        rules=[
            NotificationRule(
                name="md-only",
                locations=["home"],
                channels=["phone"],
                products=[ProductKind.MESOSCALE_DISCUSSION],
            )
        ],
    )
    event = _event(type=AlertEventType.FIRST_APPEARANCE)
    assert match_rules(cfg, event, product_kind=ProductKind.CONVECTIVE_OUTLOOK) == []


# --- channel deduplication ---

def test_channels_deduplicated_across_matching_rules():
    cfg = _config(
        rules=[
            NotificationRule(name="rule-a", locations=["home"], channels=["phone"]),
            NotificationRule(name="rule-b", locations=["home"], channels=["phone"]),
        ],
    )
    event = _event(type=AlertEventType.FIRST_APPEARANCE)
    targets = match_rules(cfg, event)
    assert len(targets) == 1
    assert targets[0].channel_name == "phone"


# --- formatting ---

def test_format_first_appearance_includes_risk_location_and_day():
    event = _event(type=AlertEventType.FIRST_APPEARANCE, new_risk=RiskLevel.SLGT, new_day=3)
    title, body = format_event(event)
    assert "SLGT" in title
    assert "home" in title
    assert "Day 3" in body


def test_format_risk_upgrade_includes_both_risks():
    event = _event(
        type=AlertEventType.RISK_UPGRADE,
        new_risk=RiskLevel.SLGT,
        prior_risk=RiskLevel.MRGL,
        new_day=3,
    )
    title, body = format_event(event)
    assert "MRGL" in title and "SLGT" in title
    assert "MRGL" in body and "SLGT" in body


def test_format_day_shift_closer_shows_day_transition():
    event = _event(
        type=AlertEventType.DAY_SHIFT_CLOSER,
        new_risk=RiskLevel.SLGT,
        new_day=2,
        prior_day=4,
    )
    title, body = format_event(event)
    assert "Day 2" in title
    assert "Day 4" in body and "Day 2" in body


def test_format_risk_cleared_shows_peak():
    event = _event(
        type=AlertEventType.RISK_CLEARED,
        new_risk=None,
        new_day=None,
        prior_risk=RiskLevel.SLGT,
        prior_day=3,
        peak_risk=RiskLevel.ENH,
    )
    title, body = format_event(event)
    assert "cleared" in title.lower()
    assert "ENH" in body
