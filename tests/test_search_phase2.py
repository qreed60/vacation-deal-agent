import json
import os
import subprocess
import sys

import pytest
from sqlmodel import Session, SQLModel, select

from app.db.models import SearchRun, SourceResult
from app.db.session import get_engine, init_db
from app.services.manifest_io import vacation_from_manifest
from app.services.search_planner import build_search_plan
from app.services.search_runner import run_search_once
from app.web.routes import create_search_run


@pytest.fixture()
def session(tmp_path, monkeypatch):
    db_path = tmp_path / "vacation_deals.sqlite3"
    monkeypatch.setenv("VACATION_DEAL_DB_URL", f"sqlite:///{db_path}")
    SQLModel.metadata.drop_all(get_engine())
    init_db()
    with Session(get_engine()) as db_session:
        yield db_session


def manifest(**overrides):
    data = {
        "title": "Mock search trip",
        "status": "active",
        "number_of_travelers": 2,
        "travelers": [{"name": "A", "age": 34}, {"name": "B", "age": 35}],
        "origin": "JFK",
        "destination": "Lisbon",
        "date_mode": "fixed_dates",
        "start_date": "2026-07-10",
        "end_date": "2026-07-17",
        "nights_min": None,
        "nights_target": 7,
        "nights_max": None,
        "hotel_needed": False,
        "airfare_needed": True,
        "rental_car_needed": False,
        "special_accommodations": "Quiet room",
    }
    data.update(overrides)
    return data


def create_vacation(session, **overrides):
    return vacation_from_manifest(session, manifest(**overrides))


def test_search_plan_generation_airfare_only(session):
    vacation = create_vacation(session, airfare_needed=True, hotel_needed=False, rental_car_needed=False)

    plan = build_search_plan(vacation)

    assert plan["requested_services"] == {"flight": True, "hotel": False, "rental_car": False}
    assert [query["result_type"] for query in plan["queries"]] == ["flight"]
    assert plan["queries"][0]["query"]["origin"] == "JFK"
    assert plan["queries"][0]["query"]["mock"] is True


def test_search_plan_generation_hotel_and_car(session):
    vacation = create_vacation(session, airfare_needed=False, hotel_needed=True, rental_car_needed=True)

    plan = build_search_plan(vacation)

    assert plan["requested_services"] == {"flight": False, "hotel": True, "rental_car": True}
    assert [query["result_type"] for query in plan["queries"]] == ["hotel", "rental_car"]
    assert all(query["source_name"] == "mock_travel" for query in plan["queries"])


def test_mock_search_runner_creates_search_run(session):
    vacation = create_vacation(session)

    search_run = run_search_once(vacation.id, "manual", session=session)

    persisted = session.get(SearchRun, search_run.id)
    assert persisted.status == "completed"
    assert persisted.trigger_type == "manual"
    assert json.loads(persisted.summary_json)["mock"] is True


def test_mock_search_runner_creates_source_result_rows(session):
    vacation = create_vacation(session, airfare_needed=True, hotel_needed=True, rental_car_needed=True)

    search_run = run_search_once(vacation.id, "manual", session=session)
    results = session.exec(select(SourceResult).where(SourceResult.search_run_id == search_run.id)).all()

    assert [result.result_type for result in results] == ["flight", "hotel", "rental_car"]
    assert all(result.status == "mock" for result in results)
    assert all(json.loads(result.normalized_result_json)["mock"] is True for result in results)
    normalized = {result.result_type: json.loads(result.normalized_result_json) for result in results}
    assert normalized["flight"]["provider"] == "Mock Air"
    assert normalized["flight"]["provider_code"] == "MA"
    assert normalized["flight"]["search_reference_url"]
    assert normalized["flight"]["link_label"] == "Search reference"
    assert normalized["hotel"]["provider"] == "Mock Harbor Hotel"
    assert normalized["hotel"]["search_reference_url"]
    assert normalized["rental_car"]["provider"] == "Mock Rent-A-Car"
    assert normalized["rental_car"]["search_reference_url"]


def test_cli_vacation_id_works(session):
    vacation = create_vacation(session)
    env = os.environ.copy()

    result = subprocess.run(
        [sys.executable, "scripts/run_search_once.py", "--vacation-id", str(vacation.id)],
        cwd=os.getcwd(),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"vacation_id={vacation.id}" in result.stdout
    assert "status=completed" in result.stdout
    assert "source_results=1" in result.stdout


def test_web_post_search_runs_creates_run(session):
    vacation = create_vacation(session)
    vacation_id = vacation.id

    response = create_search_run(vacation_id, session=session)

    assert response.status_code == 303
    assert response.headers["location"] == f"/vacations/{vacation_id}"
    runs = session.exec(select(SearchRun).where(SearchRun.vacation_id == vacation_id)).all()
    assert len(runs) == 1
    assert runs[0].status == "completed"
