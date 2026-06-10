"""Tests for additive SQLite schema migration of vacation columns."""

import json as _json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, select, text

from app.db.session import (
    _backfill_mock_flags,
    _ensure_deal_candidate_columns,
    _ensure_price_snapshot_columns,
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


# ---------------------------------------------------------------------------
# Tests for Phase 4D-1 is_mock column migration and backfill
# ---------------------------------------------------------------------------

@pytest.fixture()
def old_schema_deal_candidate_db(tmp_path, monkeypatch):
    """Create a temporary SQLite DB with deal_candidate missing is_mock."""
    db_path = tmp_path / "vacation_deals_old_dc.sqlite3"
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    # Create vacation table (with new columns already present)
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
            preferred_airports_json VARCHAR DEFAULT '[]',
            alternate_airports_json VARCHAR DEFAULT '[]',
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
        """
    )
    # Create deal_candidate WITHOUT is_mock column (old schema)
    conn.execute(
        """
        CREATE TABLE deal_candidate (
            id INTEGER PRIMARY KEY,
            vacation_id INTEGER NOT NULL,
            search_run_id INTEGER NOT NULL,
            candidate_type VARCHAR NOT NULL,
            title VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            total_price REAL,
            currency VARCHAR DEFAULT 'USD',
            score REAL,
            score_breakdown_json VARCHAR DEFAULT '{}',
            component_snapshot_ids_json VARCHAR DEFAULT '[]',
            source_links_json VARCHAR DEFAULT '[]',
            normalized_result_json VARCHAR DEFAULT '{}',
            created_at DATETIME NOT NULL
        )
        """
    )
    # Create price_snapshot WITHOUT is_mock column (old schema)
    conn.execute(
        """
        CREATE TABLE price_snapshot (
            id INTEGER PRIMARY KEY,
            vacation_id INTEGER NOT NULL,
            search_run_id INTEGER NOT NULL,
            source_result_id INTEGER,
            quote_type VARCHAR NOT NULL,
            source_name VARCHAR NOT NULL,
            provider VARCHAR,
            label VARCHAR NOT NULL,
            total_price REAL,
            currency VARCHAR DEFAULT 'USD',
            source_url VARCHAR,
            normalized_json VARCHAR DEFAULT '{}',
            captured_at DATETIME NOT NULL,
            created_at DATETIME NOT NULL
        )
        """
    )

    # Insert test data into deal_candidate (old schema)
    conn.execute(
        "INSERT INTO deal_candidate (vacation_id, search_run_id, candidate_type, title, status, total_price, currency, normalized_result_json, source_links_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, 1, "flight_only", "MOCK Flight Deal", "valid", 250.0, "USD", '{"mock": true}', '[]', NOW_STR),
    )
    conn.execute(
        "INSERT INTO deal_candidate (vacation_id, search_run_id, candidate_type, title, status, total_price, currency, normalized_result_json, source_links_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, 1, "flight_only", "Real Flight Deal", "valid", 320.0, "USD", '{"mock": false}', '[]', NOW_STR),
    )
    conn.execute(
        "INSERT INTO deal_candidate (vacation_id, search_run_id, candidate_type, title, status, total_price, currency, normalized_result_json, source_links_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, 1, "flight_only", "Normal Flight Title", "valid", 280.0, "USD", '{"mock": true}', '[]', NOW_STR),
    )

    # Insert test data into price_snapshot (old schema)
    conn.execute(
        "INSERT INTO price_snapshot (vacation_id, search_run_id, quote_type, source_name, label, total_price, currency, normalized_json, captured_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, 1, "flight", "mock_travel", "MOCK Flight Label", 200.0, "USD", '{"mock": true}', NOW_STR, NOW_STR),
    )
    conn.execute(
        "INSERT INTO price_snapshot (vacation_id, search_run_id, quote_type, source_name, label, total_price, currency, normalized_json, captured_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, 1, "flight", "trvl", "Real Flight Label", 350.0, "USD", '{"mock": false}', NOW_STR, NOW_STR),
    )
    conn.execute(
        "INSERT INTO price_snapshot (vacation_id, search_run_id, quote_type, source_name, label, total_price, currency, normalized_json, captured_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, 1, "flight", "google_flights", "MOCK Google Label", 275.0, "USD", '{"mock": false}', NOW_STR, NOW_STR),
    )

    conn.commit()
    conn.close()

    monkeypatch.setenv("VACATION_DEAL_DB_URL", f"sqlite:///{db_path}")
    _reset_engine_cache()

    yield db_path


def test_ensure_deal_candidate_columns_adds_is_mock(old_schema_deal_candidate_db):
    """Migration adds is_mock column to deal_candidate when absent."""
    _reset_engine_cache()
    engine = get_engine()
    with engine.connect() as conn:
        columns_before = _get_table_columns("deal_candidate")

    assert "is_mock" not in columns_before

    init_db()  # triggers migration

    with engine.connect() as conn:
        columns_after = _get_table_columns("deal_candidate")

    assert "is_mock" in columns_after


def test_ensure_price_snapshot_columns_adds_is_mock(old_schema_deal_candidate_db):
    """Migration adds is_mock column to price_snapshot when absent."""
    _reset_engine_cache()
    engine = get_engine()
    with engine.connect() as conn:
        columns_before = _get_table_columns("price_snapshot")

    assert "is_mock" not in columns_before

    init_db()  # triggers migration

    with engine.connect() as conn:
        columns_after = _get_table_columns("price_snapshot")

    assert "is_mock" in columns_after


def test_existing_rows_preserved_after_is_mock_migration(old_schema_deal_candidate_db):
    """Existing deal_candidate and price_snapshot rows survive is_mock migration."""
    _reset_engine_cache()
    init_db()

    engine = get_engine()
    with engine.connect() as conn:
        dc_count = conn.execute(text("SELECT COUNT(*) FROM deal_candidate")).fetchone()[0]
        ps_count = conn.execute(text("SELECT COUNT(*) FROM price_snapshot")).fetchone()[0]

    assert dc_count == 3
    assert ps_count == 3


def test_mock_backfill_deal_candidate_title_contains_mock(old_schema_deal_candidate_db):
    """Rows with MOCK in title get is_mock=1."""
    _reset_engine_cache()
    init_db()

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, title, is_mock FROM deal_candidate WHERE title = 'MOCK Flight Deal'")
        ).fetchone()

    assert row[0] == 1
    assert row[2] == 1


def test_mock_backfill_deal_candidate_normalized_json_has_mock_true(old_schema_deal_candidate_db):
    """Rows with mock:true in normalized_result_json get is_mock=1."""
    _reset_engine_cache()
    init_db()

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, title, is_mock FROM deal_candidate WHERE title = 'Normal Flight Title'")
        ).fetchone()

    assert row[0] == 3
    assert row[2] == 1


def test_mock_backfill_deal_candidate_real_rows_remain_false(old_schema_deal_candidate_db):
    """Real rows without mock markers remain is_mock=0."""
    _reset_engine_cache()
    init_db()

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, title, is_mock FROM deal_candidate WHERE title = 'Real Flight Deal'")
        ).fetchone()

    assert row[0] == 2
    assert row[2] == 0


def test_mock_backfill_price_snapshot_label_contains_mock(old_schema_deal_candidate_db):
    """Rows with MOCK in label get is_mock=1."""
    _reset_engine_cache()
    init_db()

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, label, is_mock FROM price_snapshot WHERE label = 'MOCK Google Label'")
        ).fetchone()

    assert row[0] == 3
    assert row[2] == 1


def test_mock_backfill_price_snapshot_source_name_contains_mock(old_schema_deal_candidate_db):
    """Rows with mock in source_name get is_mock=1."""
    _reset_engine_cache()
    init_db()

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, source_name, label, is_mock FROM price_snapshot WHERE source_name = 'mock_travel'")
        ).fetchone()

    assert row[0] == 1
    assert row[3] == 1


def test_mock_backfill_price_snapshot_real_rows_remain_false(old_schema_deal_candidate_db):
    """Real rows without mock markers remain is_mock=0."""
    _reset_engine_cache()
    init_db()

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, source_name, label, is_mock FROM price_snapshot WHERE source_name = 'trvl'")
        ).fetchone()

    assert row[0] == 2
    assert row[3] == 0


def test_is_mock_migration_is_idempotent(old_schema_deal_candidate_db):
    """Running init_db twice does not error and values remain correct."""
    _reset_engine_cache()
    init_db()

    engine = get_engine()
    with engine.connect() as conn:
        rows1_dc = conn.execute(text("SELECT id, is_mock FROM deal_candidate ORDER BY id")).fetchall()
        rows1_ps = conn.execute(text("SELECT id, is_mock FROM price_snapshot ORDER BY id")).fetchall()

    # Run again
    init_db()

    with engine.connect() as conn:
        rows2_dc = conn.execute(text("SELECT id, is_mock FROM deal_candidate ORDER BY id")).fetchall()
        rows2_ps = conn.execute(text("SELECT id, is_mock FROM price_snapshot ORDER BY id")).fetchall()

    assert rows1_dc == rows2_dc
    assert rows1_ps == rows2_ps


def test_is_mock_queryable_via_sqlmodel(old_schema_deal_candidate_db):
    """After migration, is_mock can be queried through SQLModel models."""
    _reset_engine_cache()
    from app.db.models import DealCandidate, PriceSnapshot

    init_db()

    with Session(get_engine()) as session:
        dc = session.exec(select(DealCandidate).where(DealCandidate.id == 1)).first()
        ps = session.exec(select(PriceSnapshot).where(PriceSnapshot.id == 2)).first()

    assert dc is not None
    assert ps is not None
    assert dc.is_mock == True  # MOCK in title
    assert ps.is_mock == False  # real trvl row


def test_backfill_safe_to_run_repeatedly(old_schema_deal_candidate_db):
    """Running _backfill_mock_flags multiple times does not change results."""
    _reset_engine_cache()
    init_db()

    engine = get_engine()
    with engine.connect() as conn:
        rows_before = conn.execute(
            text("SELECT id, is_mock FROM deal_candidate ORDER BY id")
        ).fetchall()

    # Run backfill again directly
    _backfill_mock_flags()

    with engine.connect() as conn:
        rows_after = conn.execute(
            text("SELECT id, is_mock FROM deal_candidate ORDER BY id")
        ).fetchall()

    assert rows_before == rows_after


def test_init_db_on_fresh_db_with_old_schema_tables_no_error(monkeypatch):
    """init_db works when tables already have is_mock (fresh DB)."""
    tmp_path = Path("/tmp/vacation_is_mock_fresh_test.sqlite3")
    _reset_engine_cache()
    monkeypatch.setenv("VACATION_DEAL_DB_URL", f"sqlite:///{tmp_path}")

    try:
        # Create a fresh DB with all tables including is_mock already present
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
                preferred_airports_json VARCHAR DEFAULT '[]',
                alternate_airports_json VARCHAR DEFAULT '[]',
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE deal_candidate (
                id INTEGER PRIMARY KEY,
                vacation_id INTEGER NOT NULL,
                search_run_id INTEGER NOT NULL,
                candidate_type VARCHAR NOT NULL,
                title VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                total_price REAL,
                currency VARCHAR DEFAULT 'USD',
                score REAL,
                score_breakdown_json VARCHAR DEFAULT '{}',
                component_snapshot_ids_json VARCHAR DEFAULT '[]',
                source_links_json VARCHAR DEFAULT '[]',
                normalized_result_json VARCHAR DEFAULT '{}',
                created_at DATETIME NOT NULL,
                is_mock INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE price_snapshot (
                id INTEGER PRIMARY KEY,
                vacation_id INTEGER NOT NULL,
                search_run_id INTEGER NOT NULL,
                source_result_id INTEGER,
                quote_type VARCHAR NOT NULL,
                source_name VARCHAR NOT NULL,
                provider VARCHAR,
                label VARCHAR NOT NULL,
                total_price REAL,
                currency VARCHAR DEFAULT 'USD',
                source_url VARCHAR,
                normalized_json VARCHAR DEFAULT '{}',
                captured_at DATETIME NOT NULL,
                created_at DATETIME NOT NULL,
                is_mock INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()
        conn.close()

        # init_db should not error even though tables already exist with is_mock
        init_db()

        with Session(get_engine()) as session:
            count = len(session.execute(text("SELECT * FROM deal_candidate")).fetchall())
        assert count == 0
    finally:
        _reset_engine_cache()
        tmp_path.unlink(missing_ok=True)
