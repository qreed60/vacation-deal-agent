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
    _ensure_deal_candidate_columns()
    _ensure_price_snapshot_columns()
    _backfill_mock_flags()


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

        for col_name, col_type, default in [
            ("preferred_airports_json", "VARCHAR", "'[]'"),
            ("alternate_airports_json", "VARCHAR", "'[]'"),
        ]:
            if col_name not in columns:
                try:
                    conn.execute(
                        text(f"ALTER TABLE vacation ADD COLUMN {col_name} {col_type} NOT NULL DEFAULT {default}")
                    )
                    conn.commit()
                except Exception:
                    # Table may have been dropped or renamed between checks; safe to ignore.
                    pass


def _ensure_deal_candidate_columns() -> None:
    """Add missing deal_candidate columns from Phase 4D-1.

    Idempotent: only adds columns that are absent. Safe to call repeatedly.
    """
    engine = get_engine()
    url = database_url()
    if not url.startswith("sqlite"):
        return

    with engine.connect() as conn:
        try:
            columns = _get_table_columns("deal_candidate")
        except Exception:
            # Table doesn't exist yet; SQLModel.create_all will create it.
            return

        if "is_mock" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE deal_candidate ADD COLUMN is_mock INTEGER NOT NULL DEFAULT 0"
                )
            )
            conn.commit()


def _ensure_price_snapshot_columns() -> None:
    """Add missing price_snapshot columns from Phase 4D-1.

    Idempotent: only adds columns that are absent. Safe to call repeatedly.
    """
    engine = get_engine()
    url = database_url()
    if not url.startswith("sqlite"):
        return

    with engine.connect() as conn:
        try:
            columns = _get_table_columns("price_snapshot")
        except Exception:
            # Table doesn't exist yet; SQLModel.create_all will create it.
            return

        if "is_mock" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE price_snapshot ADD COLUMN is_mock INTEGER NOT NULL DEFAULT 0"
                )
            )
            conn.commit()


def _backfill_mock_flags() -> None:
    """Backfill is_mock=1 for historical rows that are clearly mock data.

    Conservative backfill rules (all checked via LIKE to avoid partial matches):
      - deal_candidate.title contains 'MOCK'
      - price_snapshot.label contains 'MOCK'
      - normalized_result_json / normalized_json contains '"mock":true' or '"mock": true'
      - source_name equals or contains 'mock'

    Safe to run repeatedly — only updates rows where is_mock=0.
    """
    engine = get_engine()
    url = database_url()
    if not url.startswith("sqlite"):
        return

    # Use raw SQLite connection to bypass SQLAlchemy text() bind parameter parsing.
    # The SQL contains JSON patterns like {"mock":true} that would trigger
    # SQLAlchemy's bind parameter detection (both {name} and :name patterns).
    raw_conn = engine.raw_connection()
    try:
        cur = raw_conn.cursor()

        # Backfill deal_candidate.is_mock from title, normalized_result_json, source_links_json, component_snapshot_ids_json
        # Match both "mock":true and "mock": true (with optional space after colon)
        cur.execute(
            """UPDATE deal_candidate SET is_mock = 1 WHERE is_mock = 0 AND (
                title LIKE '%MOCK%' OR
                normalized_result_json LIKE '%"mock":true%' OR
                normalized_result_json LIKE '%"mock": true%' OR
                source_links_json LIKE '%"source":"mock"%\' OR
                component_snapshot_ids_json LIKE '%"source":"mock"%'
            )"""
        )

        # Backfill price_snapshot.is_mock from label, normalized_json, source_name
        cur.execute(
            """UPDATE price_snapshot SET is_mock = 1 WHERE is_mock = 0 AND (
                label LIKE '%MOCK%' OR
                normalized_json LIKE '%"mock":true%' OR
                normalized_json LIKE '%"mock": true%' OR
                source_name LIKE '%mock%'
            )"""
        )

        raw_conn.commit()
    finally:
        raw_conn.close()


def get_session() -> Generator[Session, None, None]:
    with Session(get_engine()) as session:
        yield session
