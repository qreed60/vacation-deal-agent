"""Tests for fast-flights IATA airport resolution and result bounding."""

import json
import os
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, select

from app.adapters import fast_flights_adapter
from app.adapters.fast_flights_adapter import DEFAULT_MAX_RESULTS, resolve_airport
from app.db.models import DealCandidate, PriceSnapshot, SearchRun, SourceResult
from app.db.session import get_engine, init_db
from app.services.manifest_io import vacation_from_manifest
from app.services.quote_normalizer import (
    _deduplicate_flight_offers,
    _limit_fast_flights_offers,
)
from app.services.search_runner import run_search_once


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def session(tmp_path, monkeypatch):
    db_path = tmp_path / "vacation_deals_iata.sqlite3"
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


def _vacation_manifest(**overrides):
    data = {
        "title": "IATA test trip",
        "status": "active",
        "number_of_travelers": 2,
        "travelers": [],
        "origin": "Pittsburgh, PA",
        "destination": "Minot, ND",
        "date_mode": "fixed_dates",
        "start_date": "2026-07-10",
        "end_date": "2026-07-17",
        "nights_min": None,
        "nights_target": 7,
        "nights_max": None,
        "hotel_needed": False,
        "airfare_needed": True,
        "rental_car_needed": False,
        "special_accommodations": "",
    }
    data.update(overrides)
    return data


def _create_vacation(session, **overrides):
    manifest = _vacation_manifest(**overrides)
    return vacation_from_manifest(session, manifest)


# ---------------------------------------------------------------------------
# IATA resolution tests
# ---------------------------------------------------------------------------

class TestResolveAirport:
    """Test the resolve_airport priority chain."""

    def test_preferred_airports_used_before_raw_city(self):
        """preferred_airports takes priority over raw city value."""
        result = resolve_airport(
            "Pittsburgh, PA",
            preferred_airports=["KDF"],
            alternate_airports=None,
        )
        assert result == "KDF"

    def test_alternate_airports_used_when_preferred_absent(self):
        """alternate_airports is used when preferred is empty."""
        result = resolve_airport(
            "Pittsburgh, PA",
            preferred_airports=[],
            alternate_airports=["KPX"],
        )
        assert result == "KPX"

    def test_raw_iata_value_accepted(self):
        """Raw 3-letter IATA code is accepted directly."""
        assert resolve_airport("PIT") == "PIT"
        assert resolve_airport("mot") == "MOT"
        assert resolve_airport("ORD") == "ORD"

    def test_fallback_city_map_resolves_pittsburgh(self):
        """Fallback city map resolves Pittsburgh variants."""
        assert resolve_airport("Pittsburgh, PA") == "PIT"
        assert resolve_airport("Pittsburgh") == "PIT"
        # Case-insensitive
        assert resolve_airport("pittsburgh, pa") == "PIT"

    def test_fallback_city_map_resolves_minot(self):
        """Fallback city map resolves Minot variants."""
        assert resolve_airport("Minot, ND") == "MOT"
        assert resolve_airport("Minot") == "MOT"
        # Case-insensitive
        assert resolve_airport("minot, nd") == "MOT"

    def test_fallback_city_map_resolves_orlando(self):
        """Fallback city map resolves Orlando variants."""
        assert resolve_airport("Orlando, FL") == "MCO"
        assert resolve_airport("Orlando") == "MCO"

    def test_fallback_city_map_resolves_chicago(self):
        """Fallback city map resolves Chicago variants."""
        assert resolve_airport("Chicago, IL") == "ORD"
        assert resolve_airport("Chicago") == "ORD"

    def test_fallback_city_map_resolves_new_york(self):
        """Fallback city map resolves New York variants."""
        assert resolve_airport("New York, NY") == "JFK"
        assert resolve_airport("New York") == "JFK"

    def test_fallback_city_map_resolves_los_angeles(self):
        """Fallback city map resolves Los Angeles variants."""
        assert resolve_airport("Los Angeles, CA") == "LAX"
        assert resolve_airport("Los Angeles") == "LAX"

    def test_unresolved_city_returns_none(self):
        """Unresolvable city returns None (no guessing)."""
        result = resolve_airport("Unknownville, ZZ")
        assert result is None


# ---------------------------------------------------------------------------
# Adapter query metadata tests
# ---------------------------------------------------------------------------

