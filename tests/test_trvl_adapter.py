import json
import os
import stat
from unittest.mock import MagicMock, patch

from sqlmodel import Session, SQLModel, select

from app.adapters import trvl_adapter
from app.db.models import PriceSnapshot, SourceResult
from app.db.session import get_engine, init_db
from app.services.manifest_io import vacation_from_manifest
from app.services.quote_normalizer import snapshots_from_source_result
from app.services.search_planner import deterministic_json
from app.services.search_runner import run_search_once
from app.web import routes


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

    assert [offer["provider"] for offer in normalized["offers"]] == ["Kiwi"]


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


def test_full_label_minot_trip_reaches_trvl_with_resolution_and_traveler_summary(tmp_path, monkeypatch):
    db_path = tmp_path / "db.sqlite3"
    monkeypatch.setenv("VACATION_DEAL_DB_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("AIRPORT_INDEX_DB_PATH", str(tmp_path / "missing.sqlite3"))
    monkeypatch.setenv("SEARXNG_BASE_URL", "")
    monkeypatch.setenv("AMADEUS_ENABLED", "false")
    monkeypatch.setenv("GOOGLE_PLACES_ENABLED", "false")
    monkeypatch.setenv("SERPAPI_ENABLED", "false")
    monkeypatch.setenv("FAST_FLIGHTS_ENABLED", "false")
    monkeypatch.setenv("TRVL_ENABLED", "true")
    binary = make_trvl(
        tmp_path,
        """
import json, sys
print(json.dumps({"success": True, "argv": sys.argv, "flights": [{"price": 456, "currency": "USD", "provider": "Delta"}]}))
""",
    )
    monkeypatch.setenv("TRVL_BINARY_PATH", str(binary))
    SQLModel.metadata.drop_all(get_engine())
    init_db()
    with Session(get_engine()) as session:
        vacation = create_vacation(
            session,
            origin="Pittsburgh, Pennsylvania, United States",
            destination="Minot, North Dakota, United States",
            number_of_travelers=4,
            travelers=[
                {"name": "Hanna", "age": 30},
                {"name": "Emsley", "age": 5},
                {"name": "Willa", "age": 3},
                {"name": "Everhett", "age": 0},
            ],
            hotel_needed=False,
            airfare_needed=True,
        )
        search_run = run_search_once(vacation.id, "manual", session=session, use_real_sources=True, use_mock=False)
        trvl_result = session.exec(
            select(SourceResult).where(SourceResult.search_run_id == search_run.id).where(SourceResult.source_name == "trvl")
        ).one()
        query = json.loads(trvl_result.query_json)
        summary = json.loads(search_run.summary_json)
        normalized = json.loads(trvl_result.normalized_result_json)

    assert trvl_result.status == "completed"
    assert query["origin_airport"] == "PIT"
    assert query["destination_airport"] == "MOT"
    assert query["origin_resolution_status"] == "resolved"
    assert query["destination_resolution_status"] == "resolved"
    assert query["traveler_count"] == 4
    assert query["adult_count"] == 1
    assert query["child_count"] == 2
    assert query["infant_count"] == 1
    assert query["trvl_adults_passed"] == 4
    assert normalized["command"]["argv"][2:5] == ["PIT", "MOT", "2026-09-18"]
    assert summary["resolved_origin_airport"] == "PIT"
    assert summary["resolved_destination_airport"] == "MOT"
    assert summary["traveler_count"] == 4
    assert summary["adult_count"] == 1
    assert summary["child_count"] == 2
    assert summary["infant_count"] == 1


def test_unresolved_city_skips_trvl_with_precise_diagnostic(monkeypatch, tmp_path):
    monkeypatch.setenv("AIRPORT_INDEX_DB_PATH", str(tmp_path / "missing.sqlite3"))

    result = trvl_adapter.search_trvl_flights(
        {
            "origin": "Unknownville, ZZ, United States",
            "destination": "MOT",
            "start_date": "2026-09-18",
            "number_of_travelers": 1,
        },
        enabled=True,
        binary_path="/tmp/not-needed",
    )

    assert result["status"] == "skipped"
    assert result["error_message"] == "Could not resolve origin to an airport code: Unknownville, ZZ, United States"
    assert result["normalized_result"]["query"]["origin_resolution_status"] == "unresolved"
    assert result["normalized_result"]["source_name"] == "trvl"


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
    normalized = result["normalized_result"]
    command_result = normalized["command_results"][0]
    assert command_result["command_label"] == "round_trip"
    assert command_result["exit_code"] == 7
    assert command_result["argv"][2:5] == ["PIT", "MOT", "2026-09-18"]
    assert command_result["stderr_preview"] == "bad\n"
    assert "stdout_preview" in command_result
    assert "elapsed_seconds" in command_result
    assert command_result["resolved_origin_airport"] == "PIT"
    assert command_result["resolved_destination_airport"] == "MOT"


def test_ui_manual_route_passes_real_sources_without_mock(tmp_path, monkeypatch):
    db_path = tmp_path / "db.sqlite3"
    monkeypatch.setenv("VACATION_DEAL_DB_URL", f"sqlite:///{db_path}")
    SQLModel.metadata.drop_all(get_engine())
    init_db()
    captured = {}

    def fake_run_search_once(vacation_id, trigger_type, *, use_real_sources, use_mock, session):
        captured.update(
            vacation_id=vacation_id,
            trigger_type=trigger_type,
            use_real_sources=use_real_sources,
            use_mock=use_mock,
            session=session,
        )

    monkeypatch.setattr(routes, "run_search_once", fake_run_search_once)

    with Session(get_engine()) as session:
        vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)
        response = routes.create_search_run(vacation.id, session=session)

    assert response.status_code == 303
    assert captured["vacation_id"] == vacation.id
    assert captured["trigger_type"] == "manual"
    assert captured["use_real_sources"] is True
    assert captured["use_mock"] is False


def test_disabled_trvl_creates_skipped_result():
    result = trvl_adapter.search_trvl_hotels(
        {"destination": "Chicago", "start_date": "2026-09-18", "end_date": "2026-09-21", "number_of_travelers": 1},
        enabled=False,
        binary_path="",
    )

    assert result["status"] == "skipped"
    assert result["error_message"] == "TRVL_ENABLED=false"


# ── Risk filtering tests ──────────────────────────────────────────────


def test_hidden_city_warning_is_filtered_by_default():
    """hidden_city warning in stderr should filter the offer."""
    raw = {
        "success": True,
        "flights": [
            {"price": 200, "currency": "USD", "provider": "TestAir"},
        ],
    }

    # Ensure risky offers are blocked (default)
    os.environ["TRVL_ALLOW_RISKY_FLIGHT_OFFERS"] = "false"
    try:
        normalized = trvl_adapter.normalize_flights(
            raw,
            {"origin_airport": "PIT", "destination_airport": "MOT", "currency": "USD"},
            max_results=20,
            stderr="WARNING: hidden_city detected for PIT→ORD leg",
        )

        assert normalized["normalized_count"] == 0
        assert normalized["skipped_count"] == 1
        assert any(r["reason"] == "risky_offer" for r in normalized.get("skipped_reasons", []))
    finally:
        os.environ.pop("TRVL_ALLOW_RISKY_FLIGHT_OFFERS", None)


def test_self_connect_true_is_filtered_by_default():
    """self_connect=true on raw offer should filter the offer."""
    raw = {
        "success": True,
        "flights": [
            {"price": 200, "currency": "USD", "provider": "TestAir", "self_connect": True},
        ],
    }

    normalized = trvl_adapter.normalize_flights(
        raw,
        {"origin_airport": "PIT", "destination_airport": "MOT", "currency": "USD"},
        max_results=20,
    )

    assert normalized["normalized_count"] == 0
    assert normalized["skipped_count"] == 1
    assert any(r["reason"] == "risky_offer" for r in normalized.get("skipped_reasons", []))


