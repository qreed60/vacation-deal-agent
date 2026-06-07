import json

import pytest
from sqlmodel import Session, SQLModel

from app.db.session import get_engine, init_db
from app.web.routes import (
    create_vacation,
    dashboard,
    export_vacation,
    import_manifest as import_manifest_route,
    update_vacation,
    vacation_detail,
)


@pytest.fixture()
def session(tmp_path, monkeypatch):
    db_path = tmp_path / "vacation_deals.sqlite3"
    monkeypatch.setenv("VACATION_DEAL_DB_URL", f"sqlite:///{db_path}")
    SQLModel.metadata.drop_all(get_engine())
    init_db()
    with Session(get_engine()) as db_session:
        yield db_session


def form_data(**overrides):
    data = {
        "title": "Summer in Lisbon",
        "status": "active",
        "number_of_travelers": 2,
        "travelers_json": "[]",
        "origin": "JFK",
        "destination": "Lisbon",
        "date_mode": "fixed_dates",
        "start_date": "2026-07-10",
        "end_date": "2026-07-17",
        "nights_min": "",
        "nights_target": "7",
        "nights_max": "",
        "hotel_needed": True,
        "airfare_needed": True,
        "rental_car_needed": False,
        "special_accommodations": "",
    }
    data.update(overrides)
    return data


def create_test_vacation(session):
    response = create_vacation(request=None, session=session, **form_data())
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[-1])


def test_create_and_list_vacation(session):
    vacation_id = create_test_vacation(session)

    detail = vacation_detail(vacation_id, request=None, session=session)
    assert detail.status_code == 200
    assert detail.context["vacation"].title == "Summer in Lisbon"

    listing = dashboard(request=None, session=session)
    assert listing.status_code == 200
    assert len(listing.context["vacations"]) == 1
    assert listing.context["vacations"][0].destination == "Lisbon"


def test_edit_vacation(session):
    vacation_id = create_test_vacation(session)

    response = update_vacation(
        vacation_id,
        request=None,
        session=session,
        **form_data(title="Updated Lisbon", status="paused", destination="Porto"),
    )

    assert response.status_code == 303
    detail = vacation_detail(vacation_id, request=None, session=session)
    assert detail.context["vacation"].title == "Updated Lisbon"
    assert detail.context["vacation"].status == "paused"
    assert detail.context["vacation"].destination == "Porto"


def test_export_manifest(session):
    vacation_id = create_test_vacation(session)

    response = export_vacation(vacation_id, session=session)
    manifest = json.loads(response.body)

    assert response.status_code == 200
    assert manifest["title"] == "Summer in Lisbon"
    assert manifest["destination"] == "Lisbon"
    assert manifest["hotel_needed"] is True


def test_import_manifest(session):
    manifest = {
        "title": "Imported trip",
        "status": "active",
        "number_of_travelers": 3,
        "travelers": [],
        "origin": "LAX",
        "destination": "Tokyo",
        "date_mode": "flexible_window",
        "nights_min": 5,
        "nights_target": 7,
        "nights_max": 9,
        "hotel_needed": True,
        "airfare_needed": True,
        "rental_car_needed": False,
        "special_accommodations": "Quiet room",
    }

    response = import_manifest_route(request=None, manifest_json=json.dumps(manifest), session=session)

    assert response.status_code == 303
    vacation_id = int(response.headers["location"].rsplit("/", 1)[-1])
    detail = vacation_detail(vacation_id, request=None, session=session)
    assert detail.context["vacation"].title == "Imported trip"
    assert detail.context["vacation"].destination == "Tokyo"


def test_invalid_import_fails_cleanly(session):
    response = import_manifest_route(
        request=None,
        manifest_json=json.dumps({"title": "Missing fields"}),
        session=session,
    )

    assert response.status_code == 400
    assert "Missing required fields" in response.context["error"]
