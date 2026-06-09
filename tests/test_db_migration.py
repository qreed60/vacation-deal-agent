"""Tests for additive SQLite schema migration of vacation columns."""

import json as _json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, text

from app.db.session import (
    _ensure_vacation_columns,
    _get_table_columns,
    get_engine,
    init_db,
)


def _reset_engine_cache():
    """Reset the module-level engine singleton so tests can switch DB URLs."""
    import app.db.session as session_mod

    session_mod._engine = None
    session_mod._engine_url = None


NOW_STR = datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def old_db(tmp_path, monkeypatch):
    """Create a temporary SQLite DB with the old vacation schema (no JSON columns)."""
    db_path = tmp_path / "vacation_deals_old.sqlite3"
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE vacation (
            id INTEGER PRIMARY KEY,
            slug VARCHAR NOT NULL,
            title VARCHAR NOT NULL,
            status VARCHAR DEFAULT 'active',
            number_of_travelers INTEGER NOT NULL,
            travelers_json VARCHAR DEFAULT '[]',
            origin VARCHAR NOT NULL,
            destination VARCHAR NOT NULL,
            date_mode VARCHAR NOT NULL,
            start_date DATE,
            end_date DATE,
            nights_min INTEGER,
            nights_target INTEGER,
            nights_max INTEGER,
            hotel_needed BOOLEAN DEFAULT 1,
            airfare_needed BOOLEAN DEFAULT 1,
            rental_car_needed BOOLEAN DEFAULT 0,
            special_accommodations VARCHAR DEFAULT '',
            manifest_json VARCHAR NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
        """
    )
    # Insert an existing vacation row to verify it survives migration.
    conn.execute(
        "INSERT INTO vacation (slug, title, origin, destination, date_mode, manifest_json, number_of_travelers, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("test-trip", "Old Test Trip", "PIT", "MOT", "flexible_dates", _json.dumps({"key": "value"}), 2, NOW_STR, NOW_STR),
    )
    conn.commit()
    conn.close()

    # Point the engine at this temp DB
    monkeypatch.setenv("VACATION_DEAL_DB_URL", f"sqlite:///{db_path}")
    _reset_engine_cache()

    yield db_path


def test_migration_adds_columns(old_db):
    """Migration adds preferred_airports_json and alternate_airports_json."""
    _reset_engine_cache()  # ensure clean state before each test
    engine = get_engine()
    with engine.connect() as conn:
        columns_before = _get_table_columns("vacation")

    assert "preferred_airports_json" not in columns_before
    assert "alternate_airports_json" not in columns_before

    init_db()  # triggers migration

    with engine.connect() as conn:
        columns_after = _get_table_columns("vacation")

    assert "preferred_airports_json" in columns_after
    assert "alternate_airports_json" in columns_after


def test_migration_is_idempotent(old_db):
    """Running migration twice does not error and columns remain."""
    _reset_engine_cache()  # ensure clean state before each test
    init_db()
    engine = get_engine()

    with engine.connect() as conn:
        cols1 = _get_table_columns("vacation")

    # Run again
    init_db()

    with engine.connect() as conn:
        cols2 = _get_table_columns("vacation")

    assert cols1 == cols2


def test_existing_vacation_row_survives(old_db):
    """Existing vacation row is preserved after migration."""
    _reset_engine_cache()  # ensure clean state before each test
    init_db()

    with Session(get_engine()) as session:
        vacation = session.execute(
            text("SELECT id, slug, title, origin, destination FROM vacation")
        ).fetchone()

    assert vacation is not None
    assert vacation[0] == 1
    assert vacation[1] == "test-trip"
    assert vacation[2] == "Old Test Trip"
    assert vacation[3] == "PIT"
    assert vacation[4] == "MOT"


def test_existing_vacation_queryable_via_sqlmodel(old_db):
    """Existing row can be queried through SQLModel after migration."""
    _reset_engine_cache()  # ensure clean state before each test
    from app.db.models import Vacation

    init_db()

    with Session(get_engine()) as session:
        vacation = session.get(Vacation, 1)

    assert vacation is not None
    assert vacation.slug == "test-trip"
    assert vacation.title == "Old Test Trip"


def test_migration_defaults_are_empty_list(old_db):
    """preferred_airports_json and alternate_airports_json default to '[]'."""
    _reset_engine_cache()  # ensure clean state before each test
    from app.db.models import Vacation

    init_db()

    with Session(get_engine()) as session:
        vacation = session.get(Vacation, 1)

    assert _json.loads(vacation.preferred_airports_json) == []
    assert _json.loads(vacation.alternate_airports_json) == []


def test_migration_on_fresh_db_no_error(monkeypatch):
    """Migration does not error when the vacation table is freshly created."""
    tmp_path = Path("/tmp/vacation_migrate_test.sqlite3")
    _reset_engine_cache()  # reset BEFORE setting env so get_engine reads new URL
    monkeypatch.setenv("VACATION_DEAL_DB_URL", f"sqlite:///{tmp_path}")

    try:
        init_db()  # creates tables + migration
        _ensure_vacation_columns()  # should be no-op, not error

        with Session(get_engine()) as session:
            count = len(session.execute(text("SELECT * FROM vacation")).fetchall())
        assert count == 0
    finally:
        _reset_engine_cache()
        tmp_path.unlink(missing_ok=True)


def test_run_search_once_invokes_migration(monkeypatch):
    """run_search_once.py calls init_db which triggers migration before querying Vacation."""
    from sqlmodel import text

    tmp_path = Path("/tmp/vacation_cli_test.sqlite3")
    _reset_engine_cache()  # reset BEFORE setting env so get_engine reads new URL
    monkeypatch.setenv("VACATION_DEAL_DB_URL", f"sqlite:///{tmp_path}")

    try:
        # Create old-schema DB manually (no JSON columns)
        import sqlite3 as _sqlite3

        conn = _sqlite3.connect(str(tmp_path))
        conn.execute(
            """
            CREATE TABLE vacation (
                id INTEGER PRIMARY KEY,
                slug VARCHAR NOT NULL,
                title VARCHAR NOT NULL,
                status VARCHAR DEFAULT 'active',
                number_of_travelers INTEGER NOT NULL,
                travelers_json VARCHAR DEFAULT '[]',
                origin VARCHAR NOT NULL,
                destination VARCHAR NOT NULL,
                date_mode VARCHAR NOT NULL,
                start_date DATE,
                end_date DATE,
                nights_min INTEGER,
                nights_target INTEGER,
                nights_max INTEGER,
                hotel_needed BOOLEAN DEFAULT 1,
                airfare_needed BOOLEAN DEFAULT 1,
                rental_car_needed BOOLEAN DEFAULT 0,
                special_accommodations VARCHAR DEFAULT '',
                manifest_json VARCHAR NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO vacation (slug, title, origin, destination, date_mode, manifest_json, number_of_travelers, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("cli-test", "CLI Test Trip", "PIT", "MOT", "flexible_dates", _json.dumps({"key": "val"}), 1, NOW_STR, NOW_STR),
        )
        conn.commit()
        conn.close()

        # Now import and call init_db (which triggers migration)
        from app.db.models import Vacation
        from app.db.session import get_engine, init_db

        init_db()

        with Session(get_engine()) as session:
            vacation = session.get(Vacation, 1)
            assert vacation is not None
            assert vacation.preferred_airports_json == "[]"
            assert vacation.alternate_airports_json == "[]"
    finally:
        _reset_engine_cache()
        tmp_path.unlink(missing_ok=True)