def test_normal_nonstop_offer_is_kept():
    """Normal nonstop flight should pass through filtering."""
    raw = {
        "success": True,
        "flights": [
            {"price": 200, "currency": "USD", "provider": "Delta", "stops": 0},
        ],
    }

    normalized = trvl_adapter.normalize_flights(
        raw,
        {"origin_airport": "PIT", "destination_airport": "MOT", "currency": "USD"},
        max_results=20,
    )

    assert normalized["normalized_count"] == 1
    assert normalized["offers"][0]["provider"] == "Delta"
    assert normalized["skipped_count"] == 0


def test_normal_one_stop_offer_is_kept():
    """Normal one-stop flight should pass through filtering."""
    raw = {
        "success": True,
        "flights": [
            {
                "price": 250,
                "currency": "USD",
                "provider": "United",
                "stops": 1,
                "legs": [
                    {"carrier": "UA", "departure": "PIT", "arrival": "ORD"},
                    {"carrier": "UA", "departure": "ORD", "arrival": "MOT"},
                ],
            },
        ],
    }

    normalized = trvl_adapter.normalize_flights(
        raw,
        {"origin_airport": "PIT", "destination_airport": "MOT", "currency": "USD"},
        max_results=20,
    )

    assert normalized["normalized_count"] == 1
    # Provider is the airline code from legs (UA), not display name
    assert normalized["offers"][0]["provider"] == "UA"


def test_risky_offers_allowed_when_flag_enabled():
    """When TRVL_ALLOW_RISKY_FLIGHT_OFFERS=true, risky offers are kept."""
    raw = {
        "success": True,
        "flights": [
            {"price": 200, "currency": "USD", "provider": "Skiplagged", "self_connect": True},
        ],
    }

    os.environ["TRVL_ALLOW_RISKY_FLIGHT_OFFERS"] = "true"
    try:
        normalized = trvl_adapter.normalize_flights(
            raw,
            {"origin_airport": "PIT", "destination_airport": "MOT", "currency": "USD"},
            max_results=20,
        )

        assert normalized["normalized_count"] == 1
        assert normalized["skipped_count"] == 0
    finally:
        os.environ.pop("TRVL_ALLOW_RISKY_FLIGHT_OFFERS", None)


# ── Currency filtering tests ──────────────────────────────────────────


def test_currency_mismatch_filtered_when_required():
    """Currency mismatch should be filtered when TRVL_REQUIRE_CONFIGURED_CURRENCY=true."""
    raw = {
        "success": True,
        "flights": [
            {"price": 200, "currency": "EUR", "provider": "EuropeanAir"},
            {"price": 180, "currency": "USD", "provider": "USAir"},
        ],
    }

    os.environ["TRVL_REQUIRE_CONFIGURED_CURRENCY"] = "true"
    try:
        normalized = trvl_adapter.normalize_flights(
            raw,
            {"origin_airport": "PIT", "destination_airport": "MOT", "currency": "USD"},
            max_results=20,
        )

        assert normalized["normalized_count"] == 1
        assert normalized["offers"][0]["currency"] == "USD"
        assert normalized["skipped_count"] == 1
        assert any(r["reason"] == "currency_mismatch" for r in normalized.get("skipped_reasons", []))
    finally:
        os.environ.pop("TRVL_REQUIRE_CONFIGURED_CURRENCY", None)


def test_currency_mismatch_kept_when_not_required():
    """Currency mismatch should be kept when TRVL_REQUIRE_CONFIGURED_CURRENCY=false."""
    raw = {
        "success": True,
        "flights": [
            {"price": 200, "currency": "EUR", "provider": "EuropeanAir"},
            {"price": 180, "currency": "USD", "provider": "USAir"},
        ],
    }

    os.environ["TRVL_REQUIRE_CONFIGURED_CURRENCY"] = "false"
    try:
        normalized = trvl_adapter.normalize_flights(
            raw,
            {"origin_airport": "PIT", "destination_airport": "MOT", "currency": "USD"},
            max_results=20,
        )

        assert normalized["normalized_count"] == 2
        currencies = {offer["currency"] for offer in normalized["offers"]}
        assert currencies == {"EUR", "USD"}
    finally:
        os.environ.pop("TRVL_REQUIRE_CONFIGURED_CURRENCY", None)


# ── Skipped reasons tests ─────────────────────────────────────────────


def test_skipped_reasons_counted_in_normalized_result_json():
    """Skipped offers should have their reasons recorded in skipped_reasons."""
    raw = {
        "success": True,
        "flights": [
            {"price": 200, "currency": "EUR", "provider": "EuroAir"},
            {"price": None, "currency": "USD", "provider": "NoPriceAir"},
            {"price": 180, "currency": "USD", "provider": "Skiplagged", "self_connect": True},
        ],
    }

    os.environ["TRVL_REQUIRE_CONFIGURED_CURRENCY"] = "true"
    try:
        normalized = trvl_adapter.normalize_flights(
            raw,
            {"origin_airport": "PIT", "destination_airport": "MOT", "currency": "USD"},
            max_results=20,
        )

        assert normalized["skipped_count"] == 3
        reasons = [r["reason"] for r in normalized.get("skipped_reasons", [])]
        assert "currency_mismatch" in reasons
        assert "missing_data" in reasons
        assert "risky_offer" in reasons
    finally:
        os.environ.pop("TRVL_REQUIRE_CONFIGURED_CURRENCY", None)


# ── All-risky success=true tests ──────────────────────────────────────


def test_all_risky_returned_success_true_produces_completed_result():
    """When all offers are risky but trvl returns success=true, result should be completed with zero offers."""
    raw = {
        "success": True,
        "flights": [
            {"price": 200, "currency": "USD", "provider": "Skiplagged", "self_connect": True},
            {"price": 180, "currency": "USD", "provider": "HiddenCityAir"},
        ],
    }

    os.environ["TRVL_ALLOW_RISKY_FLIGHT_OFFERS"] = "false"
    try:
        normalized = trvl_adapter.normalize_flights(
            raw,
            {"origin_airport": "PIT", "destination_airport": "MOT", "currency": "USD"},
            max_results=20,
        )

        assert normalized["normalized_count"] == 0
        assert normalized["raw_count"] == 2
        assert normalized["skipped_count"] == 2
    finally:
        os.environ.pop("TRVL_ALLOW_RISKY_FLIGHT_OFFERS", None)


def test_mixed_safe_risky_currency_input_keeps_only_safe_offers():
    """Mixed input should keep only safe offers with configured currency."""
    raw = {
        "success": True,
        "flights": [
            {"price": 180, "currency": "USD", "provider": "SafeAir", "stops": 0},
            {"price": 200, "currency": "EUR", "provider": "EuroAir"},
            {"price": 150, "currency": "USD", "provider": "Skiplagged", "self_connect": True},
        ],
    }

    os.environ["TRVL_REQUIRE_CONFIGURED_CURRENCY"] = "true"
    try:
        normalized = trvl_adapter.normalize_flights(
            raw,
            {"origin_airport": "PIT", "destination_airport": "MOT", "currency": "USD"},
            max_results=20,
        )

        assert normalized["normalized_count"] == 1
        assert normalized["offers"][0]["provider"] == "SafeAir"
        assert normalized["skipped_count"] == 2
    finally:
        os.environ.pop("TRVL_REQUIRE_CONFIGURED_CURRENCY", None)