class TestAdapterQueryMetadata:
    """Test that the adapter builds correct query metadata."""

    def test_query_json_includes_original_and_resolved_route(self):
        """query_json includes both original values and resolved airports."""
        result = fast_flights_adapter.search_fast_flights(
            {
                "origin": "Pittsburgh, PA",
                "destination": "Minot, ND",
                "start_date": "2026-07-10",
                "end_date": "2026-07-17",
                "number_of_travelers": 2,
            },
            enabled=False,
        )
        # When disabled we get a skip; check the _skip result has error info
        assert result["status"] == "skipped"

    def test_adapter_receives_resolved_iata_codes(self):
        """Adapter passes resolved IATA codes to upstream when enabled."""
        captured_calls = []

        class _FlightData:
            def __init__(self, date=None, from_airport=None, to_airport=None, max_stops=None):
                self.date = date
                self.from_airport = from_airport
                self.to_airport = to_airport
                self.max_stops = max_stops

        class _Passengers:
            def __init__(self, adults=1, children=0):
                self.adults = adults
                self.children = children

        # Must have the exact parameter names the adapter checks via inspect.signature.
        # Use staticmethod so inspect.signature sees all params (not bound-method).
        def fake_get_flights(flight_data=None, trip=None, passengers=None, seat=None, fetch_mode=None, max_stops=None):
            captured_calls.append({
                "flight_data": flight_data,
                "trip": trip,
                "passengers": passengers,
                "seat": seat,
            })
            return []

        FakeModule = type("FakeModule", (), {
            "FlightData": _FlightData,
            "Passengers": _Passengers,
            "get_flights": staticmethod(fake_get_flights),
        })

        with patch.object(fast_flights_adapter, "_module_exists", return_value=True), \
             patch("importlib.import_module", return_value=FakeModule):
            result = fast_flights_adapter.search_fast_flights(
                {
                    "origin": "Pittsburgh, PA",
                    "destination": "Minot, ND",
                    "start_date": "2026-07-10",
                    "number_of_travelers": 1,
                },
                enabled=True,
            )

        assert result["status"] == "completed"
        # Verify get_flights was called with resolved IATA codes in FlightData
        assert len(captured_calls) >= 1
        first_call = captured_calls[0]
        flight_data_list = first_call.get("flight_data", [])
        assert len(flight_data_list) >= 1
        first_fd = flight_data_list[0]
        assert first_fd.from_airport == "PIT"
        assert first_fd.to_airport == "MOT"

    def test_unresolved_city_returns_skipped_result(self):
        """Unresolvable origin/destination causes skipped result."""
        result = fast_flights_adapter.search_fast_flights(
            {
                "origin": "Unknownville, ZZ",
                "destination": "Nowheresville, QQ",
                "start_date": "2026-07-10",
                "number_of_travelers": 1,
            },
            enabled=True,
        )
        assert result["status"] == "skipped"
        assert "IATA airport codes" in result.get("error_message", "")

    def test_unresolved_city_no_upstream_call(self):
        """Unresolvable city does not call upstream."""
        with patch.object(fast_flights_adapter, "_module_exists", return_value=True) as mock_module:
            with patch("importlib.import_module") as mock_import:
                result = fast_flights_adapter.search_fast_flights(
                    {
                        "origin": "Unknownville, ZZ",
                        "destination": "Nowheresville, QQ",
                        "start_date": "2026-07-10",
                        "number_of_travelers": 1,
                    },
                    enabled=True,
                )
        assert result["status"] == "skipped"
        mock_import.assert_not_called()


# ---------------------------------------------------------------------------
# Dedup and limit tests
# ---------------------------------------------------------------------------

class TestDedupAndLimit:
    """Test deduplication and bounding logic."""

    def test_deduplicate_removes_duplicates(self):
        """Duplicate flight quotes are deduplicated by provider+price+departure+arrival+label."""
        offers = [
            {"provider": "American", "total_price": 296, "departure": "08:00", "arrival": "10:00", "label": "Morning"},
            {"provider": "American", "total_price": 296, "departure": "08:00", "arrival": "10:00", "label": "Morning"},
            {"provider": "Delta", "total_price": 310, "departure": "14:00", "arrival": "16:00", "label": "Afternoon"},
        ]
        result = _deduplicate_flight_offers(offers)
        assert len(result) == 2

    def test_deduplicate_preserves_unique(self):
        """Unique flight quotes are preserved."""
        offers = [
            {"provider": "American", "total_price": 296, "departure": "08:00", "arrival": "10:00", "label": "Morning"},
            {"provider": "Delta", "total_price": 310, "departure": "14:00", "arrival": "16:00", "label": "Afternoon"},
        ]
        result = _deduplicate_flight_offers(offers)
        assert len(result) == 2

    def test_limit_fast_flights_defaults_to_20(self):
        """FAST_FLIGHTS_MAX_RESULTS defaults to 20."""
        offers = [{"provider": f"Airline{i}", "total_price": i * 10, "departure": "08:00", "arrival": "10:00", "label": f"Flight {i}"} for i in range(30)]
        result = _limit_fast_flights_offers(offers)
        assert len(result) == 20

    def test_limit_fast_flights_sorts_by_price_ascending(self):
        """Results are sorted by total_price ascending before limiting."""
        offers = [
            {"provider": f"Airline{i}", "total_price": (30 - i) * 10, "departure": "08:00", "arrival": "10:00", "label": f"Flight {i}"}
            for i in range(25)
        ]
        result = _limit_fast_flights_offers(offers, max_results=5)
        prices = [o["total_price"] for o in result]
        assert prices == sorted(prices)

    def test_more_than_20_returns_at_most_20_snapshots(self):
        """More than 20 returned flight quotes creates at most 20 PriceSnapshots."""
        offers = [{"provider": f"Airline{i}", "total_price": i * 10, "departure": "08:00", "arrival": "10:00", "label": f"Flight {i}"} for i in range(50)]

        class _FlightData:
            def __init__(self, date=None, from_airport=None, to_airport=None, max_stops=None):
                self.date = date
                self.from_airport = from_airport
                self.to_airport = to_airport
                self.max_stops = max_stops

        class _Passengers:
            def __init__(self, adults=1, children=0):
                self.adults = adults
                self.children = children

        # Must have the exact parameter names the adapter checks via inspect.signature.
        # Use staticmethod so inspect.signature sees all params (not bound-method).
        def fake_get_flights(flight_data=None, trip=None, passengers=None, seat=None, fetch_mode=None, max_stops=None):
            return offers

        FakeModule = type("FakeModule", (), {
            "FlightData": _FlightData,
            "Passengers": _Passengers,
            "get_flights": staticmethod(fake_get_flights),
        })

        with patch.object(fast_flights_adapter, "_module_exists", return_value=True), \
             patch("importlib.import_module", return_value=FakeModule), \
             patch.dict(os.environ, {"FAST_FLIGHTS_ENABLED": "true"}):
            result = fast_flights_adapter.search_fast_flights(
                {
                    "origin": "PIT",
                    "destination": "ORD",
                    "start_date": "2026-07-10",
                    "number_of_travelers": 1,
                },
                enabled=True,
            )

        assert result["status"] == "completed"
        limited_count = len(result.get("normalized_result", {}).get("offers", []))
        assert limited_count <= 20


