"""Notification routing and dispatch.

Three concerns, separated:
    `match_rules`   — pure: which channels should receive an event?
    `format_event`  — pure: title/body for each event type
    `dispatch`      — Apprise wrapper, per-channel so one failure doesn't poison the rest

Threshold gating is keyed off `event.new_risk or event.peak_risk` so that
`risk_cleared` events (where new_risk is None) are gated by the highest risk
ever observed for that (location, target_date). Without this, a cleared alert
for a risk the user never wanted to know about would fire — defeating the
purpose of the per-location threshold.

A rule's `min_threshold` can only RAISE the effective threshold above the
location's, never lower it. Rationale: the location threshold is the user's
floor for that place ("I don't care about MRGL at home"); a rule shouldn't be
able to override that floor downward — only narrow further upward for a
specific channel/audience.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from lookout.config import (
    AlertEventType,
    LookoutConfig,
    ProductKind,
)
from lookout.events import AlertEvent
from lookout.md import MesoscaleDiscussion

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NotificationTarget:
    channel_name: str
    channel_url: str


def match_rules(
    config: LookoutConfig,
    event: AlertEvent,
    *,
    product_kind: ProductKind = ProductKind.CONVECTIVE_OUTLOOK,
) -> list[NotificationTarget]:
    """Return the channels that should receive `event`. Pure."""
    gate_risk = event.new_risk or event.peak_risk
    if gate_risk is None:
        # Diff engine never produces an event with both fields None, but be safe.
        return []

    location_threshold = config.effective_threshold(event.location_name)

    selected: dict[str, str] = {}  # channel_name → url, dedupe across rules

    for rule in config.notification_rules:
        if event.location_name not in rule.locations:
            continue
        if rule.products is not None and product_kind not in rule.products:
            continue

        rule_min = rule.min_threshold or location_threshold
        # Rule threshold can raise but not lower the location threshold.
        effective_min = rule_min if rule_min > location_threshold else location_threshold

        if gate_risk < effective_min:
            continue

        for chan_name in rule.channels:
            url = config.notification_channels.get(chan_name)
            if url is None:
                # Cross-reference validator should have caught this at config load.
                logger.warning("rule %r references unknown channel %r", rule.name, chan_name)
                continue
            selected.setdefault(chan_name, url)

    return [
        NotificationTarget(channel_name=name, channel_url=url)
        for name, url in selected.items()
    ]


def match_md_rules(config: LookoutConfig, location_name: str) -> list[NotificationTarget]:
    """Return channels that should receive an MD notification for `location_name`.

    No threshold gating — MDs are inherently about imminent severe weather and
    don't carry categorical risk levels. Filtering is by location membership and
    the rule's `products` filter only.
    """
    selected: dict[str, str] = {}
    for rule in config.notification_rules:
        if location_name not in rule.locations:
            continue
        if rule.products is not None and ProductKind.MESOSCALE_DISCUSSION not in rule.products:
            continue
        for chan_name in rule.channels:
            url = config.notification_channels.get(chan_name)
            if url is None:
                logger.warning("rule %r references unknown channel %r", rule.name, chan_name)
                continue
            selected.setdefault(chan_name, url)
    return [
        NotificationTarget(channel_name=name, channel_url=url)
        for name, url in selected.items()
    ]


def format_md(md: MesoscaleDiscussion, location_name: str) -> tuple[str, str]:
    """Render (title, body) for an MD covering `location_name`."""
    title = f"Lookout — MD #{md.mcd_id} covering {location_name}"
    lines: list[str] = [
        f"SPC Mesoscale Discussion #{md.mcd_id} now covers {location_name}.",
    ]
    if md.areas:
        lines.append(f"Areas: {md.areas}")
    if md.concerning:
        lines.append(f"Concerning: {md.concerning}")
    if md.valid_raw:
        lines.append(f"Valid: {md.valid_raw}")
    if md.summary:
        lines.append("")
        lines.append(md.summary)
    lines.append("")
    lines.append(f"Full text: {md.html_url}")
    return title, "\n".join(lines)


def format_event(event: AlertEvent) -> tuple[str, str]:
    """Render (title, body) for an event. Pure."""
    loc = event.location_name
    when = _format_when(event.target_date)

    if event.type == AlertEventType.FIRST_APPEARANCE:
        risk = _risk_value(event.new_risk)
        title = f"Lookout — {risk} risk for {loc} ({when})"
        body = (
            f"{loc} is in {risk} risk for {when}.\n"
            f"Currently in the Day {event.new_day} outlook.\n"
            f"More: {_spc_product_url(event.new_day)}"
        )
        return title, body

    if event.type == AlertEventType.RISK_UPGRADE:
        prior = _risk_value(event.prior_risk)
        new = _risk_value(event.new_risk)
        title = f"Lookout — risk upgraded {prior} → {new} for {loc} ({when})"
        body = (
            f"Risk for {loc} on {when} upgraded from {prior} to {new}.\n"
            f"Currently in the Day {event.new_day} outlook.\n"
            f"More: {_spc_product_url(event.new_day)}"
        )
        return title, body

    if event.type == AlertEventType.DAY_SHIFT_CLOSER:
        risk = _risk_value(event.new_risk)
        title = f"Lookout — {risk} for {loc} moved to Day {event.new_day} ({when})"
        body = (
            f"The {risk} risk for {loc} on {when} has moved closer:\n"
            f"  Day {event.prior_day} → Day {event.new_day}\n"
            f"More: {_spc_product_url(event.new_day)}"
        )
        return title, body

    if event.type == AlertEventType.RISK_CLEARED:
        peak = _risk_value(event.peak_risk)
        title = f"Lookout — risk cleared for {loc} ({when})"
        body = (
            f"The previously-watched risk for {loc} on {when} has been removed.\n"
            f"Peak risk seen: {peak}."
        )
        return title, body

    return f"Lookout — {event.type.value}", f"{loc}, {when}"


def dispatch(targets: list[NotificationTarget], title: str, body: str) -> int:
    """Send to each channel via Apprise. Returns count of successful dispatches.

    Iterates per-channel rather than batching so one failing URL doesn't
    suppress notifications to the others.
    """
    if not targets:
        return 0

    import apprise

    success = 0
    for target in targets:
        ap = apprise.Apprise()
        ap.add(target.channel_url)
        try:
            if ap.notify(title=title, body=body):
                success += 1
            else:
                logger.warning("Apprise notify returned False for %s", target.channel_name)
        except Exception:
            logger.exception("Apprise dispatch error for %s", target.channel_name)
    return success


class Notifier:
    """Glues match_rules + format_event + dispatch together with optional dry-run."""

    def __init__(self, config: LookoutConfig, *, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run

    def handle(
        self,
        event: AlertEvent,
        *,
        product_kind: ProductKind = ProductKind.CONVECTIVE_OUTLOOK,
    ) -> int:
        """Process one event: match → format → dispatch. Returns channels notified."""
        targets = match_rules(self.config, event, product_kind=product_kind)
        if not targets:
            return 0
        title, body = format_event(event)

        if self.dry_run:
            print(f"[DRY-RUN] {title}")
            for t in targets:
                print(f"  → {t.channel_name} ({t.channel_url})")
            for line in body.splitlines():
                print(f"    | {line}")
            return len(targets)

        return dispatch(targets, title, body)

    def handle_md(self, md: MesoscaleDiscussion, location_name: str) -> int:
        """Process one (MD, location) pair: match → format → dispatch."""
        targets = match_md_rules(self.config, location_name)
        if not targets:
            return 0
        title, body = format_md(md, location_name)

        if self.dry_run:
            print(f"[DRY-RUN MD] {title}")
            for t in targets:
                print(f"  → {t.channel_name} ({t.channel_url})")
            for line in body.splitlines():
                print(f"    | {line}")
            return len(targets)

        return dispatch(targets, title, body)


def _format_when(d) -> str:
    # Avoid %-d / %#d strftime quirks — assemble manually for portability.
    return f"{d.strftime('%a %b')} {d.day}"


def _risk_value(risk) -> str:
    return risk.value if risk is not None else "?"


def _spc_product_url(day: Optional[int]) -> str:
    if day in (1, 2, 3):
        return f"https://www.spc.noaa.gov/products/outlook/day{day}otlk.html"
    return "https://www.spc.noaa.gov/products/exper/day4-8/"