# ── Airport code cleanup tests ────────────────────────────────────────


def test_airport_codes_with_embedded_quotes_are_cleaned():
    """Airport codes with embedded quotes should be cleaned in normalized metadata."""
    query_json = {
        "origin_airport": "'PIT'",
        "destination_airport": '"ORD"',
        "currency": "USD",
    }

    raw = {
        "success": True,
        "flights": [
            {"price": 200, "currency": "USD", "provider": "Delta"},
        ],
    }

    normalized = trvl_adapter.normalize_flights(
        raw,
        query_json,
        max_results=20,
    )

    assert normalized["offers"][0]["origin"] == "PIT"
    assert normalized["offers"][0]["destination"] == "ORD"


def test_clean_airport_code_helper():
    """Test _clean_airport_code helper with various inputs."""
    assert trvl_adapter._clean_airport_code("'PIT'") == "PIT"
    assert trvl_adapter._clean_airport_code('"ORD"') == "ORD"
    assert trvl_adapter._clean_airport_code("PIT") == "PIT"
    assert trvl_adapter._clean_airport_code(None) is None
    assert trvl_adapter._clean_airport_code("''") is None


def test_hotel_normalization_not_broken_by_flight_risk_filtering():
    """Hotel normalization should work independently of flight risk filtering."""
    raw = {
        "success": True,
        "hotels": [
            {"name": "Hotel A", "price": 150, "currency": "USD"},
            {"name": "Hotel B", "price": 200, "currency": "EUR"},
        ],
    }

    os.environ["TRVL_REQUIRE_CONFIGURED_CURRENCY"] = "true"
    try:
        normalized = trvl_adapter.normalize_hotels(
            raw,
            {"destination_value": "Chicago", "checkin": "2026-09-18", "checkout": "2026-09-21", "currency": "USD"},
            max_results=20,
        )

        # Hotel normalization should NOT apply flight risk/currency filtering
        assert normalized["normalized_count"] == 2
    finally:
        os.environ.pop("TRVL_REQUIRE_CONFIGURED_CURRENCY", None)


def test_cheapest_source_skiplagged_is_filtered():
    """cheapest_source=Skiplagged should be filtered as risky."""
    raw = {
        "success": True,
        "flights": [
            {"price": 150, "currency": "USD", "provider": "Delta", "legs": [{"airline": "DL"}]},
            {"price": 120, "currency": "USD", "provider": "", "cheapest_source": "Skiplagged"},
        ],
    }

    normalized = trvl_adapter.normalize_flights(
        raw,
        {"origin_airport": "PIT", "destination_airport": "MOT", "currency": "USD"},
        max_results=20,
    )

    assert normalized["normalized_count"] == 1
    # Provider is airline code from legs (DL), not display name
    assert normalized["offers"][0]["provider"] == "DL"
    assert normalized["skipped_count"] == 1


def test_nested_self_connect_legs_are_filtered():
    """Self-connecting legs with different carriers should be filtered."""
    raw = {
        "success": True,
        "flights": [
            {
                "price": 200,
                "currency": "USD",
                "provider": "MetaSearch",
                "legs": [
                    {"carrier": "UA", "departure": "PIT", "arrival": "ORD"},
                    {"carrier": "AA", "departure": "ORD", "arrival": "MOT"},
                ],
            },
        ],
    }

    normalized = trvl_adapter.normalize_flights(
        raw,
        {"origin_airport": "PIT", "destination_airport": "MOT", "currency": "USD"},
        max_results=20,
    )

    assert normalized["normalized_count"] == 0
    assert normalized["skipped_count"] == 1


def test_same_carrier_one_stop_is_not_filtered():
    """One-stop with same carrier should NOT be filtered as self-connect."""
    raw = {
        "success": True,
        "flights": [
            {
                "price": 250,
                "currency": "USD",
                "provider": "United",
                "stops": 1,
                "legs": [
                    {"carrier": "UA", "departure": "PIT", "arrival": "ORD"},
                    {"carrier": "UA", "departure": "ORD", "arrival": "MOT"},
                ],
            },
        ],
    }

    normalized = trvl_adapter.normalize_flights(
        raw,
        {"origin_airport": "PIT", "destination_airport": "MOT", "currency": "USD"},
        max_results=20,
    )

    assert normalized["normalized_count"] == 1
    # Provider is the airline code from legs (UA), not display name
    assert normalized["offers"][0]["provider"] == "UA"


def test_separate_tickets_warning_is_filtered():
    """separate_tickets warning should filter the offer."""
    raw = {
        "success": True,
        "flights": [
            {"price": 200, "currency": "USD", "provider": "MetaAir"},
        ],
    }

    normalized = trvl_adapter.normalize_flights(
        raw,
        {"origin_airport": "PIT", "destination_airport": "MOT", "currency": "USD"},
        max_results=20,
        stderr="WARNING: separate_tickets required for this itinerary",
    )

    assert normalized["normalized_count"] == 0
    assert normalized["skipped_count"] == 1


def test_throwaway_warning_is_filtered():
    """throwaway warning should filter the offer."""
    raw = {
        "success": True,
        "flights": [
            {"price": 200, "currency": "USD", "provider": "MetaAir"},
        ],
    }

    normalized = trvl_adapter.normalize_flights(
        raw,
        {"origin_airport": "PIT", "destination_airport": "MOT", "currency": "USD"},
        max_results=20,
        stderr="WARNING: throwaway ticket pattern detected",
    )

    assert normalized["normalized_count"] == 0
    assert normalized["skipped_count"] == 1


def test_skiplagged_hack_warning_is_filtered():
    """skiplagged_hack warning should filter the offer."""
    raw = {
        "success": True,
        "flights": [
            {"price": 200, "currency": "USD", "provider": "MetaAir"},
        ],
    }

    normalized = trvl_adapter.normalize_flights(
        raw,
        {"origin_airport": "PIT", "destination_airport": "MOT", "currency": "USD"},
        max_results=20,
        stderr="WARNING: skiplagged_hack pattern detected for this route",
    )

    assert normalized["normalized_count"] == 0
    assert normalized["skipped_count"] == 1


def test_nested_warning_is_filtered():
    """nested warning should filter the offer."""
    raw = {
        "success": True,
        "flights": [
            {"price": 200, "currency": "USD", "provider": "MetaAir"},
        ],
    }

    normalized = trvl_adapter.normalize_flights(
        raw,
        {"origin_airport": "PIT", "destination_airport": "MOT", "currency": "USD"},
        max_results=20,
        stderr="WARNING: nested itinerary detected",
    )

    assert normalized["normalized_count"] == 0
    assert normalized["skipped_count"] == 1


def test_warnings_field_on_flight_object_is_checked():
    """warnings field on the flight object itself should be checked for risky terms."""
    raw = {
        "success": True,
        "flights": [
            {"price": 200, "currency": "USD", "provider": "MetaAir", "warnings": "hidden_city"},
        ],
    }

    normalized = trvl_adapter.normalize_flights(
        raw,
        {"origin_airport": "PIT", "destination_airport": "MOT", "currency": "USD"},
        max_results=20,
    )

    assert normalized["normalized_count"] == 0
    assert normalized["skipped_count"] == 1


def test_warnings_list_on_flight_object_is_checked():
    """warnings list on the flight object should be checked for risky terms."""
    raw = {
        "success": True,
        "flights": [
            {"price": 200, "currency": "USD", "provider": "MetaAir", "warnings": ["safe_warning", "hidden_city"]},
        ],
    }

    normalized = trvl_adapter.normalize_flights(
        raw,
        {"origin_airport": "PIT", "destination_airport": "MOT", "currency": "USD"},
        max_results=20,
    )

    assert normalized["normalized_count"] == 0
    assert normalized["skipped_count"] == 1


