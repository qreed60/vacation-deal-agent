import json
import subprocess
import sys

from app.adapters import free_travel_probe
from app.adapters.free_travel_probe import (
    CandidateProbeResult,
    ProbeRequest,
    normalize_candidate_result,
    probe_fast_flights,
    probe_candidate,
    run_probe,
)


class FakeFastFlightsModule:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []
        self.created_legs = []
        self.create_filter_called = False

    def FlightData(self, *, date, from_airport, to_airport, max_stops=None):
        leg = {
            "date": date,
            "from_airport": from_airport,
            "to_airport": to_airport,
            "max_stops": max_stops,
        }
        self.created_legs.append(leg)
        return leg

    def Passengers(self, *, adults=0, children=0, infants_in_seat=0, infants_on_lap=0):
        return {
            "adults": adults,
            "children": children,
            "infants_in_seat": infants_in_seat,
            "infants_on_lap": infants_on_lap,
        }

    def create_filter(self, **kwargs):
        self.create_filter_called = True
        raise AssertionError(f"create_filter should not be called: {kwargs}")

    def get_flights(
        self,
        *,
        flight_data,
        trip,
        passengers,
        seat,
        fetch_mode="common",
        max_stops=None,
    ):
        self.calls.append(
            {
                "flight_data": flight_data,
                "trip": trip,
                "passengers": passengers,
                "seat": seat,
                "fetch_mode": fetch_mode,
                "max_stops": max_stops,
            }
        )
        if self.error:
            raise self.error
        return self.result


class FakeFlight:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeResult:
    def __init__(self, *, current_price, flights):
        self.current_price = current_price
        self.flights = flights


def install_fake_fast_flights(monkeypatch, module):
    monkeypatch.setattr(free_travel_probe, "_module_exists", lambda name: name == "fast_flights")
    monkeypatch.setattr(free_travel_probe.importlib, "import_module", lambda name: module)


