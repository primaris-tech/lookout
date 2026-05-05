"""Configuration schema for Lookout.

The whole config — locations, products, channels, rules — is parsed from a single
YAML file into typed Pydantic models. Validation includes cross-references (rules
must point at locations and channels that actually exist) and threshold resolution
helpers used by the alert pipeline.
"""

from enum import Enum
from functools import total_ordering
from pathlib import Path
from typing import Annotated, Optional

import yaml
from pydantic import BaseModel, BeforeValidator, Field, model_validator


@total_ordering
class RiskLevel(Enum):
    """SPC categorical risk levels, ordered from least to most severe.

    Severity comparisons (`>=`, `<`, etc.) follow declaration order, NOT string order.
    """

    TSTM = "TSTM"
    MRGL = "MRGL"
    SLGT = "SLGT"
    ENH = "ENH"
    MDT = "MDT"
    HIGH = "HIGH"

    def __lt__(self, other: "RiskLevel") -> bool:
        if not isinstance(other, RiskLevel):
            return NotImplemented
        members = list(RiskLevel)
        return members.index(self) < members.index(other)


def _parse_risk_level(v: object) -> object:
    if isinstance(v, str):
        return RiskLevel[v.upper()]
    return v


# Accepts strings like "MRGL" or "mrgl" in YAML and coerces to the enum.
RiskLevelField = Annotated[RiskLevel, BeforeValidator(_parse_risk_level)]


class AlertEventType(str, Enum):
    FIRST_APPEARANCE = "first_appearance"
    RISK_UPGRADE = "risk_upgrade"
    DAY_SHIFT_CLOSER = "day_shift_closer"
    RISK_CLEARED = "risk_cleared"


class Location(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    threshold: Optional[RiskLevelField] = None


class ConvectiveOutlookConfig(BaseModel):
    days: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5, 6, 7, 8])
    notify_on: list[AlertEventType] = Field(
        default_factory=lambda: [
            AlertEventType.FIRST_APPEARANCE,
            AlertEventType.RISK_UPGRADE,
            AlertEventType.DAY_SHIFT_CLOSER,
            AlertEventType.RISK_CLEARED,
        ]
    )

    @model_validator(mode="after")
    def _validate_days(self) -> "ConvectiveOutlookConfig":
        if not self.days or any(d < 1 or d > 8 for d in self.days):
            raise ValueError("convective_outlook.days must be a non-empty subset of [1..8]")
        # Normalize: dedupe + sort
        object.__setattr__(self, "days", sorted(set(self.days)))
        return self


class MesoscaleDiscussionConfig(BaseModel):
    enabled: bool = True


class ProbabilisticConfig(BaseModel):
    enabled: bool = False


class ProductsConfig(BaseModel):
    convective_outlook: ConvectiveOutlookConfig = Field(default_factory=ConvectiveOutlookConfig)
    mesoscale_discussion: MesoscaleDiscussionConfig = Field(default_factory=MesoscaleDiscussionConfig)
    probabilistic: ProbabilisticConfig = Field(default_factory=ProbabilisticConfig)


class ProductKind(str, Enum):
    CONVECTIVE_OUTLOOK = "convective_outlook"
    MESOSCALE_DISCUSSION = "mesoscale_discussion"
    PROBABILISTIC = "probabilistic"


class NotificationRule(BaseModel):
    name: str
    locations: list[str]
    channels: list[str]
    products: Optional[list[ProductKind]] = None
    min_threshold: Optional[RiskLevelField] = None


class PollingConfig(BaseModel):
    interval_minutes: int = Field(default=10, ge=1)
    meta_alert_after_minutes: int = Field(default=60, ge=5)


class LookoutConfig(BaseModel):
    user_agent_contact: str
    alert_threshold: RiskLevelField = RiskLevel.MRGL
    locations: dict[str, Location]
    products: ProductsConfig = Field(default_factory=ProductsConfig)
    notification_channels: dict[str, str]
    notification_rules: list[NotificationRule] = Field(default_factory=list)
    polling: PollingConfig = Field(default_factory=PollingConfig)
    state_retention_days: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def _validate_references(self) -> "LookoutConfig":
        if not self.locations:
            raise ValueError("at least one location must be configured")

        loc_keys = set(self.locations)
        chan_keys = set(self.notification_channels)
        for rule in self.notification_rules:
            unknown_locs = set(rule.locations) - loc_keys
            if unknown_locs:
                raise ValueError(
                    f"rule {rule.name!r} references unknown locations: {sorted(unknown_locs)}"
                )
            unknown_chans = set(rule.channels) - chan_keys
            if unknown_chans:
                raise ValueError(
                    f"rule {rule.name!r} references unknown channels: {sorted(unknown_chans)}"
                )
        return self

    def effective_threshold(self, location_name: str) -> RiskLevel:
        """Resolve threshold for a location, falling back to global default."""
        loc = self.locations[location_name]
        return loc.threshold if loc.threshold is not None else self.alert_threshold


def load_config(path: Path | str) -> LookoutConfig:
    raw = yaml.safe_load(Path(path).read_text())
    if raw is None:
        raise ValueError(f"config file {path} is empty")
    return LookoutConfig.model_validate(raw)
