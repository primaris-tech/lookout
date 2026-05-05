"""Entry point for the `lookout` command."""

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from lookout.config import load_config
from lookout.db import init_db, make_engine
from lookout.loop import run_one_cycle, run_polling_loop
from lookout.notifier import Notifier
from lookout.spc import SPCClient


def main() -> int:
    parser = argparse.ArgumentParser(prog="lookout")
    parser.add_argument(
        "--config", "-c",
        type=Path,
        default=Path("config.yml"),
        help="Path to config file (default: ./config.yml)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data/lookout.db"),
        help="Path to SQLite state database (default: ./data/lookout.db)",
    )
    parser.add_argument("--check", action="store_true", help="Validate config and exit")
    parser.add_argument(
        "--fetch-once",
        action="store_true",
        help="Run exactly one cycle and exit (vs. the polling loop)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print notifications instead of sending them, and skip DB commits",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    config = load_config(args.config)

    if args.check:
        print(
            f"Config OK: {len(config.locations)} location(s), "
            f"{len(config.notification_channels)} channel(s), "
            f"{len(config.notification_rules)} rule(s)"
        )
        return 0

    if args.fetch_once:
        return _one_cycle(config, args.db, dry_run=args.dry_run)

    return run_polling_loop(config, args.db, dry_run=args.dry_run)


def _one_cycle(config, db_path: Path, *, dry_run: bool) -> int:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(db_path)
    init_db(engine)
    notifier = Notifier(config, dry_run=dry_run)
    now = datetime.now(tz=timezone.utc)
    with SPCClient(user_agent_contact=config.user_agent_contact) as client:
        run_one_cycle(config, notifier, engine, client, now, dry_run=dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