def test_probe_cli_help_works():
    result = subprocess.run(
        [sys.executable, "scripts/probe_free_travel_sources.py", "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--candidate" in result.stdout
    assert "--all" in result.stdout


def test_missing_candidate_returns_missing_dependency_not_crash(monkeypatch):
    monkeypatch.setattr(free_travel_probe, "_module_exists", lambda name: False)
    monkeypatch.setattr(free_travel_probe.shutil, "which", lambda name: None)

    result = probe_candidate(ProbeRequest(candidate="fast-flights", origin="PIT", destination="MOT"))[0]

    assert result.status == "missing_dependency"
    assert result.install_hint


def test_normalized_flight_result_with_provider_price_is_usable():
    result = normalize_candidate_result(
        "fast-flights",
        {
            "flights": [
                {
                    "airline": "Delta Air Lines",
                    "carrier_code": "DL",
                    "total_price": "425.00",
                    "currency": "USD",
                    "departure": "2026-09-18T06:00:00",
                    "arrival": "2026-09-18T12:00:00",
                }
            ]
        },
        "flight",
        "PIT to MOT",
    )

    assert result.status == "usable"
    assert result.provider == "Delta Air Lines"
    assert result.provider_code == "DL"
    assert result.total_price == 425.0
    assert result.currency == "USD"


def test_fast_flights_v2_result_with_provider_price_is_usable(monkeypatch):
    module = FakeFastFlightsModule(
        FakeResult(
            current_price="low",
            flights=[FakeFlight(name="Delta Air Lines", price="$425", departure="PIT 06:00", arrival="MOT 12:00")],
        )
    )
    install_fake_fast_flights(monkeypatch, module)

    result = probe_fast_flights(
        ProbeRequest(
            candidate="fast-flights",
            origin="PIT",
            destination="MOT",
            depart="2026-09-18",
            return_date="2026-09-21",
            adults=2,
            children=3,
        )
    )

    assert result.status == "usable"
    assert result.provider == "Delta Air Lines"
    assert result.total_price == 425.0
    assert "fetch_mode=common" in result.notes


def test_fast_flights_get_flights_receives_common_fetch_mode_and_no_create_filter(monkeypatch):
    module = FakeFastFlightsModule(FakeResult(current_price="low", flights=[FakeFlight(provider="United", price=500)]))
    install_fake_fast_flights(monkeypatch, module)

    probe_fast_flights(
        ProbeRequest(
            candidate="fast-flights",
            origin="PIT",
            destination="MOT",
            depart="2026-09-18",
            return_date="2026-09-21",
            adults=2,
            children=3,
        )
    )

    assert module.create_filter_called is False
    assert module.calls[0]["fetch_mode"] == "common"
    assert module.calls[0]["seat"] == "economy"
    assert module.calls[0]["max_stops"] is None


def test_fast_flights_round_trip_produces_two_flight_data_legs(monkeypatch):
    module = FakeFastFlightsModule(FakeResult(current_price="low", flights=[FakeFlight(provider="United", price=500)]))
    install_fake_fast_flights(monkeypatch, module)

    probe_fast_flights(
        ProbeRequest(
            candidate="fast-flights",
            origin="PIT",
            destination="MOT",
            depart="2026-09-18",
            return_date="2026-09-21",
        )
    )

    assert module.calls[0]["trip"] == "round-trip"
    assert module.calls[0]["flight_data"] == [
        {"date": "2026-09-18", "from_airport": "PIT", "to_airport": "MOT", "max_stops": None},
        {"date": "2026-09-21", "from_airport": "MOT", "to_airport": "PIT", "max_stops": None},
    ]


def test_fast_flights_one_way_produces_one_flight_data_leg(monkeypatch):
    module = FakeFastFlightsModule(FakeResult(current_price="low", flights=[FakeFlight(provider="United", price=500)]))
    install_fake_fast_flights(monkeypatch, module)

    probe_fast_flights(
        ProbeRequest(
            candidate="fast-flights",
            origin="PIT",
            destination="MOT",
            depart="2026-09-18",
        )
    )

    assert module.calls[0]["trip"] == "one-way"
    assert module.calls[0]["flight_data"] == [
        {"date": "2026-09-18", "from_airport": "PIT", "to_airport": "MOT", "max_stops": None},
    ]


def test_fast_flights_current_price_is_not_used_as_total_price(monkeypatch):
    module = FakeFastFlightsModule(FakeResult(current_price="low", flights=[FakeFlight(provider="Delta Air Lines")]))
    install_fake_fast_flights(monkeypatch, module)

    result = probe_fast_flights(
        ProbeRequest(candidate="fast-flights", origin="PIT", destination="MOT", depart="2026-09-18")
    )

    assert result.status == "not_usable_for_pricing"
    assert result.total_price is None
    assert "current_price" not in "".join(result.notes)


def test_fast_flights_provider_without_parseable_price_is_not_usable(monkeypatch):
    module = FakeFastFlightsModule(
        FakeResult(current_price="low", flights=[FakeFlight(provider="Delta Air Lines", price="low")])
    )
    install_fake_fast_flights(monkeypatch, module)

    result = probe_fast_flights(
        ProbeRequest(candidate="fast-flights", origin="PIT", destination="MOT", depart="2026-09-18")
    )

    assert result.status == "not_usable_for_pricing"
    assert result.provider is None
    assert result.total_price is None


def test_fast_flights_get_flights_exception_becomes_failed(monkeypatch):
    module = FakeFastFlightsModule(error=RuntimeError("upstream changed response"))
    install_fake_fast_flights(monkeypatch, module)

    result = probe_fast_flights(
        ProbeRequest(candidate="fast-flights", origin="PIT", destination="MOT", depart="2026-09-18")
    )

    assert result.status == "failed"
    assert "upstream changed response" in result.error
    assert "api_style=v2" in result.notes
    assert "fetch_mode=common" in result.notes


def test_normalized_hotel_result_with_provider_price_is_usable():
    result = normalize_candidate_result(
        "trvl",
        {"hotels": [{"hotel_name": "Mainstay Suites", "price": 510, "currency": "USD"}]},
        "hotel",
        "Minot, ND",
    )

    assert result.status == "usable"
    assert result.provider == "Mainstay Suites"
    assert result.total_price == 510.0
    assert result.component_type == "hotel"


def test_text_only_result_is_not_usable_for_pricing():
    result = normalize_candidate_result("fli", "Delta from PIT to MOT is usually cheap", "flight", "PIT to MOT")

    assert result.status == "not_usable_for_pricing"
    assert result.raw_result_available is True
    assert "text" in result.notes[0].lower()


def test_report_file_is_written_under_free_source_probe_dir(tmp_path, monkeypatch):
    def fake_probe(request):
        return [
            CandidateProbeResult(
                candidate=request.candidate,
                status="missing_dependency",
                component_type="flight",
                install_hint="install locally",
            )
        ]

    monkeypatch.setattr(free_travel_probe, "probe_candidate", fake_probe)

    report = run_probe([ProbeRequest(candidate="fast-flights")], report_dir=tmp_path / "data/free_source_probes")

    report_path = tmp_path / "data/free_source_probes" / report["report_path"].split("/")[-1]
    assert report_path.exists()
    assert json.loads(report_path.read_text())["results"][0]["status"] == "missing_dependency"
    assert "data/free_source_probes" in report["report_path"]


def test_one_failed_candidate_does_not_fail_all(tmp_path, monkeypatch):
    def fake_probe(request):
        if request.candidate == "fli":
            raise RuntimeError("candidate exploded")
        return [CandidateProbeResult(candidate=request.candidate, status="missing_dependency", component_type="flight")]

    monkeypatch.setattr(free_travel_probe, "probe_candidate", fake_probe)

    report = run_probe(
        [ProbeRequest(candidate="fli"), ProbeRequest(candidate="fast-flights")],
        report_dir=tmp_path / "data/free_source_probes",
    )

    statuses = {result["candidate"]: result["status"] for result in report["results"]}
    assert statuses["fli"] == "failed"
    assert statuses["fast-flights"] == "missing_dependency"
