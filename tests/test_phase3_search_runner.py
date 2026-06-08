import json
import os
import subprocess
import sys

import pytest
from sqlmodel import Session, SQLModel, select

from app.db.models import SearchRun, SourceResult
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
    assert len(results) == 8
    assert {result.status for result in results} == {"skipped"}
    assert {result.source_name for result in results} == {
        "searxng",
        "amadeus",
        "google_places",
        "serpapi_google_flights",
        "serpapi_google_hotels",
    }
    assert all(result.error_message for result in results)
    summary = json.loads(session.get(SearchRun, search_run.id).summary_json)
    assert summary["real_sources"] is True
    assert summary["mock"] is False
    assert summary["source_status_counts"] == {"skipped": 8}


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
    assert "source_results=3" in result.stdout
