"""Polling loop, first-run seed handling, and fetch-failure meta-alerts.

Composition (top-down):
    run_polling_loop          — long-running; signal-driven shutdown
        run_one_cycle         — one fetch + diff + dispatch + failure tracking
            run_fetch_once    — HTTP, parse, evaluate per location → FetchResult
            process_cycle     — diff + persist state + dispatch (or seed-summary)
            track_fetch_failures  — meta-alert lifecycle

`--fetch-once` and the loop's per-iteration body share `run_one_cycle`, so behavior
is identical between one-shot and continuous modes.

Dry-run skips both notification dispatch AND DB commits, so dry-run is fully
read-only — you can preview the same cycle as many times as you want without
"consuming" the first-run seed.
"""

import logging
import signal
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import shapely.geometry as shp
from sqlalchemy import delete
from sqlmodel import select

from lookout.config import LookoutConfig
from lookout.db import init_db, make_engine, session
from lookout.diff import diff_observation
from lookout.events import AlertEvent
from lookout.fetcher import FetchResult, run_fetch_once
from lookout.md import MesoscaleDiscussion, fetch_active_mds
from lookout.models import (
    FetchFailureState,
    LocationSeed,
    MesoscaleDiscussionAlert,
    OutlookState,
)
from lookout.notifier import (
    NotificationTarget,
    Notifier,
    dispatch,
    match_rules,
)
from lookout.spc import SPCClient

logger = logging.getLogger(__name__)