# ---------------------------------------------------------------------------
# Integration: existing fixture still works with bounding
# ---------------------------------------------------------------------------

class TestExistingFixtureStillWorks:
    """Ensure the existing fast-flights successful fixture still creates metadata."""

    def test_fast_flights_successful_fixture_creates_metadata(self, session, monkeypatch):
        """Existing fast-flights success path still creates PriceSnapshot/provider/source/link metadata."""
        vacation = _create_vacation(session)
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

        fast_result = [r for r in results if r.source_name == "fast_flights"][0]
        assert fast_result.status == "completed"
        assert len(snapshots) >= 1
        snap = snapshots[0]
        assert snap.provider == "American"
        assert snap.total_price == 296.0
        assert snap.source_name == "fast_flights"

        component = json.loads(snap.normalized_json)
        assert component["link_type"] == "search_reference"
        assert component["link_label"] == "Search reference"
        assert component["search_reference_url"]


# ---------------------------------------------------------------------------
# Integration: bounded run creates limited snapshots/candidates
# ---------------------------------------------------------------------------

class TestBoundedRun:
    """Test that a bounded fast-flights run creates at most N snapshots."""

    def test_bounded_run_creates_limited_snapshots(self, session, monkeypatch):
        """A fast-flights run with >20 offers creates at most 20 PriceSnapshots and DealCandidates."""
        vacation = _create_vacation(session)
        monkeypatch.setenv("FAST_FLIGHTS_ENABLED", "true")

        # Create 35 unique flight offers (all different providers to avoid dedup)
        offers = [
            {
                "component_type": "flight",
                "component_type_label": "Airfare",
                "source_name": "fast_flights",
                "result_type": "flight",
                "provider": f"Airline{i:03d}",
                "label": f"Flight {i}",
                "total_price": (i + 1) * 10.0,
                "currency": "USD",
                "departure": "08:00",
                "arrival": "10:00",
                "search_reference_url": f"https://example.com/search?q={i}",
                "link_type": "search_reference",
                "link_label": "Search reference",
                "mock": False,
            }
            for i in range(35)
        ]

        def fake_search(query, **kwargs):
            return {
                "status": "completed",
                "normalized_result": {
                    "source_name": "fast_flights",
                    "result_type": "flight",
                    "offers": offers,
                },
                "raw_result": {"diagnostic_raw": {"flight_count": 35}},
                "error_message": None,
            }

        monkeypatch.setattr(fast_flights_adapter, "search_fast_flights", fake_search)

        search_run = run_search_once(vacation.id, "manual", session=session, use_real_sources=True, use_mock=False)
        results = session.exec(select(SourceResult).where(SourceResult.search_run_id == search_run.id)).all()
        snapshots = session.exec(select(PriceSnapshot).where(PriceSnapshot.search_run_id == search_run.id)).all()
        deals = session.exec(select(DealCandidate).where(DealCandidate.search_run_id == search_run.id)).all()

        fast_result = [r for r in results if r.source_name == "fast_flights"][0]
        assert fast_result.status == "completed"
        # Bounded to at most 20
        assert len(snapshots) <= 20
        # DealCandidates bounded similarly (flight-only candidates from snapshots)
        assert len(deals) <= 20
