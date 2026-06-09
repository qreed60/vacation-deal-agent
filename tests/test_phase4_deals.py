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
from app.web.routes import component_summary_for_deal, deal_detail, price_history_page, search_run_detail, templates, vacation_detail


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


def source_result(session, search_run_id, result_type, normalized, *, status="completed", source_name="unit", raw_result=None):
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
    component = json.loads(snapshots[0].normalized_json)
    assert component["provider"] == "DL"
    assert component["link_type"] == "search_reference"
    assert component["link_label"] == "Search reference"
    assert component["search_reference_url"]


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
    component = json.loads(snapshots[0].normalized_json)
    assert component["provider"] == "Hotel A"
    assert component["link_type"] == "search_reference"
    assert component["search_reference_url"]


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
    component = json.loads(snapshots[0].normalized_json)
    assert component["provider"] == "Enterprise"
    assert component["link_type"] == "search_reference"
    assert component["link_label"] == "Search reference"


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


def test_amadeus_carrier_dictionary_reaches_component_provider(session):
    vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)
    search_run = run_search_once(vacation.id, "manual", session=session, use_mock=False)
    raw_offer = {
        "validatingAirlineCodes": ["DL"],
        "itineraries": [
            {
                "segments": [
                    {
                        "carrierCode": "DL",
                        "number": "123",
                        "departure": {"iataCode": "PIT"},
                        "arrival": {"iataCode": "MOT"},
                    }
                ]
            }
        ],
    }
    result = source_result(
        session,
        search_run.id,
        "flight",
        {
            "source_name": "amadeus",
            "result_type": "flight",
            "offers": [
                {
                    "source_name": "amadeus",
                    "result_type": "flight",
                    "airline_carrier_codes": ["DL"],
                    "itinerary_summary": "PIT->MOT",
                    "total_price": "425.00",
                    "currency": "USD",
                    "raw_offer_reference": raw_offer,
                }
            ],
        },
        source_name="amadeus",
        raw_result={"dictionaries": {"carriers": {"DL": "Delta Air Lines"}}},
    )
    snapshots = snapshots_from_source_result(vacation, result)
    candidates = build_deal_candidates(session, vacation, search_run.id, snapshots)

    component = json.loads(candidates[0].normalized_result_json)["component_summary"][0]

    assert component["provider"] == "Delta Air Lines"
    assert component["provider_code"] == "DL"
    assert component["source_name"] == "amadeus"
    assert component["source_name_label"] == "Amadeus"
    assert component["label"] == "DL 123 PIT->MOT"
    assert component["link_type"] == "search_reference"
    assert component["link_label"] == "Search reference"


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
    assert response.context["best_deal_components"][0]["provider"] == "Mock Air"
    assert response.context["best_deal_components"][0]["component_type_label"] == "Airfare"
    assert response.context["best_deal_components"][0]["link_label"] == "Search reference"


def test_existing_vacation_with_no_deal_candidates_renders(session):
    vacation = create_vacation(session)

    response = vacation_detail(vacation.id, request=None, session=session)

    assert response.status_code == 200
    assert response.context["best_deal"] is None
    assert response.context["latest_deals"] == []


def test_vacation_detail_renders_old_minimal_component_without_total_price(session):
    vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)
    deal = DealCandidate(
        vacation_id=vacation.id,
        search_run_id=1,
        candidate_type="flight_only",
        title="Legacy deal",
        status="valid",
        total_price=425,
        currency="USD",
        score=1,
        normalized_result_json=deterministic_json(
            {
                "component_summary": [
                    {
                        "provider": "Legacy Air",
                        "source_name": "amadeus",
                        "source_result_id": 7,
                    }
                ]
            }
        ),
        source_links_json="[]",
    )
    session.add(deal)
    session.commit()
    session.refresh(deal)

    response = vacation_detail(vacation.id, request=None, session=session)

    assert response.status_code == 200
    assert response.context["best_deal_components"][0]["provider"] == "Legacy Air"
    assert response.context["best_deal_components"][0]["total_price"] is None
    assert b"Legacy Air" in response.body


def test_invalid_source_links_json_does_not_crash_vacation_or_deal_detail(session):
    vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)
    deal = DealCandidate(
        vacation_id=vacation.id,
        search_run_id=1,
        candidate_type="flight_only",
        title="Bad source links",
        status="valid",
        total_price=425,
        currency="USD",
        score=1,
        normalized_result_json="{}",
        source_links_json="{not valid json",
    )
    session.add(deal)
    session.commit()
    session.refresh(deal)

    vacation_response = vacation_detail(vacation.id, request=None, session=session)
    deal_response = deal_detail(deal.id, request=None, session=session)

    assert vacation_response.status_code == 200
    assert deal_response.status_code == 200
    assert vacation_response.context["best_deal_components"] == []
    assert deal_response.context["source_links"] == "[]"


