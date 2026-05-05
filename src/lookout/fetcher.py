"""Orchestration: pull configured products from SPC, evaluate every location, and
emit one ObservedRisk per (location, outlook_day) pair.

Pure-ish: takes an SPCClient and a config, returns a list of observations + any
fetch errors encountered. Does not touch the database. The caller persists state
and dispatches events.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from lookout.config import LookoutConfig
from lookout.events import ObservedRisk
from lookout.spc import (
    URLS_CATEGORICAL,
    URLS_PROBABILISTIC,
    FetchError,
    Outlook,
    SPCClient,
    evaluate_location,
)


@dataclass
class FetchResult:
    observations: list[ObservedRisk]
    fetched: list[Outlook]            # outlooks that returned new data
    skipped_unchanged: list[int]      # outlook days that returned 304-equivalent (unchanged hash)
    errors: dict[int, str]            # outlook day → error message
    attempted: int = 0                # total fetch attempts this cycle (for failure-rate calc)


def run_fetch_once(
    config: LookoutConfig, client: SPCClient, now: datetime
) -> FetchResult:
    observations: list[ObservedRisk] = []
    fetched: list[Outlook] = []
    skipped: list[int] = []
    errors: dict[int, str] = {}

    attempted = 0
    for day in config.products.convective_outlook.days:
        outlook: Optional[Outlook]
        if day in URLS_CATEGORICAL:
            attempted += 1
            try:
                outlook = client.fetch_categorical(day)
            except FetchError as e:
                errors[day] = str(e)
                continue
        elif day in URLS_PROBABILISTIC:
            attempted += 1
            try:
                outlook = client.fetch_probabilistic(day)
            except FetchError as e:
                errors[day] = str(e)
                continue
        else:
            continue

        if outlook is None:
            skipped.append(day)
            continue

        fetched.append(outlook)
        for loc_name, loc in config.locations.items():
            risk = evaluate_location(outlook, lat=loc.lat, lon=loc.lon)
            observations.append(
                ObservedRisk(
                    location_name=loc_name,
                    target_date=outlook.target_date,
                    risk=risk,
                    outlook_day=day,
                    observed_at=now,
                )
            )

    return FetchResult(
        observations=observations,
        fetched=fetched,
        skipped_unchanged=skipped,
        errors=errors,
        attempted=attempted,
    )
