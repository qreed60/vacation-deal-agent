import json
import stat
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


def make_fake_command(tmp_path, body: str):
    path = tmp_path / "trvl"
    path.write_text("#!/usr/bin/env python3\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


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
    assert result.currency == "USD"
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


def test_fast_flights_diagnostic_raw_includes_result_and_sample_public_fields(monkeypatch):
    module = FakeFastFlightsModule(
        FakeResult(
            current_price="low",
            flights=[FakeFlight(name="American", price="$296", _private="hidden", duration="2 hr")],
        )
    )
    install_fake_fast_flights(monkeypatch, module)

    result = probe_fast_flights(
        ProbeRequest(candidate="fast-flights", origin="PIT", destination="ORD", depart="2026-09-18")
    )

    assert result.diagnostic_raw["api_style"] == "v2"
    assert result.diagnostic_raw["result_public_fields"]["current_price"] == "low"
    assert result.diagnostic_raw["flight_count"] == 1
    assert result.diagnostic_raw["sample_flights"][0]["public_fields"]["name"] == "American"
    assert "_private" not in result.diagnostic_raw["sample_flights"][0]["public_fields"]
    assert "price" in result.diagnostic_raw["sample_flights"][0]["field_names"]


def test_fast_flights_diagnostic_raw_limits_sample_flights_to_five(monkeypatch):
    module = FakeFastFlightsModule(
        FakeResult(current_price="typical", flights=[FakeFlight(name="American", price=100 + index) for index in range(8)])
    )
    install_fake_fast_flights(monkeypatch, module)

    result = probe_fast_flights(
        ProbeRequest(candidate="fast-flights", origin="PIT", destination="ORD", depart="2026-09-18")
    )

    assert result.diagnostic_raw["flight_count"] == 8
    assert len(result.diagnostic_raw["sample_flights"]) == 5


def test_nested_provider_extraction_from_segments_and_legs():
    result = normalize_candidate_result(
        "fast-flights",
        {
            "flights": [
                {
                    "price": "$296",
                    "segments": [{"airline": "American"}, {"airline": "American"}],
                },
                {
                    "price": "US$310",
                    "legs": [{"carrier": "Delta"}, {"carrier": "United"}],
                },
            ]
        },
        "flight",
        "PIT to ORD",
    )

    assert result.status == "usable"
    assert result.provider == "American"
    assert result.total_price == 296.0
    assert result.currency == "USD"


def test_multiple_nested_providers_are_joined_uniquely():
    result = normalize_candidate_result(
        "fast-flights",
        {
            "flights": [
                {
                    "price": "296 USD",
                    "legs": [{"airline": "Delta"}, {"carrier": "United"}, {"carrier": "Delta"}],
                }
            ]
        },
        "flight",
        "JFK to LAX",
    )

    assert result.status == "usable"
    assert result.provider == "Delta, United"
    assert result.total_price == 296.0
    assert result.currency == "USD"


def test_airport_codes_are_not_treated_as_providers():
    result = normalize_candidate_result(
        "fast-flights",
        {"flights": [{"name": "JFK", "price": "$296"}, {"carrier": "LAX", "price": "$310"}]},
        "flight",
        "JFK to LAX",
    )

    assert result.status == "not_usable_for_pricing"
    assert result.provider is None


def test_us_dollar_price_strings_parse_as_usd():
    dollar = normalize_candidate_result(
        "fast-flights",
        {"flights": [{"provider": "American", "price": "$296"}]},
        "flight",
        "PIT to ORD",
    )
    us_dollar = normalize_candidate_result(
        "fast-flights",
        {"flights": [{"provider": "American", "price_raw": "US$296"}]},
        "flight",
        "PIT to ORD",
    )

    assert dollar.total_price == 296.0
    assert dollar.currency == "USD"
    assert us_dollar.total_price == 296.0
    assert us_dollar.currency == "USD"


def test_repeated_no_provider_notes_are_collapsed_with_count():
    result = normalize_candidate_result(
        "fast-flights",
        {"flights": [{"price": 100}, {"price": 200}, {"price": 300}]},
        "flight",
        "JFK to LAX",
    )

    assert result.status == "not_usable_for_pricing"
    assert result.notes == ["Structured price was present without provider on 3 result(s)."]


def test_huge_upstream_html_error_is_truncated(monkeypatch):
    huge_error = "No flights found:\n" + ("<html>body</html>\n" * 300)
    module = FakeFastFlightsModule(error=RuntimeError(huge_error))
    install_fake_fast_flights(monkeypatch, module)

    result = probe_fast_flights(
        ProbeRequest(candidate="fast-flights", origin="PIT", destination="MCO", depart="2026-09-18")
    )

    assert result.status == "failed"
    assert len(result.error) < len(huge_error)
    assert result.error.endswith("[truncated]")
    assert len(result.diagnostic_error_excerpt) <= 1815


def test_pit_ord_style_mocked_result_remains_usable(monkeypatch):
    module = FakeFastFlightsModule(FakeResult(current_price="typical", flights=[FakeFlight(name="American", price="$296")]))
    install_fake_fast_flights(monkeypatch, module)

    result = probe_fast_flights(
        ProbeRequest(candidate="fast-flights", origin="PIT", destination="ORD", depart="2026-09-18")
    )

    assert result.status == "usable"
    assert result.provider == "American"
    assert result.total_price == 296.0


def test_jfk_lax_style_mocked_result_with_nested_provider_becomes_usable(monkeypatch):
    module = FakeFastFlightsModule(
        FakeResult(
            current_price="low",
            flights=[FakeFlight(price_raw="US$296", flight_details={"segments": [{"airline": "JetBlue"}]})],
        )
    )
    install_fake_fast_flights(monkeypatch, module)

    result = probe_fast_flights(
        ProbeRequest(candidate="fast-flights", origin="JFK", destination="LAX", depart="2026-09-18")
    )

    assert result.status == "usable"
    assert result.provider == "JetBlue"
    assert result.total_price == 296.0


def test_jfk_lax_style_mocked_result_with_price_but_no_provider_stays_not_usable(monkeypatch):
    module = FakeFastFlightsModule(
        FakeResult(
            current_price="low",
            flights=[FakeFlight(price="$296", duration="6 hr"), FakeFlight(price="$310", stops=0)],
        )
    )
    install_fake_fast_flights(monkeypatch, module)

    result = probe_fast_flights(
        ProbeRequest(candidate="fast-flights", origin="JFK", destination="LAX", depart="2026-09-18")
    )

    assert result.status == "not_usable_for_pricing"
    assert result.total_price is None
    assert result.diagnostic_raw["flight_count"] == 2
    assert result.diagnostic_raw["sample_flights"][0]["public_fields"]["price"] == "$296"


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


def test_probe_trvl_flight_uses_local_binary_and_returns_usable(tmp_path, monkeypatch):
    command = make_fake_command(
        tmp_path,
        """
import json, sys
print(json.dumps({"success": True, "flights": [{"price": 296, "currency": "USD", "provider": "Delta"}]}))
""",
    )
    monkeypatch.setattr(free_travel_probe.trvl_adapter, "resolve_trvl_binary", lambda configured_path=None: str(command))

    results = probe_candidate(
        ProbeRequest(candidate="trvl", origin="PIT", destination="MOT", depart="2026-09-18", return_date="2026-09-21")
    )

    assert len(results) == 1
    assert results[0].status == "usable"
    assert results[0].component_type == "flight"
    assert results[0].provider == "Delta"
    assert results[0].total_price == 296


def test_probe_trvl_hotel_uses_local_binary_and_returns_usable(tmp_path, monkeypatch):
    command = make_fake_command(
        tmp_path,
        """
import json, sys
print(json.dumps({"success": True, "hotels": [{"name": "Mainstay Suites", "price": 155, "currency": "USD"}]}))
""",
    )
    monkeypatch.setattr(free_travel_probe.trvl_adapter, "resolve_trvl_binary", lambda configured_path=None: str(command))

    results = probe_candidate(
        ProbeRequest(candidate="trvl", destination="Minot, ND", check_in="2026-09-18", check_out="2026-09-21")
    )

    assert len(results) == 1
    assert results[0].status == "usable"
    assert results[0].component_type == "hotel"
    assert results[0].provider == "Mainstay Suites"
    assert results[0].total_price == 155


def test_probe_trvl_nonzero_without_json_is_failed(tmp_path, monkeypatch):
    command = make_fake_command(
        tmp_path,
        """
import sys
print("failure", file=sys.stderr)
sys.exit(2)
""",
    )
    monkeypatch.setattr(free_travel_probe.trvl_adapter, "resolve_trvl_binary", lambda configured_path=None: str(command))

    results = probe_candidate(ProbeRequest(candidate="trvl", origin="PIT", destination="MOT", depart="2026-09-18"))

    assert results[0].status == "failed"
    assert "trvl exited with code 2" in results[0].error


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
