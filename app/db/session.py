from __future__ import annotations

import os
from pathlib import Path
from typing import Generator, List, Tuple

from sqlmodel import Session, SQLModel, create_engine, text


DEFAULT_DB_PATH = Path("data/vacation_deals.sqlite3")
_engine = None
_engine_url = None


def database_url() -> str:
    return os.environ.get("VACATION_DEAL_DB_URL", f"sqlite:///{DEFAULT_DB_PATH}")


def get_engine():
    global _engine, _engine_url
    url = database_url()
    if _engine is None or _engine_url != url:
        if url.startswith("sqlite:///"):
            db_path = Path(url.replace("sqlite:///", "", 1))
            if str(db_path) != ":memory:":
                db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            url,
            connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
        )
        _engine_url = url
    return _engine


def init_db() -> None:
    SQLModel.metadata.create_all(get_engine())
    _ensure_vacation_columns()


def _get_table_columns(table_name: str) -> List[str]:
    """Return a list of column names for the given SQLite table."""
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text(f"PRAGMA table_info({table_name})"))
        rows: List[Tuple] = result.fetchall()
        return [row[1] for row in rows]


def _ensure_vacation_columns() -> None:
    """Add missing vacation columns to existing SQLite databases.

    This is an additive, idempotent migration that only applies to SQLite.
    It does not drop tables, delete data, or rewrite existing rows.
    Safe to call when the vacation table does not exist yet.
    """
    engine = get_engine()
    url = database_url()
    if not url.startswith("sqlite"):
        return

    with engine.connect() as conn:
        try:
            columns = _get_table_columns("vacation")
        except Exception:
            # Table doesn't exist yet; SQLModel.create_all will create it.
            return

        if "preferred_airports_json" not in columns:
            conn.execute(
                text("ALTER TABLE vacation ADD COLUMN preferred_airports_json VARCHAR NOT NULL DEFAULT '[]'")
            )
            conn.commit()
        if "alternate_airports_json" not in columns:
            conn.execute(
                text("ALTER TABLE vacation ADD COLUMN alternate_airports_json VARCHAR NOT NULL DEFAULT '[]'")
            )
            conn.commit()


def get_session() -> Generator[Session, None, None]:
    with Session(get_engine()) as session:
        yield session