def test_clean_airport_code_helper():
    """Test _clean_airport_code helper with various inputs."""
    assert trvl_adapter._clean_airport_code("'PIT'") == "PIT"
    assert trvl_adapter._clean_airport_code('"ORD"') == "ORD"
    assert trvl_adapter._clean_airport_code("PIT") == "PIT"
    assert trvl_adapter._clean_airport_code(None) is None
    assert trvl_adapter._clean_airport_code("''") is None


# ── Broad discovery tests ───────────────────────────────────────────────


def test_broad_discovery_disabled_keeps_existing_behavior():
    """When broad discovery is disabled, search_trvl_flights behaves normally."""
    raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "TestAir"}]}

    with patch("app.adapters.trvl_adapter._run_trvl") as mock_run:
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            completed = MagicMock()
            completed.stdout = json.dumps(raw)
            completed.stderr = ""
            completed.returncode = 0
            completed.args = ["trvl", "flights"]
            completed.elapsed_seconds = 1.5
            mock_run.return_value = completed

            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=False,
            )

    assert result["status"] == "completed"
    assert result["normalized_result"]["offers"][0]["provider"] == "TestAir"
    # No broad_alternatives key when disabled
    assert "broad_alternatives" not in result["normalized_result"]


def test_broad_discovery_enabled_runs_round_trip_plus_one_way():
    """Broad discovery enabled should run round-trip + outbound one-way + return one-way."""
    raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "TestAir"}]}

    with patch("app.adapters.trvl_adapter._run_trvl") as mock_run:
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            completed = MagicMock()
            completed.stdout = json.dumps(raw)
            completed.stderr = ""
            completed.returncode = 0
            completed.args = ["trvl", "flights"]
            completed.elapsed_seconds = 1.5
            mock_run.return_value = completed

            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    # _run_trvl should be called 3 times: round-trip + outbound + return
    assert mock_run.call_count == 3

    calls_argv = [c[0][0] for c in mock_run.call_args_list]
    assert "flights" in calls_argv[0]
    assert "flights" in calls_argv[1]
    assert "flights" in calls_argv[2]

    # Broad alternatives should be present
    assert "broad_alternatives" in result["normalized_result"]
    broad = result["normalized_result"]["broad_alternatives"]
    search_types = [a["search_type"] for a in broad]
    assert "outbound_one_way" in search_types
    assert "return_one_way" in search_types


def test_safe_round_trip_offers_still_create_normal_offers():
    """Safe round-trip offers should still appear under normalized_result['offers']."""
    raw = {"success": True, "flights": [
        {"price": 200, "currency": "USD", "provider": "Delta"},
        {"price": 180, "currency": "USD", "provider": "United"},
    ]}

    with patch("app.adapters.trvl_adapter._run_trvl") as mock_run:
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            completed = MagicMock()
            completed.stdout = json.dumps(raw)
            completed.stderr = ""
            completed.returncode = 0
            completed.args = ["trvl", "flights"]
            completed.elapsed_seconds = 1.5
            mock_run.return_value = completed

            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    assert result["normalized_result"]["offers"][0]["total_price"] == 180
    assert result["normalized_result"]["offers"][1]["total_price"] == 200


def test_risky_one_way_alternatives_stored_under_broad_not_normal_offers():
    """Risky one-way alternatives should be in broad_alternatives, not normal offers."""
    safe_raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "Delta"}]}
    risky_raw = {
        "success": True,
        "flights": [
            {"price": 120, "currency": "USD", "provider": "", "cheapest_source": "Skiplagged"},
            {"price": 130, "currency": "USD", "provider": "HiddenCityAir"},
        ],
    }

    call_count = [0]

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        if "--return" in command:
            # Round-trip returns safe offer
            completed.stdout = json.dumps(safe_raw)
        else:
            # One-way searches return risky offers
            call_count[0] += 1
            completed.stdout = json.dumps(risky_raw)
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    # Normal offers should only contain the safe round-trip offer
    assert len(result["normalized_result"]["offers"]) == 1
    assert result["normalized_result"]["offers"][0]["provider"] == "Delta"

    # Broad alternatives should contain the risky ones
    broad = result["normalized_result"]["broad_alternatives"]
    total_broad = sum(a["normalized_count"] for a in broad)
    assert total_broad > 0


def test_broad_alternatives_do_not_create_pricesnapshots_or_dealcandidates():
    """Broad alternatives should not create PriceSnapshots or DealCandidates by default."""
    risky_raw = {
        "success": True,
        "flights": [
            {"price": 120, "currency": "USD", "provider": "", "cheapest_source": "Skiplagged"},
        ],
    }

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        if "--return" in command:
            completed.stdout = json.dumps({"success": True, "flights": []})
        else:
            completed.stdout = json.dumps(risky_raw)
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    # Normal offers should be empty (round-trip returned zero flights)
    assert result["normalized_result"]["offers"] == []
    # Broad alternatives exist but are not in normal offers
    assert len(result["normalized_result"]["broad_alternatives"]) > 0


def test_success_true_count_zero_is_completed_no_offers():
    """success=true with zero flights should be completed/no-offers, not error."""
    raw = {"success": True, "flights": []}

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        completed.stdout = json.dumps(raw)
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
            )

    assert result["status"] == "completed"
    assert result["error_message"] is None
    assert result["normalized_result"]["offers"] == []


def test_one_way_fallback_data_preserved_when_round_trip_has_zero_flights():
    """When round-trip returns zero flights, one-way fallback data should be visible in diagnostics."""
    empty_raw = {"success": True, "flights": []}
    useful_raw = {
        "success": True,
        "flights": [
            {"price": 150, "currency": "USD", "provider": "Delta"},
        ],
    }

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        if "--return" in command:
            completed.stdout = json.dumps(empty_raw)
        else:
            completed.stdout = json.dumps(useful_raw)
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    # Round-trip has zero offers
    assert result["normalized_result"]["offers"] == []
    # But broad alternatives have data
    broad = result["normalized_result"]["broad_alternatives"]
    total_broad_norm = sum(a["normalized_count"] for a in broad)
    assert total_broad_norm > 0


def test_command_stderr_warnings_stored_per_command():
    """Each command's stderr warnings should be stored separately in diagnostics."""
    raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "Delta"}]}

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        completed.stdout = json.dumps(raw)
        if "--return" in command:
            completed.stderr = "WARNING: round-trip slow"
        else:
            completed.stderr = "WARNING: one-way limited results"
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    # Round-trip stderr_warnings should be present
    assert len(result["normalized_result"]["stderr_warnings"]) > 0


def test_raw_airport_code_quote_cleanup_applies_inside_raw_offer_reference():
    """Embedded quotes in raw airport codes/names should be cleaned inside raw_offer_reference."""
    raw = {
        "success": True,
        "flights": [
            {
                "price": 200,
                "currency": "USD",
                "provider": "Delta",
                "legs": [
                    {
                        "departure_airport": {"code": "'PIT'", "name": "'Pittsburgh Intl'"},
                        "arrival_airport": {"code": "'ORD'", "name": "'Chicago O'Hare'"},
                    },
                ],
            },
        ],
    }

    with patch("app.adapters.trvl_adapter._run_trvl") as mock_run:
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            completed = MagicMock()
            completed.stdout = json.dumps(raw)
            completed.stderr = ""
            completed.returncode = 0
            completed.args = ["trvl", "flights"]
            completed.elapsed_seconds = 1.5
            mock_run.return_value = completed

            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "ORD", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
            )

    # Cleaned airport codes in normalized offer (origin/destination come from resolved query)
    assert result["normalized_result"]["offers"][0]["origin"] == "PIT"
    assert result["normalized_result"]["offers"][0]["destination"] == "ORD"


