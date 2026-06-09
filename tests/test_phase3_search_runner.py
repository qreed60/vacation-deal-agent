import json
import os
import subprocess
import sys

import pytest
from sqlmodel import Session, SQLModel, select

from app.adapters import fast_flights_adapter
from app.db.models import DealCandidate, PriceSnapshot, SearchRun, SourceResult
from app.db.session import get_engine, init_db
from app.services.manifest_io import vacation_from_manifest
from app.services.search_runner import run_search_once


@pytest.fixture()
def session(tmp_path, monkeypatch):
    db_path = tmp_path / "vacation_deals.sqlite3"
    monkeypatch.setenv("VACATION_DEAL_DB_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("SEARXNG_BASE_URL", "")
    monkeypatch.setenv("AMADEUS_ENABLED", "false")
    monkeypatch.setenv("GOOGLE_PLACES_ENABLED", "false")
    monkeypatch.setenv("SERPAPI_ENABLED", "false")
    monkeypatch.setenv("FAST_FLIGHTS_ENABLED", "false")
    SQLModel.metadata.drop_all(get_engine())
    init_db()
    with Session(get_engine()) as db_session:
        yield db_session


def manifest(**overrides):
    data = {
        "title": "Phase 3 skipped-source trip",
        "status": "active",
        "number_of_travelers": 2,
        "travelers": [],
        "origin": "Pittsburgh",
        "destination": "Orlando",
        "date_mode": "fixed_dates",
        "start_date": "2026-07-10",
        "end_date": "2026-07-17",
        "nights_min": None,
        "nights_target": 7,
        "nights_max": None,
        "hotel_needed": True,
        "airfare_needed": True,
        "rental_car_needed": False,
        "special_accommodations": "",
    }
    data.update(overrides)
    return data


def create_vacation(session, **overrides):
    return vacation_from_manifest(session, manifest(**overrides))


def test_real_sources_missing_config_create_skipped_rows(session):
    vacation = create_vacation(session)

    search_run = run_search_once(
        vacation.id,
        "manual",
        session=session,
        use_real_sources=True,
        use_mock=False,
    )
    results = session.exec(select(SourceResult).where(SourceResult.search_run_id == search_run.id)).all()

    assert search_run.status == "completed"
    assert len(results) == 9
    assert {result.status for result in results} == {"skipped"}
    assert {result.source_name for result in results} == {
        "searxng",
        "amadeus",
        "fast_flights",
        "google_places",
        "serpapi_google_flights",
        "serpapi_google_hotels",
    }
    assert all(result.error_message for result in results)
    summary = json.loads(session.get(SearchRun, search_run.id).summary_json)
    assert summary["real_sources"] is True
    assert summary["mock"] is False
    assert summary["source_status_counts"] == {"skipped": 9}


def test_phase2_mock_behavior_still_available(session):
    vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)

    search_run = run_search_once(vacation.id, "manual", session=session)
    results = session.exec(select(SourceResult).where(SourceResult.search_run_id == search_run.id)).all()

    assert search_run.status == "completed"
    assert len(results) == 1
    assert results[0].source_name == "mock_travel"
    assert results[0].status == "mock"


