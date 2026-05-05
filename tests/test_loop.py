"""Tests for the polling-loop building blocks: seed detection, cycle processing,
and fetch-failure tracking. Uses an in-memory SQLite engine so each test gets a
fresh DB without touching disk.
"""

from datetime import date, datetime, timedelta
from typing import Optional

import pytest
from sqlmodel import SQLModel, create_engine, select

from lookout.config import (
    Location,
    LookoutConfig,
    NotificationRule,
    PollingConfig,
    ProductsConfig,
    RiskLevel,
)
from lookout.db import session as db_session
from lookout.events import ObservedRisk
from lookout.fetcher import FetchResult
from lookout.loop import (
    identify_unseeded_locations,
    process_cycle,
    prune_state,
    track_fetch_failures,
)
from lookout.models import (
    FetchFailureState,
    LocationSeed,
    MesoscaleDiscussionAlert,
    OutlookState,
)
from lookout.notifier import Notifier


# --- helpers ---

def _engine():
    """In-memory SQLite engine with all tables created."""
    eng = create_engine("sqlite://", echo=False)
    SQLModel.metadata.create_all(eng)
    return eng


def _config(*, locations=None, rules=None, meta_alert_minutes=60) -> LookoutConfig:
    return LookoutConfig(
        user_agent_contact="t@example.com",
        alert_threshold=RiskLevel.MRGL,
        locations=locations or {"home": Location(lat=35.0, lon=-97.0)},
        notification_channels={"phone": "ntfys://test"},
        notification_rules=rules or [
            NotificationRule(name="all", locations=["home"], channels=["phone"])
        ],
        products=ProductsConfig(),
        polling=PollingConfig(interval_minutes=10, meta_alert_after_minutes=meta_alert_minutes),
    )


def _obs(
    location: str = "home",
    *,
    risk: Optional[RiskLevel] = RiskLevel.SLGT,
    target: date = date(2026, 5, 7),
    day: int = 3,
    at: datetime = datetime(2026, 5, 5, 12, 0),
) -> ObservedRisk:
    return ObservedRisk(
        location_name=location,
        target_date=target,
        risk=risk,
        outlook_day=day,
        observed_at=at,
    )


def _fetch(observations=None, *, attempted: int = 1, errors: Optional[dict] = None) -> FetchResult:
    return FetchResult(
        observations=observations or [],
        fetched=[],
        skipped_unchanged=[],
        errors=errors or {},
        attempted=attempted,
    )


NOW = datetime(2026, 5, 5, 12, 0)  # naive UTC; matches the DB round-trip form


# --- identify_unseeded_locations ---

def test_unseeded_locations_includes_all_when_db_is_empty():
    eng = _engine()
    cfg = _config(locations={"home": Location(lat=0, lon=0), "parents": Location(lat=1, lon=1)},
                  rules=[NotificationRule(name="all", locations=["home", "parents"], channels=["phone"])])
    assert identify_unseeded_locations(eng, cfg) == {"home", "parents"}


def test_unseeded_excludes_locations_with_seed_marker():
    eng = _engine()
    cfg = _config(locations={"home": Location(lat=0, lon=0), "parents": Location(lat=1, lon=1)},
                  rules=[NotificationRule(name="all", locations=["home", "parents"], channels=["phone"])])
    with db_session(eng) as sess:
        sess.add(LocationSeed(location_name="home", seeded_at=NOW))
        sess.commit()
    assert identify_unseeded_locations(eng, cfg) == {"parents"}


# --- process_cycle: non-seed (events dispatch normally) ---

def test_process_cycle_non_seed_dispatches_events(capsys):
    eng = _engine()
    cfg = _config()
    notifier = Notifier(cfg, dry_run=True)
    fetch = _fetch([_obs(risk=RiskLevel.SLGT)])

    cycle = process_cycle(cfg, notifier, eng, fetch, NOW, seed_locations=set())

    assert cycle.events_generated == 1
    assert cycle.notifications_sent == 1
    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out
    assert "SLGT" in out


# --- process_cycle: seed mode suppresses events, emits summary ---