def test_hotel_normalization_not_broken_by_flight_risk_filtering():
    """Hotel normalization should continue to work correctly."""
    raw = {
        "success": True,
        "hotels": [
            {"name": "Test Hotel", "price_per_night": 100, "currency": "USD"},
        ],
    }

    with patch("app.adapters.trvl_adapter._run_trvl") as mock_run:
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            completed = MagicMock()
            completed.stdout = json.dumps(raw)
            completed.stderr = ""
            completed.returncode = 0
            completed.args = ["trvl", "hotels"]
            completed.elapsed_seconds = 1.5
            mock_run.return_value = completed

            result = trvl_adapter.search_trvl_hotels(
                {"destination": "PIT", "start_date": "2026-07-01", "end_date": "2026-07-05"},
                enabled=True,
                binary_path="/fake/trvl",
            )

    assert result["status"] == "completed"
    assert result["normalized_result"]["hotels"][0]["hotel_name"] == "Test Hotel"


def test_broad_discovery_runs_exactly_three_commands():
    """Broad discovery should run exactly 3 commands: round-trip, outbound one-way, return one-way."""
    raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "Delta"}]}

    with patch("app.adapters.trvl_adapter._run_trvl") as mock_run:
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            completed = MagicMock()
            completed.stdout = json.dumps(raw)
            completed.stderr = ""
            completed.returncode = 0
            completed.args = ["trvl", "flights"]
            completed.elapsed_seconds = 1.5
            mock_run.return_value = completed

            trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    assert mock_run.call_count == 3


def test_broad_summary_includes_diagnostics():
    """broad_summary should include useful diagnostic information."""
    raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "Delta"}]}

    with patch("app.adapters.trvl_adapter._run_trvl") as mock_run:
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            completed = MagicMock()
            completed.stdout = json.dumps(raw)
            completed.stderr = ""
            completed.returncode = 0
            completed.args = ["trvl", "flights"]
            completed.elapsed_seconds = 1.5
            mock_run.return_value = completed

            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    assert "broad_summary" in result["normalized_result"]
    summary = result["normalized_result"]["broad_summary"]
    assert summary["enabled"] is True
    assert summary["one_way_searches_run"] == 2
    assert "search_types" in summary


def test_broad_alternatives_have_is_risky_flag():
    """Broad alternatives should have is_risky flag set correctly."""
    safe_raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "Delta"}]}
    risky_raw = {
        "success": True,
        "flights": [
            {"price": 120, "currency": "USD", "self_connect": True},
        ],
    }

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        if "--return" in command:
            completed.stdout = json.dumps(safe_raw)
        else:
            completed.stdout = json.dumps(risky_raw)
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    broad = result["normalized_result"]["broad_alternatives"]
    for alt in broad:
        for offer in alt.get("alternatives", []):
            assert "is_risky" in offer


def test_risky_round_trip_offers_become_broad_alternatives_when_allowed():
    """Risky round-trip offers should appear in broad_alternatives when TRVL_BROAD_ALLOW_RISKY_ALTERNATIVES=true."""
    risky_raw = {
        "success": True,
        "flights": [
            {"price": 120, "currency": "USD", "provider": "", "cheapest_source": "Skiplagged"},
            {"price": 130, "currency": "USD", "provider": "HiddenCityAir"},
            {"price": 200, "currency": "USD", "provider": "Delta"},  # safe offer too
        ],
    }

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        completed.stdout = json.dumps(risky_raw)
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
                broad_allow_risky_alternatives=True,
            )

    # Normal offers should only contain the safe offer
    assert result["status"] == "completed"
    normal_offers = result["normalized_result"]["offers"]
    assert len(normal_offers) == 1
    assert normal_offers[0]["provider"] == "Delta"

    # Broad alternatives should include risky offers from round-trip
    broad = result["normalized_result"]["broad_alternatives"]
    total_broad_norm = sum(a["normalized_count"] for a in broad)
    assert total_broad_norm > 0

    # Find the risky_round_trip entry
    risky_entries = [a for a in broad if a.get("search_type") == "round_trip"]
    assert len(risky_entries) > 0
    risky_offers = risky_entries[0].get("alternatives", [])
    assert len(risky_offers) >= 1

    # Verify metadata on risky offers
    for offer in risky_offers:
        assert offer.get("eligibility_for_best_deal") is False
        assert "offer_category" in offer
        assert "broad_reason" in offer


def test_risky_round_trip_offers_do_not_become_normal_offers():
    """Risky round-trip offers must NOT appear in normal offers even when broad mode is enabled."""
    all_risky_raw = {
        "success": True,
        "flights": [
            {"price": 120, "currency": "USD", "provider": "", "cheapest_source": "Skiplagged"},
            {"price": 130, "currency": "USD", "self_connect": True},
        ],
    }

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        completed.stdout = json.dumps(all_risky_raw)
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
                broad_allow_risky_alternatives=True,
            )

    # Normal offers should be empty (all risky)
    assert result["status"] == "completed"
    assert result["normalized_result"]["offers"] == []

    # But broad alternatives should have the risky ones
    broad = result["normalized_result"]["broad_alternatives"]
    total_broad_norm = sum(a["normalized_count"] for a in broad)
    assert total_broad_norm > 0


def test_broad_alternatives_do_not_create_pricesnapshots_or_dealcandidates():
    """Broad alternatives must not create PriceSnapshots or DealCandidates."""
    risky_raw = {
        "success": True,
        "flights": [
            {"price": 120, "currency": "USD", "provider": "", "cheapest_source": "Skiplagged"},
        ],
    }

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        if "--return" in command:
            completed.stdout = json.dumps({"success": True, "flights": []})
        else:
            completed.stdout = json.dumps(risky_raw)
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    # Normal offers should be empty (round-trip returned zero flights)
    assert result["normalized_result"]["offers"] == []
    # Broad alternatives exist but are not in normal offers
    assert len(result["normalized_result"]["broad_alternatives"]) > 0


def test_primary_command_failure_preserves_command_diagnostics():
    """When round-trip command fails but one-way commands succeed, diagnostics should be preserved."""
    empty_raw = {"success": True, "flights": []}

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.elapsed_seconds = 1.5
        if "--return" in command:
            # Round-trip fails with exit code 1 but has valid JSON
            completed.returncode = 1
            completed.stdout = json.dumps(empty_raw)
            completed.stderr = "WARNING: round-trip timeout"
        else:
            # One-way commands succeed
            completed.returncode = 0
            completed.stdout = json.dumps({"success": True, "flights": [{"price": 150, "currency": "USD", "provider": "Delta"}]})
            completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    # Should be completed (not error) because RT returned valid JSON via _success()
    assert result["status"] == "completed"
    assert result["error_message"] is None

    # Command diagnostics should include exit_code and elapsed_seconds for RT
    rt_cmd = result["normalized_result"]["command"]
    assert isinstance(rt_cmd, dict)


def test_one_way_command_success_after_round_trip_failure_creates_broad_alternatives():
    """When round-trip fails but one-way commands succeed, broad_alternatives should contain the one-way results."""
    def side_effect(command, timeout):
        completed = MagicMock()
        completed.elapsed_seconds = 1.5
        if "--return" in command:
            # Round-trip fails completely (no valid JSON)
            completed.returncode = 1
            completed.stdout = ""
            completed.stderr = "ERROR: trvl flights exited with code 1"
        else:
            # One-way commands succeed
            completed.returncode = 0
            completed.stdout = json.dumps({"success": True, "flights": [{"price": 150, "currency": "USD", "provider": "Delta"}]})
            completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    # Should be error because RT has no valid JSON and exit code != 0
    assert result["status"] == "error"


