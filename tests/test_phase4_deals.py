import json

import pytest
from sqlmodel import Session, SQLModel, select

from app.db.models import DealCandidate, PriceSnapshot, SourceResult
from app.db.session import get_engine, init_db
from app.services.deal_scoring import score_candidate
from app.services.manifest_io import vacation_from_manifest
from app.services.package_builder import build_deal_candidates
from app.services.quote_normalizer import snapshots_from_source_result
from app.services.search_planner import deterministic_json
from app.services.search_runner import run_search_once
from app.web.routes import component_summary_for_deal, deal_detail, price_history_page, vacation_detail


@pytest.fixture()
def session(tmp_path, monkeypatch):
    db_path = tmp_path / "vacation_deals.sqlite3"
    monkeypatch.setenv("VACATION_DEAL_DB_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("SEARXNG_BASE_URL", "")
    monkeypatch.setenv("AMADEUS_ENABLED", "false")
    monkeypatch.setenv("GOOGLE_PLACES_ENABLED", "false")
    SQLModel.metadata.drop_all(get_engine())
    init_db()
    with Session(get_engine()) as db_session:
        yield db_session


def manifest(**overrides):
    data = {
        "title": "Phase 4 trip",
        "status": "active",
        "number_of_travelers": 2,
        "travelers": [],
        "origin": "JFK",
        "destination": "Lisbon",
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


def source_result(session, search_run_id, result_type, normalized, *, status="completed", source_name="unit"):
    result = SourceResult(
        search_run_id=search_run_id,
        source_name=source_name,
        result_type=result_type,
        status=status,
        query_json="{}",
        normalized_result_json=deterministic_json(normalized),
        raw_result_json="{}",
    )
    session.add(result)
    session.commit()
    session.refresh(result)
    return result


def test_quote_normalizer_extracts_priced_flight_result(session):
    vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)
    search_run = run_search_once(vacation.id, "manual", session=session, use_mock=False)
    result = source_result(
        session,
        search_run.id,
        "flight",
        {
            "result_type": "flight",
            "offers": [
                {
                    "label": "Flight A",
                    "total_price": "321.45",
                    "currency": "USD",
                    "airline_carrier_codes": ["DL"],
                }
            ],
        },
    )

    snapshots = snapshots_from_source_result(vacation, result)

    assert len(snapshots) == 1
    assert snapshots[0].quote_type == "flight"
    assert snapshots[0].total_price == 321.45
    assert snapshots[0].label == "Flight A"
    assert snapshots[0].provider == "DL"


def test_quote_normalizer_extracts_priced_hotel_result(session):
    vacation = create_vacation(session, hotel_needed=True, airfare_needed=False)
    search_run = run_search_once(vacation.id, "manual", session=session, use_mock=False)
    result = source_result(
        session,
        search_run.id,
        "hotel",
        {"result_type": "hotel", "hotels": [{"hotel_name": "Hotel A", "total_price": "900", "currency": "USD"}]},
    )

    snapshots = snapshots_from_source_result(vacation, result)

    assert len(snapshots) == 1
    assert snapshots[0].quote_type == "hotel"
    assert snapshots[0].total_price == 900
    assert snapshots[0].label == "Hotel A"
    assert snapshots[0].provider == "Hotel A"


def test_quote_normalizer_extracts_priced_rental_car_provider(session):
    vacation = create_vacation(session, hotel_needed=False, airfare_needed=False, rental_car_needed=True)
    search_run = run_search_once(vacation.id, "manual", session=session, use_mock=False)
    result = source_result(
        session,
        search_run.id,
        "rental_car",
        {
            "result_type": "rental_car",
            "cars": [{"label": "Compact car", "rental_company": "Enterprise", "total_price": "210", "currency": "USD"}],
        },
    )

    snapshots = snapshots_from_source_result(vacation, result)

    assert len(snapshots) == 1
    assert snapshots[0].quote_type == "rental_car"
    assert snapshots[0].provider == "Enterprise"


def test_unpriced_skipped_error_results_do_not_crash_scoring(session):
    vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)
    search_run = run_search_once(vacation.id, "manual", session=session, use_mock=False)
    skipped = source_result(
        session,
        search_run.id,
        "flight",
        {"result_type": "flight", "reason": "disabled"},
        status="skipped",
    )
    error = source_result(
        session,
        search_run.id,
        "flight",
        {"result_type": "flight"},
        status="error",
    )

    assert snapshots_from_source_result(vacation, skipped) == []
    assert snapshots_from_source_result(vacation, error) == []
    candidate = DealCandidate(
        vacation_id=vacation.id,
        search_run_id=search_run.id,
        candidate_type="flight_only",
        title="No price",
        status="skipped",
        total_price=None,
    )

    score_candidate(candidate, [skipped, error])

    assert candidate.score is None
    assert "no total price" in json.loads(candidate.score_breakdown_json)["reason"].lower()