def test_process_cycle_seed_mode_suppresses_individual_events_and_emits_summary(capsys):
    eng = _engine()
    cfg = _config()
    notifier = Notifier(cfg, dry_run=True)
    fetch = _fetch([
        _obs(risk=RiskLevel.SLGT, target=date(2026, 5, 7), day=3),
        _obs(risk=RiskLevel.MRGL, target=date(2026, 5, 8), day=4),
    ])

    cycle = process_cycle(cfg, notifier, eng, fetch, NOW, seed_locations={"home"})

    out = capsys.readouterr().out
    # No per-event dry-run lines (those start with "[DRY-RUN]" not "[DRY-RUN SUMMARY]")
    assert "[DRY-RUN] Lookout — SLGT risk" not in out
    # One summary, with both events in body.
    assert "[DRY-RUN SUMMARY]" in out
    assert "SLGT" in out and "MRGL" in out
    assert cycle.notifications_sent == 1


def test_process_cycle_seed_mode_marks_location_seeded(capsys):
    eng = _engine()
    cfg = _config()
    notifier = Notifier(cfg, dry_run=False)  # NOT dry-run, so commit happens
    fetch = _fetch([_obs(risk=RiskLevel.SLGT)])

    process_cycle(cfg, notifier, eng, fetch, NOW, seed_locations={"home"})

    with db_session(eng) as sess:
        rows = sess.exec(select(LocationSeed)).all()
    assert {r.location_name for r in rows} == {"home"}


# --- process_cycle: dry_run skips DB commits ---

def test_process_cycle_dry_run_does_not_commit_state():
    eng = _engine()
    cfg = _config()
    notifier = Notifier(cfg, dry_run=True)
    fetch = _fetch([_obs(risk=RiskLevel.SLGT)])

    process_cycle(cfg, notifier, eng, fetch, NOW, seed_locations=set(), dry_run=True)

    with db_session(eng) as sess:
        states = sess.exec(select(OutlookState)).all()
        seeds = sess.exec(select(LocationSeed)).all()
    assert states == []
    assert seeds == []


# --- track_fetch_failures ---

def test_partial_success_does_not_open_failure_window():
    eng = _engine()
    cfg = _config(meta_alert_minutes=60)
    notifier = Notifier(cfg, dry_run=True)
    fetch = _fetch(attempted=3, errors={1: "boom"})  # 1 of 3 errored — partial success

    track_fetch_failures(cfg, notifier, eng, fetch, NOW)

    with db_session(eng) as sess:
        state = sess.get(FetchFailureState, 1)
    assert state is None or state.first_failure_at is None


def test_total_failure_under_threshold_tracks_but_does_not_alert(capsys):
    eng = _engine()
    cfg = _config(meta_alert_minutes=60)
    notifier = Notifier(cfg, dry_run=True)
    fetch = _fetch(attempted=2, errors={1: "boom", 2: "boom"})

    track_fetch_failures(cfg, notifier, eng, fetch, NOW)

    out = capsys.readouterr().out
    assert "[DRY-RUN BROADCAST]" not in out
    with db_session(eng) as sess:
        state = sess.get(FetchFailureState, 1)
    assert state.first_failure_at == NOW
    assert state.last_meta_alert_at is None


def test_total_failure_past_threshold_fires_meta_alert_once(capsys):
    eng = _engine()
    cfg = _config(meta_alert_minutes=60)
    notifier = Notifier(cfg, dry_run=True)

    fetch = _fetch(attempted=2, errors={1: "boom", 2: "boom"})
    # First call: opens window
    track_fetch_failures(cfg, notifier, eng, fetch, NOW)
    capsys.readouterr()  # discard
    # Second call: 65 min later, should fire
    later = NOW + timedelta(minutes=65)
    track_fetch_failures(cfg, notifier, eng, fetch, later)
    out1 = capsys.readouterr().out
    assert "[DRY-RUN BROADCAST]" in out1
    assert "SPC unreachable" in out1

    # Third call: 5 minutes later (still within same window) — must NOT re-fire
    track_fetch_failures(cfg, notifier, eng, fetch, later + timedelta(minutes=5))
    out2 = capsys.readouterr().out
    assert "[DRY-RUN BROADCAST]" not in out2