def test_invalid_normalized_result_json_does_not_crash_vacation_or_deal_detail(session):
    vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)
    deal = DealCandidate(
        vacation_id=vacation.id,
        search_run_id=1,
        candidate_type="flight_only",
        title="Bad normalized JSON",
        status="valid",
        total_price=425,
        currency="USD",
        score=1,
        normalized_result_json="{not valid json",
        source_links_json=deterministic_json(
            [
                {
                    "component_type": "flight",
                    "provider": "Fallback Air",
                    "source_name": "amadeus",
                    "total_price": 425,
                    "currency": "USD",
                }
            ]
        ),
    )
    session.add(deal)
    session.commit()
    session.refresh(deal)

    vacation_response = vacation_detail(vacation.id, request=None, session=session)
    deal_response = deal_detail(deal.id, request=None, session=session)

    assert vacation_response.status_code == 200
    assert deal_response.status_code == 200
    assert vacation_response.context["best_deal_components"][0]["provider"] == "Fallback Air"
    assert deal_response.context["normalized_result"] == "{}"


def test_mock_run_vacation_page_renders_provider_source_rows(session):
    vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)
    run_search_once(vacation.id, "manual", session=session)

    response = vacation_detail(vacation.id, request=None, session=session)

    assert response.status_code == 200
    assert b"mock_travel" in response.body
    assert b"MOCK" in response.body


def test_deal_detail_context_exposes_component_provider_labels(session):
    vacation = create_vacation(session, hotel_needed=True, airfare_needed=True)
    run_search_once(vacation.id, "manual", session=session)
    deal = session.exec(select(DealCandidate).where(DealCandidate.vacation_id == vacation.id)).first()

    response = deal_detail(deal.id, request=None, session=session)

    assert response.status_code == 200
    providers = [component["provider"] for component in response.context["components"]]
    assert providers == ["Mock Air", "Mock Harbor Hotel"]
    assert all(component["is_mock"] for component in response.context["components"])
    assert component_summary_for_deal(deal)[0]["source_name"] == "mock_travel"


def test_deal_detail_renders_provider_source_cards_without_primary_json_dump(session):
    deal = DealCandidate(
        vacation_id=1,
        search_run_id=1,
        candidate_type="flight_only",
        title="Delta test",
        status="valid",
        total_price=425,
        normalized_result_json=deterministic_json(
            {
                "component_summary": [
                    {
                        "component_type": "flight",
                        "component_type_label": "Airfare",
                        "provider": "Delta Air Lines",
                        "provider_code": "DL",
                        "source_name": "amadeus",
                        "source_name_label": "Amadeus",
                        "source_result_id": 39,
                        "label": "DL 123 PIT->MOT",
                        "total_price": 425,
                        "currency": "USD",
                        "source_url": None,
                        "captured_at": "2026-06-07T12:00:00+00:00",
                    }
                ]
            }
        ),
        source_links_json="[]",
    )
    components = component_summary_for_deal(deal)
    html = templates.get_template("deal_detail.html").render(
        deal=deal,
        vacation=None,
        components=components,
        component_snapshot_ids="[]",
        source_links="[]",
        score_breakdown="{}",
        normalized_result=deal.normalized_result_json,
    )

    assert "component-detail-card" in html
    assert "Delta Air Lines" in html
    assert "Amadeus" in html
    assert "Airfare through" not in html
    assert "Debug JSON" in html
    assert html.index("Delta Air Lines") < html.index("Debug JSON")


def test_mock_component_renders_mock_travel_and_mock_badge(session):
    vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)
    run_search_once(vacation.id, "manual", session=session)
    deal = session.exec(select(DealCandidate).where(DealCandidate.vacation_id == vacation.id)).first()
    components = component_summary_for_deal(deal)
    html = templates.get_template("deal_detail.html").render(
        deal=deal,
        vacation=vacation,
        components=components,
        component_snapshot_ids=deal.component_snapshot_ids_json,
        source_links=deal.source_links_json,
        score_breakdown=deal.score_breakdown_json,
        normalized_result=deal.normalized_result_json,
    )

    assert components[0]["provider"] == "Mock Air"
    assert components[0]["source_name"] == "mock_travel"
    assert components[0]["is_mock"] is True
    assert components[0]["link_label"] == "Search reference"
    assert "Mock Air" in html
    assert "MOCK" in html


