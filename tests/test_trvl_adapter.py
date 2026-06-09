import json
import os
import stat

from sqlmodel import Session, SQLModel, select

from app.adapters import trvl_adapter
from app.db.models import PriceSnapshot, SourceResult
from app.db.session import get_engine, init_db
from app.services.manifest_io import vacation_from_manifest
from app.services.quote_normalizer import snapshots_from_source_result
from app.services.search_planner import deterministic_json
from app.services.search_runner import run_search_once


def make_trvl(tmp_path, body: str):
    path = tmp_path / "trvl"
    path.write_text("#!/usr/bin/env python3\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def manifest(**overrides):
    data = {
        "title": "trvl trip",
        "status": "active",
        "number_of_travelers": 2,
        "travelers": [],
        "origin": "Pittsburgh",
        "destination": "Minot",
        "date_mode": "fixed_dates",
        "start_date": "2026-09-18",
        "end_date": "2026-09-21",
        "nights_min": None,
        "nights_target": 3,
        "nights_max": None,
        "hotel_needed": False,
        "airfare_needed": True,
        "rental_car_needed": False,
        "special_accommodations": "",
    }
    data.update(overrides)
    return data


def create_vacation(session, **overrides):
    return vacation_from_manifest(session, manifest(**overrides))


def source_result(session, search_run_id, result_type, normalized, *, status="completed", source_name="trvl", raw_result=None):
    result = SourceResult(
        search_run_id=search_run_id,
        source_name=source_name,
        result_type=result_type,
        status=status,
        query_json="{}",
        normalized_result_json=deterministic_json(normalized),
        raw_result_json=deterministic_json(raw_result or {}),
    )
    session.add(result)
    session.commit()
    session.refresh(result)
    return result


def test_missing_trvl_binary_returns_skipped_missing_dependency(monkeypatch):
    monkeypatch.setattr(trvl_adapter, "resolve_trvl_binary", lambda configured_path=None: None)
    result = trvl_adapter.search_trvl_flights(
        {"origin": "PIT", "destination": "MOT", "start_date": "2026-09-18", "end_date": "2026-09-21", "number_of_travelers": 1},
        enabled=True,
        binary_path="/tmp/does-not-exist-trvl",
    )

    assert result["status"] == "skipped"
    assert result["error_message"] == "TRVL_ENABLED=true but trvl binary was not found"
    assert result["normalized_result"]["status_reason"] == "missing_dependency"


def test_trvl_binary_path_is_honored(tmp_path):
    binary = make_trvl(
        tmp_path,
        """
import json, sys
print(json.dumps({"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "Path Air"}]}))
""",
    )

    result = trvl_adapter.search_trvl_flights(
        {"origin": "PIT", "destination": "MOT", "start_date": "2026-09-18", "number_of_travelers": 1},
        enabled=True,
        binary_path=str(binary),
    )

    assert result["status"] == "completed"
    command = result["normalized_result"]["command"]["argv"]
    assert command[0] == str(binary)
    assert result["normalized_result"]["offers"][0]["provider"] == "Path Air"


def test_stderr_warning_with_success_json_does_not_fail(tmp_path):
    binary = make_trvl(
        tmp_path,
        """
import json, sys
print("WARN provider partial", file=sys.stderr)
print(json.dumps({"success": True, "flights": [{"price": 210, "currency": "USD", "provider": "Warn Air"}]}))
""",
    )

    result = trvl_adapter.search_trvl_flights(
        {"origin": "PIT", "destination": "MOT", "start_date": "2026-09-18", "number_of_travelers": 1},
        enabled=True,
        binary_path=str(binary),
    )

    assert result["status"] == "completed"
    assert result["normalized_result"]["stderr_warnings"] == ["WARN provider partial"]


def test_flight_json_with_airline_provider_price_creates_snapshot(tmp_path, monkeypatch):
    binary = make_trvl(
        tmp_path,
        """
import json
print(json.dumps({"success": True, "flights": [{"price": 300, "currency": "USD", "provider": "Google Flights", "legs": [{"airline": "Delta Air Lines", "flight_number": "DL 100", "departure": "08:00", "arrival": "11:00"}], "booking_url": "https://example.test/dl"}]}))
""",
    )
    result = trvl_adapter.search_trvl_flights(
        {"origin": "PIT", "destination": "MOT", "start_date": "2026-09-18", "end_date": "2026-09-21", "number_of_travelers": 1},
        enabled=True,
        binary_path=str(binary),
    )

    offer = result["normalized_result"]["offers"][0]
    assert offer["provider"] == "Delta Air Lines"
    assert offer["trvl_provider"] == "Google Flights"
    assert offer["source_url"] == "https://example.test/dl"
    assert offer["link_type"] == "exact_source"


def test_flight_provider_falls_back_to_provider_and_cheapest_source():
    raw = {
        "success": True,
        "flights": [
            {"price": 350, "currency": "USD", "provider": "Kiwi", "legs": [{"airline": ""}]},
            {"price": 360, "currency": "USD", "provider": "", "cheapest_source": "Skiplagged"},
        ],
    }
    normalized = trvl_adapter.normalize_flights(
        raw,
        {
            "origin_airport": "PIT",
            "destination_airport": "MOT",
            "departure_date": "2026-09-18",
            "return_date": "2026-09-21",
        },
        max_results=20,
    )

    assert [offer["provider"] for offer in normalized["offers"]] == ["Kiwi", "Skiplagged"]
    assert normalized["offers"][1]["cheapest_source"] == "Skiplagged"


def test_flight_lacking_provider_identity_creates_no_snapshot(tmp_path, monkeypatch):
    db_path = tmp_path / "db.sqlite3"
    monkeypatch.setenv("VACATION_DEAL_DB_URL", f"sqlite:///{db_path}")
    SQLModel.metadata.drop_all(get_engine())
    init_db()
    with Session(get_engine()) as session:
        vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)
        search_run = run_search_once(vacation.id, "manual", session=session, use_mock=False)
        result = source_result(
            session,
            search_run.id,
            "flight",
            trvl_adapter.normalize_flights(
                {"success": True, "flights": [{"price": 250, "currency": "USD"}]},
                {"origin_airport": "PIT", "destination_airport": "MOT"},
                max_results=20,
            ),
        )

        assert snapshots_from_source_result(vacation, result) == []