def test_cli_real_sources_skips_without_credentials(session):
    vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)
    env = os.environ.copy()
    env["SEARXNG_BASE_URL"] = ""
    env["AMADEUS_ENABLED"] = "false"
    env["GOOGLE_PLACES_ENABLED"] = "false"
    env["SERPAPI_ENABLED"] = "false"
    env["FAST_FLIGHTS_ENABLED"] = "false"

    result = subprocess.run(
        [sys.executable, "scripts/run_search_once.py", "--vacation-id", str(vacation.id), "--use-real-sources"],
        cwd=os.getcwd(),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"vacation_id={vacation.id}" in result.stdout
    assert "status=completed" in result.stdout
    assert "source_results=4" in result.stdout


def test_fast_flights_success_creates_source_snapshot_and_deal(session, monkeypatch):
    vacation = create_vacation(
        session,
        origin="PIT",
        destination="ORD",
        hotel_needed=False,
        airfare_needed=True,
        number_of_travelers=1,
    )
    monkeypatch.setenv("FAST_FLIGHTS_ENABLED", "true")

    def fake_search(query, **kwargs):
        return {
            "status": "completed",
            "normalized_result": {
                "source_name": "fast_flights",
                "result_type": "flight",
                "offers": [
                    {
                        "component_type": "flight",
                        "component_type_label": "Airfare",
                        "source_name": "fast_flights",
                        "result_type": "flight",
                        "provider": "American",
                        "label": "American PIT to ORD",
                        "total_price": 296.0,
                        "currency": "USD",
                        "search_reference_url": "https://www.google.com/search?q=American+flight+PIT+ORD",
                        "link_type": "search_reference",
                        "link_label": "Search reference",
                        "mock": False,
                    }
                ],
            },
            "raw_result": {"diagnostic_raw": {"flight_count": 1}},
            "error_message": None,
        }

    monkeypatch.setattr(fast_flights_adapter, "search_fast_flights", fake_search)

    search_run = run_search_once(vacation.id, "manual", session=session, use_real_sources=True, use_mock=False)
    results = session.exec(select(SourceResult).where(SourceResult.search_run_id == search_run.id)).all()
    snapshots = session.exec(select(PriceSnapshot).where(PriceSnapshot.search_run_id == search_run.id)).all()
    deals = session.exec(select(DealCandidate).where(DealCandidate.search_run_id == search_run.id)).all()

    fast_result = [result for result in results if result.source_name == "fast_flights"][0]
    assert fast_result.status == "completed"
    assert search_run.status == "completed"
    assert len(snapshots) == 1
    assert snapshots[0].source_name == "fast_flights"
    assert snapshots[0].provider == "American"
    assert snapshots[0].total_price == 296.0
    assert snapshots[0].currency == "USD"
    component = json.loads(snapshots[0].normalized_json)
    assert component["link_type"] == "search_reference"
    assert component["link_label"] == "Search reference"
    assert component["search_reference_url"]
    assert deals
    deal_component = json.loads(deals[0].normalized_result_json)["component_summary"][0]
    assert deal_component["provider"] == "American"
    assert deal_component["source_name"] == "fast_flights"


def test_fast_flights_no_provider_creates_source_but_no_snapshot(session, monkeypatch):
    vacation = create_vacation(session, origin="JFK", destination="LAX", hotel_needed=False, airfare_needed=True)
    monkeypatch.setenv("FAST_FLIGHTS_ENABLED", "true")

    monkeypatch.setattr(
        fast_flights_adapter,
        "search_fast_flights",
        lambda query, **kwargs: {
            "status": "completed",
            "normalized_result": {"source_name": "fast_flights", "result_type": "flight", "offers": [], "unpriced_result_count": 1},
            "raw_result": {"diagnostic_raw": {"flight_count": 1}},
            "error_message": None,
        },
    )

    search_run = run_search_once(vacation.id, "manual", session=session, use_real_sources=True, use_mock=False)
    results = session.exec(select(SourceResult).where(SourceResult.search_run_id == search_run.id)).all()
    snapshots = session.exec(select(PriceSnapshot).where(PriceSnapshot.search_run_id == search_run.id)).all()

    assert [result for result in results if result.source_name == "fast_flights"][0].status == "completed"
    assert snapshots == []


def test_fast_flights_no_numeric_price_creates_source_but_no_snapshot(session, monkeypatch):
    vacation = create_vacation(session, origin="JFK", destination="LAX", hotel_needed=False, airfare_needed=True)
    monkeypatch.setenv("FAST_FLIGHTS_ENABLED", "true")

    monkeypatch.setattr(
        fast_flights_adapter,
        "search_fast_flights",
        lambda query, **kwargs: {
            "status": "completed",
            "normalized_result": {"source_name": "fast_flights", "result_type": "flight", "offers": [], "unpriced_result_count": 1},
            "raw_result": {"diagnostic_raw": {"flight_count": 1}},
            "error_message": None,
        },
    )

    search_run = run_search_once(vacation.id, "manual", session=session, use_real_sources=True, use_mock=False)
    results = session.exec(select(SourceResult).where(SourceResult.search_run_id == search_run.id)).all()
    snapshots = session.exec(select(PriceSnapshot).where(PriceSnapshot.search_run_id == search_run.id)).all()

    assert [result for result in results if result.source_name == "fast_flights"][0].status == "completed"
    assert snapshots == []


def test_fast_flights_error_does_not_crash_search_run(session, monkeypatch):
    vacation = create_vacation(session, origin="PIT", destination="MCO", hotel_needed=False, airfare_needed=True)
    monkeypatch.setenv("FAST_FLIGHTS_ENABLED", "true")

    monkeypatch.setattr(
        fast_flights_adapter,
        "search_fast_flights",
        lambda query, **kwargs: {
            "status": "error",
            "normalized_result": {"source_name": "fast_flights", "result_type": "flight", "offers": [], "reason": "upstream error"},
            "raw_result": {"diagnostic_error_excerpt": "upstream error"},
            "error_message": "upstream error",
        },
    )

    search_run = run_search_once(vacation.id, "manual", session=session, use_real_sources=True, use_mock=False)
    results = session.exec(select(SourceResult).where(SourceResult.search_run_id == search_run.id)).all()

    fast_result = [result for result in results if result.source_name == "fast_flights"][0]
    assert search_run.status == "completed"
    assert fast_result.status == "error"
    assert fast_result.error_message == "upstream error"