def test_missing_source_url_does_not_render_fake_exact_price_link():
    deal = DealCandidate(
        vacation_id=1,
        search_run_id=1,
        candidate_type="flight_only",
        title="No URL",
        status="valid",
        total_price=425,
        normalized_result_json=deterministic_json(
            {
                "component_summary": [
                    {
                        "component_type": "flight",
                        "component_type_label": "Airfare",
                        "provider": "Delta Air Lines",
                        "source_name": "amadeus",
                        "source_name_label": "Amadeus",
                        "label": "DL 123 PIT->MOT",
                        "total_price": 425,
                        "currency": "USD",
                        "source_url": None,
                    }
                ]
            }
        ),
        source_links_json="[]",
    )
    html = templates.get_template("deal_detail.html").render(
        deal=deal,
        vacation=None,
        components=component_summary_for_deal(deal),
        component_snapshot_ids="[]",
        source_links="[]",
        score_breakdown="{}",
        normalized_result=deal.normalized_result_json,
    )

    assert "View source" not in html
    assert "View source price" not in html
    assert "Book at this price" not in html


def test_source_url_uses_exact_source_link_type(session):
    vacation = create_vacation(session, hotel_needed=True, airfare_needed=False)
    search_run = run_search_once(vacation.id, "manual", session=session, use_mock=False)
    result = source_result(
        session,
        search_run.id,
        "hotel",
        {
            "result_type": "hotel",
            "hotels": [
                {
                    "hotel_name": "Exact Hotel",
                    "total_price": "800",
                    "currency": "USD",
                    "source_url": "https://hotel.example/offer/123",
                }
            ],
        },
    )

    snapshot = snapshots_from_source_result(vacation, result)[0]
    component = json.loads(snapshot.normalized_json)

    assert component["source_url"] == "https://hotel.example/offer/123"
    assert component["link_type"] == "exact_source"
    assert component["link_label"] == "View source price"


def test_quote_normalizer_creates_snapshot_from_serpapi_flight(session):
    vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)
    search_run = run_search_once(vacation.id, "manual", session=session, use_mock=False)
    result = source_result(
        session,
        search_run.id,
        "flight",
        {
            "source_name": "serpapi_google_flights",
            "result_type": "flight",
            "offers": [
                {
                    "source_name": "serpapi_google_flights",
                    "result_type": "flight",
                    "provider": "American Airlines",
                    "carrier_code": "AA",
                    "flight_numbers": ["AA 123"],
                    "total_price": 312,
                    "currency": "USD",
                    "search_reference_url": "https://www.google.com/search?q=American+Airlines+PIT+MOT",
                    "link_type": "search_reference",
                    "link_label": "Search reference",
                }
            ],
        },
        source_name="serpapi_google_flights",
    )

    snapshot = snapshots_from_source_result(vacation, result)[0]
    component = json.loads(snapshot.normalized_json)

    assert snapshot.provider == "American Airlines"
    assert snapshot.total_price == 312
    assert component["source_name"] == "serpapi_google_flights"
    assert component["provider_code"] == "AA"
    assert component["link_type"] == "search_reference"
    assert component["link_label"] == "Search reference"


def test_quote_normalizer_creates_snapshot_from_serpapi_hotel(session):
    vacation = create_vacation(session, hotel_needed=True, airfare_needed=False)
    search_run = run_search_once(vacation.id, "manual", session=session, use_mock=False)
    result = source_result(
        session,
        search_run.id,
        "hotel",
        {
            "source_name": "serpapi_google_hotels",
            "result_type": "hotel",
            "hotels": [
                {
                    "source_name": "serpapi_google_hotels",
                    "result_type": "hotel",
                    "hotel_name": "Hampton Inn Minot",
                    "total_price": 620,
                    "currency": "USD",
                    "rating": 4.4,
                    "source_url": "https://hotel.example/source-price",
                    "link_type": "exact_source",
                    "link_label": "View source price",
                }
            ],
        },
        source_name="serpapi_google_hotels",
    )

    snapshot = snapshots_from_source_result(vacation, result)[0]
    component = json.loads(snapshot.normalized_json)

    assert snapshot.provider == "Hampton Inn Minot"
    assert snapshot.total_price == 620
    assert snapshot.source_url == "https://hotel.example/source-price"
    assert component["link_type"] == "exact_source"
    assert component["link_label"] == "View source price"
    assert component["rating"] == 4.4