def test_all_command_failures_preserve_bounded_diagnostics():
    """When all trvl commands fail, diagnostics should explain each failure."""
    def side_effect(command, timeout):
        completed = MagicMock()
        completed.elapsed_seconds = 2.0
        completed.returncode = 1
        completed.stdout = ""
        completed.stderr = "ERROR: connection refused"
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    # All commands failed: should be error
    assert result["status"] == "error"


def test_success_true_count_zero_is_completed_no_offers():
    """success=true with zero flights should be completed/no-offers, not error."""
    raw = {"success": True, "flights": []}

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        completed.stdout = json.dumps(raw)
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
            )

    assert result["status"] == "completed"
    assert result["error_message"] is None
    assert result["normalized_result"]["offers"] == []


def test_broad_alternatives_have_offer_category_and_broad_reason():
    """Broad alternatives should have offer_category and broad_reason metadata."""
    safe_raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "Delta"}]}
    risky_raw = {
        "success": True,
        "flights": [
            {"price": 120, "currency": "USD", "self_connect": True},
            {"price": 130, "currency": "USD", "provider": "", "cheapest_source": "Skiplagged"},
        ],
    }

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        if "--return" in command:
            completed.stdout = json.dumps(safe_raw)
        else:
            completed.stdout = json.dumps(risky_raw)
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    broad = result["normalized_result"]["broad_alternatives"]
    for alt in broad:
        for offer in alt.get("alternatives", []):
            assert "offer_category" in offer, f"Missing offer_category in {alt['search_type']}"
            assert "broad_reason" in offer, f"Missing broad_reason in {alt['search_type']}"
            assert "eligibility_for_best_deal" in offer
            assert offer["eligibility_for_best_deal"] is False


def test_command_diagnostics_include_stdout_json_success_and_count():
    """Command diagnostics should include stdout_json_success and stdout_json_count."""
    raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "Delta"}]}

    with patch("app.adapters.trvl_adapter._run_trvl") as mock_run:
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            completed = MagicMock()
            completed.stdout = json.dumps(raw)
            completed.stderr = ""
            completed.returncode = 0
            completed.args = ["trvl", "flights"]
            completed.elapsed_seconds = 1.5
            mock_run.return_value = completed

            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
            )

    # RT command metadata should include stdout_json_success and stdout_json_count
    cmd = result["normalized_result"]["command"]
    assert isinstance(cmd, dict)


def test_broad_discovery_disabled_keeps_existing_behavior():
    """When broad discovery is disabled, search_trvl_flights behaves normally."""
    raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "TestAir"}]}

    with patch("app.adapters.trvl_adapter._run_trvl") as mock_run:
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            completed = MagicMock()
            completed.stdout = json.dumps(raw)
            completed.stderr = ""
            completed.returncode = 0
            completed.args = ["trvl", "flights"]
            completed.elapsed_seconds = 1.5
            mock_run.return_value = completed

            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=False,
            )

    assert result["status"] == "completed"
    assert result["normalized_result"]["offers"][0]["provider"] == "TestAir"
    # No broad_alternatives key when disabled
    assert "broad_alternatives" not in result["normalized_result"]


def test_broad_discovery_enabled_runs_round_trip_plus_one_way():
    """Broad discovery enabled should run round-trip + outbound one-way + return one-way."""
    raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "TestAir"}]}

    with patch("app.adapters.trvl_adapter._run_trvl") as mock_run:
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            completed = MagicMock()
            completed.stdout = json.dumps(raw)
            completed.stderr = ""
            completed.returncode = 0
            completed.args = ["trvl", "flights"]
            completed.elapsed_seconds = 1.5
            mock_run.return_value = completed

            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    # _run_trvl should be called 3 times: round-trip + outbound + return
    assert mock_run.call_count == 3

    calls_argv = [c[0][0] for c in mock_run.call_args_list]
    assert "flights" in calls_argv[0]
    assert "flights" in calls_argv[1]
    assert "flights" in calls_argv[2]

    # Broad alternatives should be present
    assert "broad_alternatives" in result["normalized_result"]
    broad = result["normalized_result"]["broad_alternatives"]
    search_types = [a["search_type"] for a in broad]
    assert "outbound_one_way" in search_types
    assert "return_one_way" in search_types


def test_safe_round_trip_offers_still_create_normal_offers():
    """Safe round-trip offers should still appear under normalized_result['offers']."""
    raw = {"success": True, "flights": [
        {"price": 200, "currency": "USD", "provider": "Delta"},
        {"price": 180, "currency": "USD", "provider": "United"},
    ]}

    with patch("app.adapters.trvl_adapter._run_trvl") as mock_run:
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            completed = MagicMock()
            completed.stdout = json.dumps(raw)
            completed.stderr = ""
            completed.returncode = 0
            completed.args = ["trvl", "flights"]
            completed.elapsed_seconds = 1.5
            mock_run.return_value = completed

            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    assert result["normalized_result"]["offers"][0]["total_price"] == 180
    assert result["normalized_result"]["offers"][1]["total_price"] == 200


def test_risky_one_way_alternatives_stored_under_broad_not_normal_offers():
    """Risky one-way alternatives should be in broad_alternatives, not normal offers."""
    safe_raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "Delta"}]}
    risky_raw = {
        "success": True,
        "flights": [
            {"price": 120, "currency": "USD", "provider": "", "cheapest_source": "Skiplagged"},
            {"price": 130, "currency": "USD", "provider": "HiddenCityAir"},
        ],
    }

    call_count = [0]

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        if "--return" in command:
            # Round-trip returns safe offer
            completed.stdout = json.dumps(safe_raw)
        else:
            # One-way searches return risky offers
            call_count[0] += 1
            completed.stdout = json.dumps(risky_raw)
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    # Normal offers should only contain the safe round-trip offer
    assert len(result["normalized_result"]["offers"]) == 1
    assert result["normalized_result"]["offers"][0]["provider"] == "Delta"

    # Broad alternatives should contain the risky ones
    broad = result["normalized_result"]["broad_alternatives"]
    total_broad = sum(a["normalized_count"] for a in broad)
    assert total_broad > 0


def test_one_way_fallback_data_preserved_when_round_trip_has_zero_flights():
    """When round-trip returns zero flights, one-way fallback data should be visible in diagnostics."""
    empty_raw = {"success": True, "flights": []}
    useful_raw = {
        "success": True,
        "flights": [
            {"price": 150, "currency": "USD", "provider": "Delta"},
        ],
    }

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        if "--return" in command:
            completed.stdout = json.dumps(empty_raw)
        else:
            completed.stdout = json.dumps(useful_raw)
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    # Round-trip has zero offers
    assert result["normalized_result"]["offers"] == []
    # But broad alternatives have data
    broad = result["normalized_result"]["broad_alternatives"]
    total_broad_norm = sum(a["normalized_count"] for a in broad)
    assert total_broad_norm > 0


def test_command_stderr_warnings_stored_per_command():
    """Each command's stderr warnings should be stored separately in diagnostics."""
    raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "Delta"}]}

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        completed.stdout = json.dumps(raw)
        if "--return" in command:
            completed.stderr = "WARNING: round-trip slow"
        else:
            completed.stderr = "WARNING: one-way limited results"
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    # Round-trip stderr_warnings should be present
    assert len(result["normalized_result"]["stderr_warnings"]) > 0


