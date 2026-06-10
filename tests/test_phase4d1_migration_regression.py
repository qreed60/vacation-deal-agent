"""Regression tests for Phase 4D-1 is_mock migration on routes and services."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, select, text

from app.db.models import DealCandidate, PriceSnapshot, Vacation
from app.db.session import get_engine, init_db
from app.services.price_history import vacation_price_history


def _reset_engine_cache():
    """Reset the module-level engine singleton."""
    import app.db.session as session_mod

    session_mod._engine = None
    session_mod._engine_url = None


NOW_STR = datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def old_schema_db(tmp_path, monkeypatch):
    """Create a temporary SQLite DB with old deal_candidate/price_snapshot schema (no is_mock)."""
    db_path = tmp_path / "vacation_deals_old_route.sqlite3"
    import sqlite3

    conn = sqlite3.connect(str(db_path))

    # vacation table with all columns including the ones added by prior migrations
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

    # deal_candidate WITHOUT is_mock (old schema)
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

    # price_snapshot WITHOUT is_mock (old schema)
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

    # search_run table (needed for FK references)
    conn.execute(
        """
        CREATE TABLE search_run (
            id INTEGER PRIMARY KEY,
            vacation_id INTEGER NOT NULL,
            status VARCHAR DEFAULT 'queued',
            trigger_type VARCHAR NOT NULL,
            started_at DATETIME,
            completed_at DATETIME,
            search_plan_json VARCHAR DEFAULT '{}',
            summary_json VARCHAR DEFAULT '{}',
            error_message VARCHAR,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
        """
    )

    # source_result table (needed for FK references)
    conn.execute(
        """
        CREATE TABLE source_result (
            id INTEGER PRIMARY KEY,
            search_run_id INTEGER NOT NULL,
            source_name VARCHAR NOT NULL,
            result_type VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            query_json VARCHAR NOT NULL,
            normalized_result_json VARCHAR NOT NULL,
            raw_result_json VARCHAR NOT NULL,
            error_message VARCHAR,
            created_at DATETIME NOT NULL
        )
        """
    )

    # Insert a vacation (id=1)
    conn.execute(
        "INSERT INTO vacation (slug, title, origin, destination, date_mode, manifest_json, number_of_travelers, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("test-trip", "Old Schema Test Trip", "PIT", "MOT", "flexible_dates", json.dumps({"key": "value"}), 2, NOW_STR, NOW_STR),
    )

    # Insert a search_run (id=1)
    conn.execute(
        "INSERT INTO search_run (vacation_id, status, trigger_type, started_at, completed_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, "completed", "manual", NOW_STR, NOW_STR, NOW_STR, NOW_STR),
    )

    # Insert deal_candidate rows (old schema — no is_mock column)
    conn.execute(
        "INSERT INTO deal_candidate (vacation_id, search_run_id, candidate_type, title, status, total_price, currency, normalized_result_json, source_links_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, 1, "flight_only", "MOCK Flight Deal", "valid", 250.0, "USD", json.dumps({"mock": True}), "[]", NOW_STR),
    )
    conn.execute(
        "INSERT INTO deal_candidate (vacation_id, search_run_id, candidate_type, title, status, total_price, currency, normalized_result_json, source_links_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, 1, "flight_only", "Real Flight Deal", "valid", 320.0, "USD", json.dumps({"mock": False}), "[]", NOW_STR),
    )

    # Insert price_snapshot rows (old schema — no is_mock column)
    conn.execute(
        "INSERT INTO price_snapshot (vacation_id, search_run_id, quote_type, source_name, label, total_price, currency, normalized_json, captured_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, 1, "flight", "mock_travel", "MOCK Flight Label", 200.0, "USD", json.dumps({"mock": True}), NOW_STR, NOW_STR),
    )
    conn.execute(
        "INSERT INTO price_snapshot (vacation_id, search_run_id, quote_type, source_name, label, total_price, currency, normalized_json, captured_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, 1, "flight", "trvl", "Real Flight Label", 350.0, "USD", json.dumps({"mock": False}), NOW_STR, NOW_STR),
    )

    conn.commit()
    conn.close()

    monkeypatch.setenv("VACATION_DEAL_DB_URL", f"sqlite:///{db_path}")
    _reset_engine_cache()

    yield db_path


def test_vacation_detail_loads_after_old_schema_migration(old_schema_db):
    """Vacancy detail page loads without HTTP 500 after migration."""
    from app.web.routes import vacation_detail

    init_db()  # triggers column addition + backfill

    with Session(get_engine()) as session:
        response = vacation_detail(1, request=None, session=session)

    assert response is not None
    assert response.status_code == 200


def test_price_history_loads_after_old_schema_migration(old_schema_db):
    """Price history page loads without HTTP 500 after migration."""
    from app.web.routes import price_history_page

    init_db()

    with Session(get_engine()) as session:
        response = price_history_page(1, request=None, session=session, include_mock=0)

    assert response is not None
    assert response.status_code == 200


def test_mock_rows_hidden_by_default_after_migration(old_schema_db):
    """Default price history excludes mock rows after migration."""
    init_db()

    with Session(get_engine()) as session:
        history = vacation_price_history(session, vacation_id=1, include_mock=False)

    # Snapshots should exclude the mock_travel row
    snapshot_sources = [s["source_name"] for s in history["snapshots"]]
    assert "trvl" in snapshot_sources
    assert "mock_travel" not in snapshot_sources

    # Deals should exclude the MOCK Flight Deal row
    deal_titles = [d["label"] for d in history["deals"]]
    assert "Real Flight Deal" in deal_titles
    assert "MOCK Flight Deal" not in deal_titles


def test_include_mock_shows_backfilled_mock_rows(old_schema_db):
    """include_mock=1 shows backfilled mock rows with is_mock flag."""
    init_db()

    with Session(get_engine()) as session:
        history = vacation_price_history(session, vacation_id=1, include_mock=True)

    # Snapshots should include the mock_travel row
    snapshot_sources = [s["source_name"] for s in history["snapshots"]]
    assert "mock_travel" in snapshot_sources

    # Verify is_mock flag is set correctly on backfilled rows
    snapshot_by_source = {s["source_name"]: s for s in history["snapshots"]}
    assert snapshot_by_source["mock_travel"]["is_mock"] == True
    assert snapshot_by_source["trvl"]["is_mock"] == False

    deal_titles = [d["label"] for d in history["deals"]]
    assert "MOCK Flight Deal" in deal_titles
    assert "Real Flight Deal" in deal_titles


def test_real_rows_remain_visible_after_migration(old_schema_db):
    """Real trvl rows remain visible as real (is_mock=false) after migration."""
    init_db()

    with Session(get_engine()) as session:
        history = vacation_price_history(session, vacation_id=1, include_mock=True)

    snapshot_by_source = {s["source_name"]: s for s in history["snapshots"]}
    assert snapshot_by_source["trvl"]["is_mock"] == False


def test_deal_candidate_is_mock_queryable_after_migration(old_schema_db):
    """DealCandidate.is_mock can be queried via SQLModel after migration."""
    init_db()

    with Session(get_engine()) as session:
        deals = session.exec(select(DealCandidate).where(DealCandidate.vacation_id == 1)).all()

    assert len(deals) == 2
    mock_deal = next(d for d in deals if "MOCK" in d.title)
    real_deal = next(d for d in deals if "Real" in d.title)

    assert mock_deal.is_mock == True
    assert real_deal.is_mock == False


def test_price_snapshot_is_mock_queryable_after_migration(old_schema_db):
    """PriceSnapshot.is_mock can be queried via SQLModel after migration."""
    init_db()

    with Session(get_engine()) as session:
        snapshots = session.exec(select(PriceSnapshot).where(PriceSnapshot.vacation_id == 1)).all()

    assert len(snapshots) == 2
    mock_snap = next(s for s in snapshots if "MOCK" in s.label)
    real_snap = next(s for s in snapshots if s.source_name == "trvl")

    assert mock_snap.is_mock == True
    assert real_snap.is_mock == False


def test_init_db_twice_routes_still_work(old_schema_db):
    """Running init_db twice and then querying routes does not error."""
    init_db()
    init_db()  # idempotent

    from app.web.routes import price_history_page, vacation_detail

    with Session(get_engine()) as session:
        resp1 = vacation_detail(1, request=None, session=session)
        assert resp1.status_code == 200

        resp2 = price_history_page(1, request=None, session=session, include_mock=0)
        assert resp2.status_code == 200