def test_package_builder_creates_flight_only_candidate_for_airfare_only_vacation(session):
    vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)
    search_run = run_search_once(vacation.id, "manual", session=session, use_mock=False)
    snapshot = PriceSnapshot(
        vacation_id=vacation.id,
        search_run_id=search_run.id,
        quote_type="flight",
        source_name="unit",
        label="Flight A",
        total_price=300,
    )
    session.add(snapshot)
    session.commit()
    session.refresh(snapshot)

    candidates = build_deal_candidates(session, vacation, search_run.id, [snapshot])

    assert len(candidates) == 1
    assert candidates[0].candidate_type == "flight_only"
    assert candidates[0].status == "valid"
    assert candidates[0].total_price == 300


def test_package_builder_creates_package_candidate_when_hotel_and_airfare_available(session):
    vacation = create_vacation(session, hotel_needed=True, airfare_needed=True)
    search_run = run_search_once(vacation.id, "manual", session=session, use_mock=False)
    snapshots = [
        PriceSnapshot(vacation_id=vacation.id, search_run_id=search_run.id, quote_type="flight", source_name="amadeus", provider="United", label="Flight", total_price=400),
        PriceSnapshot(vacation_id=vacation.id, search_run_id=search_run.id, quote_type="hotel", source_name="amadeus", provider="Hampton Inn", label="Hotel", total_price=700),
    ]
    for snapshot in snapshots:
        session.add(snapshot)
    session.commit()
    for snapshot in snapshots:
        session.refresh(snapshot)

    candidates = build_deal_candidates(session, vacation, search_run.id, snapshots)

    assert len(candidates) == 1
    assert candidates[0].candidate_type == "package"
    assert candidates[0].status == "valid"
    assert candidates[0].total_price == 1100
    components = json.loads(candidates[0].normalized_result_json)["component_summary"]
    assert [(component["component_type_label"], component["provider"]) for component in components] == [
        ("Airfare", "United"),
        ("Hotel", "Hampton Inn"),
    ]


def test_scoring_ranks_lower_total_price_better(session):
    cheap = DealCandidate(vacation_id=1, search_run_id=1, candidate_type="flight_only", title="Cheap", status="valid", total_price=200)
    expensive = DealCandidate(vacation_id=1, search_run_id=1, candidate_type="flight_only", title="Expensive", status="valid", total_price=500)

    score_candidate(cheap, [])
    score_candidate(expensive, [])

    assert cheap.score < expensive.score


def test_search_run_creates_phase4_rows_after_mock_run(session):
    vacation = create_vacation(session, hotel_needed=True, airfare_needed=True)

    search_run = run_search_once(vacation.id, "manual", session=session)

    snapshots = session.exec(select(PriceSnapshot).where(PriceSnapshot.search_run_id == search_run.id)).all()
    candidates = session.exec(select(DealCandidate).where(DealCandidate.search_run_id == search_run.id)).all()
    summary = json.loads(session.get(type(search_run), search_run.id).summary_json)
    assert len(snapshots) == 2
    assert len(candidates) == 1
    assert summary["priced_snapshot_count"] == 2
    assert summary["deal_candidate_count"] == 1
    assert summary["best_deal_total_price"] == 1685


def test_vacation_detail_page_displays_best_deal_when_present(session):
    vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)
    run_search_once(vacation.id, "manual", session=session)

    response = vacation_detail(vacation.id, request=None, session=session)

    assert response.status_code == 200
    assert response.context["best_deal"] is not None
    assert response.context["best_deal_components"][0]["provider"] == "mock_travel"
    assert response.context["best_deal_components"][0]["component_type_label"] == "Airfare"


def test_deal_detail_context_exposes_component_provider_labels(session):
    vacation = create_vacation(session, hotel_needed=True, airfare_needed=True)
    run_search_once(vacation.id, "manual", session=session)
    deal = session.exec(select(DealCandidate).where(DealCandidate.vacation_id == vacation.id)).first()

    response = deal_detail(deal.id, request=None, session=session)

    assert response.status_code == 200
    providers = [component["provider"] for component in response.context["components"]]
    assert providers == ["mock_travel", "mock_travel"]
    assert all(component["is_mock"] for component in response.context["components"])
    assert component_summary_for_deal(deal)[0]["source_name"] == "mock_travel"


def test_price_history_endpoint_page_returns_graph_data(session):
    vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)
    run_search_once(vacation.id, "manual", session=session)

    response = price_history_page(vacation.id, request=None, session=session)

    assert response.status_code == 200
    assert response.context["history"]["snapshots"]
    assert response.context["history_points"]