def test_raw_airport_code_quote_cleanup_applies_inside_raw_offer_reference():
    """Embedded quotes in raw airport codes/names should be cleaned inside raw_offer_reference."""
    raw = {
        "success": True,
        "flights": [
            {
                "price": 200,
                "currency": "USD",
                "provider": "Delta",
                "legs": [
                    {
                        "departure_airport": {"code": "'PIT'", "name": "'Pittsburgh Intl'"},
                        "arrival_airport": {"code": "'ORD'", "name": "'Chicago O'Hare'"},
                    },
                ],
            },
        ],
    }

    with patch("app.adapters.trvl_adapter._run_trvl") as mock_run:
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            completed = MagicMock()
            completed.stdout = json.dumps(raw)
            completed.stderr = ""
            completed.returncode = 0
            completed.args = ["trvl", "flights"]
            completed.elapsed_seconds = 1.5
            mock_run.return_value = completed

            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "ORD", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
            )

    # Cleaned airport codes in normalized offer (origin/destination come from resolved query)
    assert result["normalized_result"]["offers"][0]["origin"] == "PIT"
    assert result["normalized_result"]["offers"][0]["destination"] == "ORD"


def test_hotel_normalization_not_broken_by_flight_risk_filtering():
    """Hotel normalization should continue to work correctly."""
    raw = {
        "success": True,
        "hotels": [
            {"name": "Test Hotel", "price_per_night": 100, "currency": "USD"},
        ],
    }

    with patch("app.adapters.trvl_adapter._run_trvl") as mock_run:
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            completed = MagicMock()
            completed.stdout = json.dumps(raw)
            completed.stderr = ""
            completed.returncode = 0
            completed.args = ["trvl", "hotels"]
            completed.elapsed_seconds = 1.5
            mock_run.return_value = completed

            result = trvl_adapter.search_trvl_hotels(
                {"destination": "PIT", "start_date": "2026-07-01", "end_date": "2026-07-05"},
                enabled=True,
                binary_path="/fake/trvl",
            )

    assert result["status"] == "completed"
    assert result["normalized_result"]["hotels"][0]["hotel_name"] == "Test Hotel"


def test_broad_discovery_runs_exactly_three_commands():
    """Broad discovery should run exactly 3 commands: round-trip, outbound one-way, return one-way."""
    raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "Delta"}]}

    with patch("app.adapters.trvl_adapter._run_trvl") as mock_run:
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            completed = MagicMock()
            completed.stdout = json.dumps(raw)
            completed.stderr = ""
            completed.returncode = 0
            completed.args = ["trvl", "flights"]
            completed.elapsed_seconds = 1.5
            mock_run.return_value = completed

            trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    assert mock_run.call_count == 3


def test_broad_summary_includes_diagnostics():
    """broad_summary should include useful diagnostic information."""
    raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "Delta"}]}

    with patch("app.adapters.trvl_adapter._run_trvl") as mock_run:
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            completed = MagicMock()
            completed.stdout = json.dumps(raw)
            completed.stderr = ""
            completed.returncode = 0
            completed.args = ["trvl", "flights"]
            completed.elapsed_seconds = 1.5
            mock_run.return_value = completed

            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    assert "broad_summary" in result["normalized_result"]
    summary = result["normalized_result"]["broad_summary"]
    assert summary["enabled"] is True
    assert summary["one_way_searches_run"] == 2
    assert "search_types" in summary


def test_broad_alternatives_have_is_risky_flag():
    """Broad alternatives should have is_risky flag set correctly."""
    safe_raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "Delta"}]}
    risky_raw = {
        "success": True,
        "flights": [
            {"price": 120, "currency": "USD", "self_connect": True},
        ],
    }

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        if "--return" in command:
            completed.stdout = json.dumps(safe_raw)
        else:
            completed.stdout = json.dumps(risky_raw)
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    broad = result["normalized_result"]["broad_alternatives"]
    for alt in broad:
        for offer in alt.get("alternatives", []):
            assert "is_risky" in offer


def test_broad_summary_exists_when_broad_enabled_even_with_zero_alternatives():
    """broad_summary must always be present when broad discovery is enabled, even if no alternatives found."""
    empty_raw = {"success": True, "flights": []}

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        completed.stdout = json.dumps(empty_raw)
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    assert result["status"] == "completed"
    nr = result["normalized_result"]
    # broad_summary must always be present when broad discovery is enabled with return date
    assert "broad_summary" in nr
    summary = nr["broad_summary"]
    assert summary["enabled"] is True
    assert summary["one_way_searches_run"] == 2
    assert summary["total_raw_alternatives"] == 0
    assert summary["total_normalized_alternatives"] == 0


def test_command_diagnostics_present_when_broad_enabled():
    """command_results must be present when broad discovery is enabled."""
    raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "Delta"}]}

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        completed.stdout = json.dumps(raw)
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    nr = result["normalized_result"]
    assert "command_results" in nr
    cmd_list = nr["command_results"]
    labels = [c["label"] for c in cmd_list]
    assert "outbound_one_way" in labels
    assert "return_one_way" in labels


def test_round_trip_outbound_return_command_labels_present():
    """When broad discovery enabled, command diagnostics must include round_trip + outbound_one_way + return_one_way."""
    raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "Delta"}]}

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        completed.stdout = json.dumps(raw)
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    nr = result["normalized_result"]
    # RT command metadata in normalized['command']
    rt_cmd = nr.get("command", {})
    assert "exit_code" in rt_cmd
    assert "elapsed_seconds" in rt_cmd
    assert "stdout_json_success" in rt_cmd
    assert "stdout_json_count" in rt_cmd

    # One-way command diagnostics in command_results
    cmd_list = nr.get("command_results", [])
    labels = [c["label"] for c in cmd_list]
    assert "outbound_one_way" in labels
    assert "return_one_way" in labels


def test_recursive_raw_offer_reference_airport_code_cleanup_in_normal_offers():
    """Safe offers' raw_offer_reference must have airport codes/names recursively cleaned."""
    raw = {
        "success": True,
        "flights": [
            {
                "price": 200,
                "currency": "USD",
                "provider": "Delta",
                "legs": [
                    {
                        "departure_airport": {"code": "'PIT'", "name": "'Pittsburgh International Airport'"},
                        "arrival_airport": {"code": "\"MSP\"", "name": "\"Minneapolis-St. Paul Intl\""},
                    },
                ],
            },
        ],
    }

    with patch("app.adapters.trvl_adapter._run_trvl") as mock_run:
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            completed = MagicMock()
            completed.stdout = json.dumps(raw)
            completed.stderr = ""
            completed.returncode = 0
            completed.args = ["trvl", "flights"]
            completed.elapsed_seconds = 1.5
            mock_run.return_value = completed

            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MSP", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
            )

    offer = result["normalized_result"]["offers"][0]
    raw_ref = offer.get("raw_offer_reference", {})
    legs = raw_ref.get("legs", [])
    assert len(legs) > 0
    dep = legs[0].get("departure_airport", {})
    arr = legs[0].get("arrival_airport", {})
    # Cleaned codes (no embedded quotes)
    assert dep.get("code") == "PIT"
    assert arr.get("code") == "MSP"
    # Cleaned names (no embedded quotes)
    assert "'Pittsburgh International Airport'" not in str(dep.get("name", ""))
    assert "\"Minneapolis-St. Paul Intl\"" not in str(arr.get("name", ""))


