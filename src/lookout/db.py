"""SQLite engine and session management for Lookout."""

from pathlib import Path

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

# Importing models registers their tables on the SQLModel metadata used by create_all.
from lookout import models  # noqa: F401


def make_engine(db_path: Path | str):
    """Create a SQLite engine with WAL mode and foreign keys enabled."""
    url = f"sqlite:///{db_path}"
    engine = create_engine(url, echo=False)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def init_db(engine) -> None:
    SQLModel.metadata.create_all(engine)


def session(engine) -> Session:
    return Session(engine)