def _naive_utc(dt: datetime) -> datetime:
    """Convert any datetime to tz-naive UTC. SQLite drops tzinfo on round-trip,
    so we standardize on naive UTC for any timestamp that touches the DB to keep
    comparisons (e.g., `now - state.first_failure_at`) well-typed.
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass
class CycleResult:
    fetch: FetchResult
    events_generated: int
    notifications_sent: int
    seeded_locations: list[str]
    md_alerts_sent: int = 0
    mds_processed: int = 0


def identify_unseeded_locations(engine, config: LookoutConfig) -> set[str]:
    """Return locations from config that have no LocationSeed row yet."""
    with session(engine) as sess:
        seeded = set(sess.exec(select(LocationSeed.location_name)).all())
    return set(config.locations) - seeded


def process_cycle(
    config: LookoutConfig,
    notifier: Notifier,
    engine,
    fetch: FetchResult,
    now: datetime,
    *,
    seed_locations: set[str],
    dry_run: bool = False,
) -> CycleResult:
    """Apply diff results, dispatch normal events, emit summaries for seeding locations.

    On dry_run, no DB commit happens (state stays as-is) and dispatch is skipped.
    """
    now = _naive_utc(now)
    events_generated = 0
    notifications_sent = 0
    seed_events: dict[str, list[AlertEvent]] = {loc: [] for loc in seed_locations}

    with session(engine) as sess:
        for obs in fetch.observations:
            prior = sess.get(OutlookState, (obs.location_name, obs.target_date))
            diff = diff_observation(prior, obs)
            if diff.event:
                events_generated += 1
                if obs.location_name in seed_locations:
                    seed_events[obs.location_name].append(diff.event)
                else:
                    notifications_sent += notifier.handle(diff.event)
            if diff.next_state is not None:
                sess.merge(diff.next_state)

        for loc in seed_locations:
            sess.merge(LocationSeed(location_name=loc, seeded_at=now))

        if not dry_run:
            sess.commit()

    for loc, events in seed_events.items():
        notifications_sent += emit_seed_summary(notifier, loc, events)

    return CycleResult(
        fetch=fetch,
        events_generated=events_generated,
        notifications_sent=notifications_sent,
        seeded_locations=sorted(seed_locations),
    )


def emit_seed_summary(notifier: Notifier, location: str, events: list[AlertEvent]) -> int:
    """Send one summary message per seeded location instead of N first_appearance alerts.

    Channels are the union of those that would have received any individual event,
    so threshold and product gating are still respected — we just collapse the
    payload into a single message.
    """
    if not events:
        return 0

    all_targets: dict[str, str] = {}
    relevant: list[AlertEvent] = []
    for event in events:
        targets = match_rules(notifier.config, event)
        if targets:
            relevant.append(event)
            for t in targets:
                all_targets.setdefault(t.channel_name, t.channel_url)

    if not relevant:
        return 0

    title = f"Lookout — initial summary for {location}"
    body_lines = [f"Now watching {location}. Currently active outlooks:"]
    for event in sorted(relevant, key=lambda e: e.target_date):
        risk = (event.new_risk or event.peak_risk).value
        d = event.target_date
        when = f"{d.strftime('%a %b')} {d.day}"
        body_lines.append(f"  • {risk} for {when} (Day {event.new_day})")
    body = "\n".join(body_lines)

    targets_list = [
        NotificationTarget(channel_name=name, channel_url=url)
        for name, url in all_targets.items()
    ]

    if notifier.dry_run:
        print(f"[DRY-RUN SUMMARY] {title}")
        for t in targets_list:
            print(f"  → {t.channel_name} ({t.channel_url})")
        for line in body.splitlines():
            print(f"    | {line}")
        return len(targets_list)

    return dispatch(targets_list, title, body)


def track_fetch_failures(
    config: LookoutConfig,
    notifier: Notifier,
    engine,
    fetch: FetchResult,
    now: datetime,
    *,
    dry_run: bool = False,
) -> None:
    """Update FetchFailureState; emit meta-alert on prolonged total failure or recovery.

    Failure window opens when *every* attempted product errors. It resets the moment
    *any* product succeeds (fetched-or-unchanged). The meta-alert fires at most once
    per `meta_alert_after_minutes` window, and a recovery alert fires only if the
    user was previously alerted.
    """
    if fetch.attempted == 0:
        return  # nothing was attempted (e.g., empty config); not a failure signal
    now = _naive_utc(now)
    all_errored = len(fetch.errors) == fetch.attempted
    threshold = timedelta(minutes=config.polling.meta_alert_after_minutes)

    with session(engine) as sess:
        state = sess.get(FetchFailureState, 1)
        if state is None:
            state = FetchFailureState(id=1)

        if all_errored:
            if state.first_failure_at is None:
                state.first_failure_at = now
                logger.warning("fetch failure window opened")

            duration = now - state.first_failure_at
            if duration >= threshold:
                last = state.last_meta_alert_at
                if last is None or (now - last) >= threshold:
                    fire_meta_alert(notifier, duration, fetch.errors)
                    state.last_meta_alert_at = now
        else:
            if state.first_failure_at is not None:
                duration = now - state.first_failure_at
                if state.last_meta_alert_at is not None:
                    fire_recovery_alert(notifier, duration)
                state.first_failure_at = None
                state.last_meta_alert_at = None
                logger.info("fetch failure window closed")

        sess.merge(state)
        if not dry_run:
            sess.commit()


def fire_meta_alert(notifier: Notifier, duration: timedelta, errors: dict[int, str]) -> None:
    minutes = int(duration.total_seconds() // 60)
    title = f"Lookout — SPC unreachable ({minutes}m)"
    lines = [
        f"Lookout has been unable to fetch SPC products for {minutes} minutes.",
        "Most recent errors:",
    ]
    for day, err in sorted(errors.items()):
        lines.append(f"  Day {day}: {err}")
    _broadcast(notifier, title, "\n".join(lines))


def fire_recovery_alert(notifier: Notifier, duration: timedelta) -> None:
    minutes = int(duration.total_seconds() // 60)
    title = "Lookout — SPC reachable again"
    body = f"After {minutes} minute(s) of failures, Lookout successfully fetched SPC products."
    _broadcast(notifier, title, body)


def _broadcast(notifier: Notifier, title: str, body: str) -> None:
    """Send a meta-alert to every channel referenced by any notification rule."""
    channel_names: set[str] = set()
    for rule in notifier.config.notification_rules:
        channel_names.update(rule.channels)
    targets = [
        NotificationTarget(channel_name=name, channel_url=notifier.config.notification_channels[name])
        for name in sorted(channel_names)
        if name in notifier.config.notification_channels
    ]
    if not targets:
        logger.warning("meta-alert suppressed: no channels configured in any rule")
        return

    if notifier.dry_run:
        print(f"[DRY-RUN BROADCAST] {title}")
        for t in targets:
            print(f"  → {t.channel_name}")
        for line in body.splitlines():
            print(f"    | {line}")
        return

    dispatch(targets, title, body)


def process_md_cycle(
    config: LookoutConfig,
    notifier: Notifier,
    engine,
    mds: list[MesoscaleDiscussion],
    now: datetime,
    *,
    dry_run: bool = False,
) -> tuple[int, int]:
    """For each MD covering each location, dispatch (idempotent via DB).

    Returns (mds_processed, alerts_sent). MDs bypass the outlook seed mechanism —
    by their nature they're imminent-weather alerts and the user wants them on
    first detection regardless of install age.
    """
    if not mds:
        return 0, 0

    now = _naive_utc(now)
    alerts_sent = 0
    with session(engine) as sess:
        for md in mds:
            for loc_name, loc in config.locations.items():
                if not md.polygon.intersects(shp.Point(loc.lon, loc.lat)):
                    continue
                key = (md.mcd_id, loc_name)
                if sess.get(MesoscaleDiscussionAlert, key) is not None:
                    continue  # already alerted on this (MD, location)
                alerts_sent += notifier.handle_md(md, loc_name)
                sess.merge(MesoscaleDiscussionAlert(
                    mcd_id=md.mcd_id,
                    location_name=loc_name,
                    alerted_at=now,
                ))
        if not dry_run:
            sess.commit()

    return len(mds), alerts_sent


def prune_state(engine, retention_days: int, now: datetime) -> tuple[int, int]:
    """Delete state rows older than `retention_days`.

    Returns (outlook_state_rows_deleted, md_alert_rows_deleted).

    Boundary: rows are deleted strictly when their key date/datetime is BEFORE
    `now - retention_days`. Equal-to-cutoff is kept. `LocationSeed` and
    `FetchFailureState` are bounded already (one row per location, singleton)
    and don't need pruning.
    """
    now = _naive_utc(now)
    cutoff_dt = now - timedelta(days=retention_days)
    cutoff_date = cutoff_dt.date()

    with session(engine) as sess:
        outlook_stmt = delete(OutlookState).where(OutlookState.target_date < cutoff_date)
        md_stmt = delete(MesoscaleDiscussionAlert).where(
            MesoscaleDiscussionAlert.alerted_at < cutoff_dt
        )
        n_outlook = sess.execute(outlook_stmt).rowcount or 0
        n_md = sess.execute(md_stmt).rowcount or 0
        sess.commit()

    return n_outlook, n_md


def run_one_cycle(
    config: LookoutConfig,
    notifier: Notifier,
    engine,
    client: SPCClient,
    now: datetime,
    *,
    dry_run: bool = False,
) -> CycleResult:
    """Run a complete cycle: identify seed → fetch outlooks → process → MDs → track failures."""
    seed = identify_unseeded_locations(engine, config)
    if seed:
        logger.info("seeding locations: %s", sorted(seed))

    fetch = run_fetch_once(config, client, now)
    cycle = process_cycle(
        config, notifier, engine, fetch, now,
        seed_locations=seed,
        dry_run=dry_run,
    )

    if config.products.mesoscale_discussion.enabled:
        mds = fetch_active_mds(client)
        if mds is not None:  # None means RSS unchanged
            mds_processed, md_alerts = process_md_cycle(
                config, notifier, engine, mds, now, dry_run=dry_run,
            )
            cycle.mds_processed = mds_processed
            cycle.md_alerts_sent = md_alerts

    track_fetch_failures(config, notifier, engine, fetch, now, dry_run=dry_run)

    if not dry_run:
        n_outlook, n_md = prune_state(engine, config.state_retention_days, now)
        if n_outlook + n_md > 0:
            logger.info(
                "pruned %d outlook + %d MD rows older than %d days",
                n_outlook, n_md, config.state_retention_days,
            )

    logger.info(
        "cycle: fetched=%s unchanged=%s errors=%s events=%d notifs=%d seeded=%s mds=%d md_alerts=%d",
        [o.outlook_day for o in fetch.fetched],
        fetch.skipped_unchanged,
        list(fetch.errors.keys()),
        cycle.events_generated,
        cycle.notifications_sent,
        cycle.seeded_locations,
        cycle.mds_processed,
        cycle.md_alerts_sent,
    )
    return cycle


def run_polling_loop(
    config: LookoutConfig,
    db_path: Path,
    *,
    dry_run: bool = False,
    shutdown: Optional[threading.Event] = None,
) -> int:
    """Run the polling loop until `shutdown` is set (or SIGINT/SIGTERM received)."""
    if shutdown is None:
        shutdown = threading.Event()
        _install_signal_handlers(shutdown)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(db_path)
    init_db(engine)

    notifier = Notifier(config, dry_run=dry_run)
    interval_seconds = config.polling.interval_minutes * 60

    logger.info(
        "Lookout starting: %d location(s), %d rule(s), %s mode, %d-min interval",
        len(config.locations),
        len(config.notification_rules),
        "dry-run" if dry_run else "live",
        config.polling.interval_minutes,
    )

    with SPCClient(user_agent_contact=config.user_agent_contact) as client:
        while not shutdown.is_set():
            now = datetime.now(tz=timezone.utc)
            try:
                run_one_cycle(config, notifier, engine, client, now, dry_run=dry_run)
            except Exception:
                logger.exception("cycle raised an unhandled exception")

            shutdown.wait(timeout=interval_seconds)

    logger.info("Lookout shutting down cleanly")
    return 0


def _install_signal_handlers(shutdown: threading.Event) -> None:
    def _handler(_signum, _frame):
        shutdown.set()
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