def test_recursive_cleanup_applies_in_broad_alternatives():
    """Broad alternatives raw_offer_reference must also have airport codes/names cleaned."""
    risky_raw = {
        "success": True,
        "flights": [
            {
                "price": 120,
                "currency": "USD",
                "provider": "",
                "cheapest_source": "Skiplagged",
                "legs": [
                    {
                        "departure_airport": {"code": "'PIT'", "name": "'Pittsburgh Intl'"},
                        "arrival_airport": {"code": "\"MSP\"", "name": "\"Minneapolis-St. Paul\""},
                    },
                ],
            },
        ],
    }

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        if "--return" in command:
            completed.stdout = json.dumps({"success": True, "flights": []})
        else:
            completed.stdout = json.dumps(risky_raw)
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MSP", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    # Check broad alternatives have cleaned raw_offer_reference
    broad = result["normalized_result"]["broad_alternatives"]
    for alt in broad:
        for offer in alt.get("alternatives", []):
            raw_ref = offer.get("raw_offer_reference", {})
            legs = raw_ref.get("legs", [])
            if legs:
                dep = legs[0].get("departure_airport", {})
                assert dep.get("code") == "PIT"


def test_broad_discovery_disabled_no_broad_summary_or_command_results():
    """When broad discovery is disabled, no broad_summary or command_results should be present."""
    raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "Delta"}]}

    with patch("app.adapters.trvl_adapter._run_trvl") as mock_run:
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            completed = MagicMock()
            completed.stdout = json.dumps(raw)
            completed.stderr = ""
            completed.returncode = 0
            completed.args = ["trvl", "flights"]
            completed.elapsed_seconds = 1.5
            mock_run.return_value = completed

            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=False,
            )

    nr = result["normalized_result"]
    assert "broad_summary" not in nr
    assert "command_results" not in nr


def test_broad_command_diagnostics_include_all_required_fields():
    """Each command diagnostic must include label, command, exit_code, elapsed_seconds, stdout_json_success, stdout_json_count, stderr_preview."""
    raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "Delta"}]}

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        completed.stdout = json.dumps(raw)
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    nr = result["normalized_result"]
    cmd_list = nr.get("command_results", [])
    required_fields = {"label", "command", "exit_code", "elapsed_seconds", "stdout_json_success", "stdout_json_count", "stderr_preview"}
    for cmd in cmd_list:
        assert required_fields.issubset(set(cmd.keys())), f"Missing fields in {cmd.get('label')}: {required_fields - set(cmd.keys())}"


def test_broad_summary_includes_search_types_when_alternatives_found():
    """broad_summary.search_types should list which search types produced alternatives."""
    safe_raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "Delta"}]}

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        if "--return" in command:
            completed.stdout = json.dumps(safe_raw)
        else:
            completed.stdout = json.dumps({"success": True, "flights": []})
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    summary = result["normalized_result"]["broad_summary"]
    assert isinstance(summary["search_types"], list)


def test_broad_command_results_bounded_to_last_10():
    """command_results should be bounded to last 10 entries."""
    raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "Delta"}]}

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        completed.stdout = json.dumps(raw)
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    nr = result["normalized_result"]
    cmd_list = nr.get("command_results", [])
    assert len(cmd_list) <= 10


def test_broad_summary_has_zero_counts_when_all_offers_filtered():
    """When all offers are filtered out (e.g., risky), broad_summary should show zero normalized counts."""
    risky_raw = {
        "success": True,
        "flights": [
            {"price": 120, "currency": "USD", "provider": "", "cheapest_source": "Skiplagged"},
        ],
    }

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        if "--return" in command:
            completed.stdout = json.dumps({"success": True, "flights": []})
        else:
            completed.stdout = json.dumps(risky_raw)
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    summary = result["normalized_result"]["broad_summary"]
    assert summary["enabled"] is True
    # Raw alternatives exist but normalized count may be 0 if all filtered
    assert "total_raw_alternatives" in summary
    assert "total_normalized_alternatives" in summary


def test_normal_offers_still_safe_when_broad_enabled():
    """Normal offers must remain safe and eligible for PriceSnapshots even when broad discovery is enabled."""
    mixed_raw = {
        "success": True,
        "flights": [
            {"price": 200, "currency": "USD", "provider": "Delta"},  # safe
            {"price": 120, "currency": "USD", "self_connect": True},  # risky
            {"price": 130, "currency": "USD", "provider": "", "cheapest_source": "Skiplagged"},  # risky
        ],
    }

    with patch("app.adapters.trvl_adapter._run_trvl") as mock_run:
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            completed = MagicMock()
            completed.stdout = json.dumps(mixed_raw)
            completed.stderr = ""
            completed.returncode = 0
            completed.args = ["trvl", "flights"]
            completed.elapsed_seconds = 1.5
            mock_run.return_value = completed

            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    # Only safe offers in normal
    assert len(result["normalized_result"]["offers"]) == 1
    assert result["normalized_result"]["offers"][0]["provider"] == "Delta"


def test_broad_alternatives_do_not_affect_best_deal():
    """Broad alternatives must not create default best_deal or affect PriceSnapshots."""
    risky_raw = {
        "success": True,
        "flights": [
            {"price": 120, "currency": "USD", "provider": "", "cheapest_source": "Skiplagged"},
        ],
    }

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        if "--return" in command:
            completed.stdout = json.dumps({"success": True, "flights": []})
        else:
            completed.stdout = json.dumps(risky_raw)
        completed.stderr = ""
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    # No normal offers (all risky)
    assert result["normalized_result"]["offers"] == []
    # Broad alternatives exist but are not in normal offers
    assert len(result["normalized_result"]["broad_alternatives"]) > 0


def test_broad_summary_empty_search_types_when_no_return_date():
    """When no return date, broad discovery should not run and broad_summary absent."""
    raw = {"success": True, "flights": [{"price": 200, "currency": "USD", "provider": "Delta"}]}

    with patch("app.adapters.trvl_adapter._run_trvl") as mock_run:
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            completed = MagicMock()
            completed.stdout = json.dumps(raw)
            completed.stderr = ""
            completed.returncode = 0
            completed.args = ["trvl", "flights"]
            completed.elapsed_seconds = 1.5
            mock_run.return_value = completed

            # No end_date means no return date -> has_return=False
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    nr = result["normalized_result"]
    # Without return date, broad discovery doesn't run (no round-trip context)
    assert "broad_summary" not in nr


def test_command_diagnostics_stderr_preview_bounded():
    """stderr_preview should be bounded to prevent unbounded strings."""
    long_stderr = "E" * 2000

    def side_effect(command, timeout):
        completed = MagicMock()
        completed.returncode = 0
        completed.elapsed_seconds = 1.5
        completed.stdout = json.dumps({"success": True, "flights": []})
        completed.stderr = long_stderr
        completed.args = command
        return completed

    with patch("app.adapters.trvl_adapter._run_trvl", side_effect=side_effect):
        with patch("app.adapters.trvl_adapter.resolve_trvl_binary", return_value="/fake/trvl"):
            result = trvl_adapter.search_trvl_flights(
                {"origin": "PIT", "destination": "MOT", "start_date": "2026-07-01", "end_date": "2026-07-15"},
                enabled=True,
                binary_path="/fake/trvl",
                broad_discovery_enabled=True,
            )

    nr = result["normalized_result"]
    cmd_list = nr.get("command_results", [])
    for cmd in cmd_list:
        preview = cmd.get("stderr_preview", "")
        # _bounded_string limits content to limit chars then appends truncation marker.
        # The original content portion must be bounded, not total string length.
        assert len(preview) < 600, f"stderr_preview too long: {len(preview)}"
