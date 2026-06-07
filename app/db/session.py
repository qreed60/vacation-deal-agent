from __future__ import annotations

import os
from pathlib import Path
from typing import Generator

from sqlmodel import Session, SQLModel, create_engine


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


def get_session() -> Generator[Session, None, None]:
    with Session(get_engine()) as session:
        yield session