def test_pit_mot_query_json_contains_original_and_resolved_airports(tmp_path, monkeypatch):
    db_path = tmp_path / "db.sqlite3"
    monkeypatch.setenv("VACATION_DEAL_DB_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("SEARXNG_BASE_URL", "")
    monkeypatch.setenv("AMADEUS_ENABLED", "false")
    monkeypatch.setenv("GOOGLE_PLACES_ENABLED", "false")
    monkeypatch.setenv("SERPAPI_ENABLED", "false")
    monkeypatch.setenv("FAST_FLIGHTS_ENABLED", "false")
    monkeypatch.setenv("TRVL_ENABLED", "true")
    binary = make_trvl(
        tmp_path,
        """
import json
print(json.dumps({"success": True, "flights": [{"price": 300, "currency": "USD", "provider": "Delta"}]}))
""",
    )
    monkeypatch.setenv("TRVL_BINARY_PATH", str(binary))
    SQLModel.metadata.drop_all(get_engine())
    init_db()
    with Session(get_engine()) as session:
        vacation = create_vacation(session, origin="Pittsburgh", destination="Minot", hotel_needed=False, airfare_needed=True)
        search_run = run_search_once(vacation.id, "manual", session=session, use_real_sources=True, use_mock=False)
        trvl_result = session.exec(
            select(SourceResult).where(SourceResult.search_run_id == search_run.id).where(SourceResult.source_name == "trvl")
        ).one()
        query = json.loads(trvl_result.query_json)

    assert query["origin_value"] == "Pittsburgh"
    assert query["destination_value"] == "Minot"
    assert query["origin_airport"] == "PIT"
    assert query["destination_airport"] == "MOT"


def test_flight_results_sorted_deduplicated_and_bounded(monkeypatch):
    monkeypatch.setenv("TRVL_MAX_FLIGHT_RESULTS", "2")
    raw = {
        "success": True,
        "flights": [
            {"price": 500, "currency": "USD", "provider": "B", "departure": "10:00", "arrival": "12:00"},
            {"price": 100, "currency": "USD", "provider": "A", "departure": "08:00", "arrival": "10:00"},
            {"price": 100, "currency": "USD", "provider": "A", "departure": "08:00", "arrival": "10:00"},
            {"price": 200, "currency": "USD", "provider": "C", "departure": "09:00", "arrival": "11:00"},
        ],
    }
    normalized = trvl_adapter.normalize_flights(raw, {"origin_airport": "PIT", "destination_airport": "MOT"}, max_results=2)

    assert [offer["provider"] for offer in normalized["offers"]] == ["A", "C"]
    assert normalized["normalized_count"] == 2


def test_hotel_json_with_name_price_currency_creates_snapshot(tmp_path, monkeypatch):
    db_path = tmp_path / "db.sqlite3"
    monkeypatch.setenv("VACATION_DEAL_DB_URL", f"sqlite:///{db_path}")
    SQLModel.metadata.drop_all(get_engine())
    init_db()
    with Session(get_engine()) as session:
        vacation = create_vacation(session, hotel_needed=True, airfare_needed=False)
        search_run = run_search_once(vacation.id, "manual", session=session, use_mock=False)
        result = source_result(
            session,
            search_run.id,
            "hotel",
            trvl_adapter.normalize_hotels(
                {"success": True, "hotels": [{"name": "Hotel A", "price": 150, "currency": "USD"}]},
                {"destination_value": "Minot", "checkin": "2026-09-18", "checkout": "2026-09-21"},
                max_results=20,
            ),
        )

        snapshots = snapshots_from_source_result(vacation, result)

    assert len(snapshots) == 1
    assert snapshots[0].provider == "Hotel A"
    assert snapshots[0].total_price == 450


def test_hotel_preserves_nightly_price_nights_total_and_basis():
    normalized = trvl_adapter.normalize_hotels(
        {"success": True, "hotels": [{"name": "Hotel A", "price": 150, "currency": "USD"}]},
        {"destination_value": "Minot", "checkin": "2026-09-18", "checkout": "2026-09-21"},
        max_results=20,
    )
    hotel = normalized["hotels"][0]

    assert hotel["nightly_price"] == 150
    assert hotel["nights"] == 3
    assert hotel["total_price"] == 450
    assert hotel["price_basis"] == "nightly"


def test_hotel_results_sorted_deduplicated_and_bounded():
    raw = {
        "success": True,
        "hotels": [
            {"name": "B", "price": 300, "currency": "USD", "booking_url": "https://b.test"},
            {"name": "A", "price": 100, "currency": "USD", "booking_url": "https://a.test"},
            {"name": "A", "price": 100, "currency": "USD", "booking_url": "https://a.test"},
            {"name": "C", "price": 200, "currency": "USD", "booking_url": "https://c.test"},
        ],
    }

    normalized = trvl_adapter.normalize_hotels(
        raw,
        {"destination_value": "Chicago", "checkin": "2026-09-18", "checkout": "2026-09-21"},
        max_results=2,
    )

    assert [hotel["hotel_name"] for hotel in normalized["hotels"]] == ["A", "C"]
    assert normalized["normalized_count"] == 2


def test_nonzero_exit_without_valid_json_creates_error_result(tmp_path):
    binary = make_trvl(
        tmp_path,
        """
import sys
print("bad", file=sys.stderr)
sys.exit(7)
""",
    )

    result = trvl_adapter.search_trvl_flights(
        {"origin": "PIT", "destination": "MOT", "start_date": "2026-09-18", "number_of_travelers": 1},
        enabled=True,
        binary_path=str(binary),
    )

    assert result["status"] == "error"
    assert "exited with code 7" in result["error_message"]


def test_disabled_trvl_creates_skipped_result():
    result = trvl_adapter.search_trvl_hotels(
        {"destination": "Chicago", "start_date": "2026-09-18", "end_date": "2026-09-21", "number_of_travelers": 1},
        enabled=False,
        binary_path="",
    )

    assert result["status"] == "skipped"
    assert result["error_message"] == "TRVL_ENABLED=false"