def test_deal_candidate_component_display_includes_serpapi_provider_source_link(session):
    vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)
    search_run = run_search_once(vacation.id, "manual", session=session, use_mock=False)
    result = source_result(
        session,
        search_run.id,
        "flight",
        {
            "source_name": "serpapi_google_flights",
            "result_type": "flight",
            "offers": [
                {
                    "source_name": "serpapi_google_flights",
                    "result_type": "flight",
                    "provider": "Delta",
                    "total_price": 333,
                    "currency": "USD",
                    "source_url": "https://flights.example/source-price",
                }
            ],
        },
        source_name="serpapi_google_flights",
    )
    snapshots = snapshots_from_source_result(vacation, result)
    deal = build_deal_candidates(session, vacation, search_run.id, snapshots)[0]

    component = json.loads(deal.normalized_result_json)["component_summary"][0]
    display = component_summary_for_deal(deal)[0]

    assert component["provider"] == "Delta"
    assert component["source_name"] == "serpapi_google_flights"
    assert component["link_label"] == "View source price"
    assert display["source_name_label"] == "SerpAPI Google Flights"
    assert display["link_url"] == "https://flights.example/source-price"


def test_package_builder_stores_component_link_fields(session):
    vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)
    search_run = run_search_once(vacation.id, "manual", session=session)
    deal = session.exec(select(DealCandidate).where(DealCandidate.search_run_id == search_run.id)).first()

    component = json.loads(deal.normalized_result_json)["component_summary"][0]
    source_link = json.loads(deal.source_links_json)[0]

    assert component["provider"] == "Mock Air"
    assert component["link_type"] == "search_reference"
    assert component["link_label"] == "Search reference"
    assert source_link["provider"] == "Mock Air"
    assert source_link["search_reference_url"]


def test_package_candidate_contains_air_hotel_car_provider_summaries(session):
    vacation = create_vacation(session, hotel_needed=True, airfare_needed=True, rental_car_needed=True)
    search_run = run_search_once(vacation.id, "manual", session=session)
    deal = session.exec(select(DealCandidate).where(DealCandidate.search_run_id == search_run.id)).first()

    providers = [component["provider"] for component in json.loads(deal.normalized_result_json)["component_summary"]]

    assert providers == ["Mock Air", "Mock Harbor Hotel", "Mock Rent-A-Car"]


def test_search_run_detail_renders_provider_source_metadata(session):
    vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)
    search_run = run_search_once(vacation.id, "manual", session=session)

    response = search_run_detail(search_run.id, request=None, session=session)

    assert response.status_code == 200
    assert b"Mock Air" in response.body
    assert b"Search reference" in response.body
    assert b"MOCK" in response.body


def test_search_run_detail_renders_serpapi_provider_link_metadata(session):
    vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)
    search_run = run_search_once(vacation.id, "manual", session=session, use_mock=False)
    source_result(
        session,
        search_run.id,
        "flight",
        {
            "source_name": "serpapi_google_flights",
            "result_type": "flight",
            "offers": [
                {
                    "source_name": "serpapi_google_flights",
                    "result_type": "flight",
                    "provider": "United",
                    "total_price": 355,
                    "currency": "USD",
                    "source_url": "https://flights.example/united",
                    "link_type": "exact_source",
                    "link_label": "View source price",
                }
            ],
        },
        source_name="serpapi_google_flights",
    )

    response = search_run_detail(search_run.id, request=None, session=session)

    assert response.status_code == 200
    assert b"United" in response.body
    assert b"SerpAPI Google Flights" in response.body
    assert b"View source price" in response.body


def test_search_run_detail_renders_fast_flights_provider_link_metadata(session):
    vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)
    search_run = run_search_once(vacation.id, "manual", session=session, use_mock=False)
    source_result(
        session,
        search_run.id,
        "flight",
        {
            "source_name": "fast_flights",
            "result_type": "flight",
            "offers": [
                {
                    "source_name": "fast_flights",
                    "result_type": "flight",
                    "provider": "American",
                    "label": "American PIT to ORD",
                    "total_price": 296,
                    "currency": "USD",
                    "search_reference_url": "https://www.google.com/search?q=American+flight+PIT+ORD",
                    "link_type": "search_reference",
                    "link_label": "Search reference",
                }
            ],
        },
        source_name="fast_flights",
    )

    response = search_run_detail(search_run.id, request=None, session=session)

    assert response.status_code == 200
    assert b"American" in response.body
    assert b"fast_flights" in response.body
    assert b"Search reference" in response.body


def test_price_history_endpoint_page_returns_graph_data(session):
    vacation = create_vacation(session, hotel_needed=False, airfare_needed=True)
    run_search_once(vacation.id, "manual", session=session)

    response = price_history_page(vacation.id, request=None, session=session)

    assert response.status_code == 200
    assert response.context["history"]["snapshots"]
    assert response.context["history_points"]
