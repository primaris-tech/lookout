"""SQLModel table definitions for Lookout state.

State is keyed by (location_name, target_date) — NOT by outlook product. Wednesday's
threat appearing as Day 4 → 3 → 2 → 1 over four products is one event, four products,
and one row in `outlook_state`.

`peak_risk` tracks the highest categorical risk ever observed for the row's lifetime.
The notifier uses it to decide whether `risk_cleared` events meet the user's threshold:
without peak tracking, "MRGL → SLGT → MRGL → cleared" would suppress the clear alert
even though the user already knew about the SLGT.
"""

from datetime import date, datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class OutlookState(SQLModel, table=True):
    __tablename__ = "outlook_state"

    location_name: str = Field(primary_key=True, max_length=100)
    target_date: date = Field(primary_key=True)

    # Risk levels are stored as their string value (e.g. "SLGT") rather than an enum
    # column so the DB stays portable across SQLite/Postgres without dialect quirks.
    current_risk: Optional[str] = None    # None means cleared
    current_day: Optional[int] = None     # 1-8, None when cleared
    peak_risk: Optional[str] = None       # highest risk ever observed for this row

    first_seen_at: datetime
    first_seen_day: int
    first_seen_risk: str                  # always non-null: row only created on first risk

    last_changed_at: datetime
    last_observed_at: datetime


class MesoscaleDiscussionAlert(SQLModel, table=True):
    """Idempotency log for MD alerts: one row per (mcd_id, location) we've notified for."""

    __tablename__ = "mesoscale_discussion_alert"

    mcd_id: str = Field(primary_key=True, max_length=32)
    location_name: str = Field(primary_key=True, max_length=100)
    alerted_at: datetime


class FetchFailureState(SQLModel, table=True):
    """Singleton row tracking the current consecutive-failure window for the meta-alert."""

    __tablename__ = "fetch_failure_state"

    id: int = Field(default=1, primary_key=True)
    first_failure_at: Optional[datetime] = None
    last_meta_alert_at: Optional[datetime] = None


class LocationSeed(SQLModel, table=True):
    """Marker that a location has completed its first-run silent seed.

    Without this, a location with no risk anywhere would never have an OutlookState
    row created for it, and would re-enter "seed mode" every cycle, re-emitting the
    initial summary on every poll.
    """

    __tablename__ = "location_seed"

    location_name: str = Field(primary_key=True, max_length=100)
    seeded_at: datetime