def test_recovery_after_meta_alert_fires_recovery_alert(capsys):
    eng = _engine()
    cfg = _config(meta_alert_minutes=60)
    notifier = Notifier(cfg, dry_run=True)
    fetch_fail = _fetch(attempted=2, errors={1: "boom", 2: "boom"})
    fetch_ok = _fetch(attempted=2)

    track_fetch_failures(cfg, notifier, eng, fetch_fail, NOW)
    track_fetch_failures(cfg, notifier, eng, fetch_fail, NOW + timedelta(minutes=65))
    capsys.readouterr()  # discard meta-alert output

    track_fetch_failures(cfg, notifier, eng, fetch_ok, NOW + timedelta(minutes=70))

    out = capsys.readouterr().out
    assert "[DRY-RUN BROADCAST]" in out
    assert "reachable again" in out


# --- prune_state ---

def _seed_outlook(sess, *, target_date: date, location: str = "home"):
    sess.add(OutlookState(
        location_name=location,
        target_date=target_date,
        current_risk="MRGL",
        current_day=3,
        peak_risk="MRGL",
        first_seen_at=NOW,
        first_seen_day=3,
        first_seen_risk="MRGL",
        last_changed_at=NOW,
        last_observed_at=NOW,
    ))


def _seed_md_alert(sess, *, alerted_at: datetime, mcd_id: str = "0001"):
    sess.add(MesoscaleDiscussionAlert(
        mcd_id=mcd_id, location_name="home", alerted_at=alerted_at,
    ))


def test_prune_state_drops_old_outlook_rows():
    eng = _engine()
    with db_session(eng) as sess:
        _seed_outlook(sess, target_date=NOW.date() - timedelta(days=45))
        _seed_outlook(sess, target_date=NOW.date() - timedelta(days=10), location="parents")
        sess.commit()

    n_outlook, _ = prune_state(eng, retention_days=30, now=NOW)
    assert n_outlook == 1

    with db_session(eng) as sess:
        remaining = sess.exec(select(OutlookState)).all()
    assert {r.location_name for r in remaining} == {"parents"}


def test_prune_state_drops_old_md_alerts_by_alerted_at():
    eng = _engine()
    with db_session(eng) as sess:
        _seed_md_alert(sess, alerted_at=NOW - timedelta(days=45), mcd_id="0001")
        _seed_md_alert(sess, alerted_at=NOW - timedelta(days=10), mcd_id="0002")
        sess.commit()

    _, n_md = prune_state(eng, retention_days=30, now=NOW)
    assert n_md == 1

    with db_session(eng) as sess:
        remaining = sess.exec(select(MesoscaleDiscussionAlert)).all()
    assert {r.mcd_id for r in remaining} == {"0002"}


def test_prune_state_keeps_rows_at_exact_cutoff():
    eng = _engine()
    cutoff_date = (NOW - timedelta(days=30)).date()
    with db_session(eng) as sess:
        # Exact cutoff date — kept (boundary is strict <)
        _seed_outlook(sess, target_date=cutoff_date)
        sess.commit()

    n_outlook, _ = prune_state(eng, retention_days=30, now=NOW)
    assert n_outlook == 0


def test_prune_state_empty_db_no_op():
    eng = _engine()
    n_outlook, n_md = prune_state(eng, retention_days=30, now=NOW)
    assert (n_outlook, n_md) == (0, 0)


def test_recovery_without_prior_meta_alert_is_silent(capsys):
    eng = _engine()
    cfg = _config(meta_alert_minutes=60)
    notifier = Notifier(cfg, dry_run=True)
    fetch_fail = _fetch(attempted=2, errors={1: "boom", 2: "boom"})
    fetch_ok = _fetch(attempted=2)

    # Failure window opens but never crosses threshold
    track_fetch_failures(cfg, notifier, eng, fetch_fail, NOW)
    capsys.readouterr()
    track_fetch_failures(cfg, notifier, eng, fetch_ok, NOW + timedelta(minutes=10))

    out = capsys.readouterr().out
    assert "[DRY-RUN BROADCAST]" not in out
    with db_session(eng) as sess:
        state = sess.get(FetchFailureState, 1)
    assert state.first_failure_at is None
