from app.adapters import fast_flights_adapter


class FakeFastFlightsModule:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def FlightData(self, *, date, from_airport, to_airport, max_stops=None):
        return {"date": date, "from_airport": from_airport, "to_airport": to_airport, "max_stops": max_stops}

    def Passengers(self, *, adults=0, children=0, infants_in_seat=0, infants_on_lap=0):
        return {"adults": adults, "children": children}

    def get_flights(self, *, flight_data, trip, passengers, seat, fetch_mode="common", max_stops=None):
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
    def __init__(self, *, current_price="low", flights=None):
        self.current_price = current_price
        self.flights = flights or []


def query(**overrides):
    data = {
        "origin": "PIT",
        "destination": "ORD",
        "start_date": "2026-09-18",
        "end_date": "2026-09-21",
        "number_of_travelers": 1,
    }
    data.update(overrides)
    return data


def install_fake_module(monkeypatch, module):
    monkeypatch.setattr(fast_flights_adapter, "_module_exists", lambda name: name == "fast_flights")
    monkeypatch.setattr(fast_flights_adapter.importlib, "import_module", lambda name: module)


def test_disabled_fast_flights_skips():
    result = fast_flights_adapter.search_fast_flights(query(), enabled=False)

    assert result["status"] == "skipped"
    assert result["normalized_result"]["source_name"] == "fast_flights"
    assert "FAST_FLIGHTS_ENABLED" in result["error_message"]


def test_missing_fast_flights_dependency_skips(monkeypatch):
    monkeypatch.setattr(fast_flights_adapter, "_module_exists", lambda name: False)

    result = fast_flights_adapter.search_fast_flights(query(), enabled=True)

    assert result["status"] == "skipped"
    assert "not installed" in result["error_message"]


def test_unsafe_fetch_mode_is_forced_to_common(monkeypatch):
    module = FakeFastFlightsModule(FakeResult(flights=[FakeFlight(name="American", price="$296")]))
    install_fake_module(monkeypatch, module)

    result = fast_flights_adapter.search_fast_flights(query(), enabled=True, fetch_mode="fallback")

    assert result["status"] == "completed"
    assert module.calls[0]["fetch_mode"] == "common"
    assert "forced to common" in result["normalized_result"]["notes"][0]


def test_successful_fast_flights_fixture_normalizes_offer():
    raw = FakeResult(flights=[FakeFlight(name="American", price="$296", departure="6:15 AM", arrival="7:04 AM", stops=0)])

    normalized = fast_flights_adapter._normalize_flights(raw, query())

    offer = normalized["offers"][0]
    assert offer["provider"] == "American"
    assert offer["total_price"] == 296.0
    assert offer["currency"] == "USD"
    assert offer["source_name"] == "fast_flights"
    assert offer["link_type"] == "search_reference"
    assert offer["link_label"] == "Search reference"
    assert offer["search_reference_url"]
    assert offer["source_url"] is None
    assert normalized["diagnostic_raw"]["flight_count"] == 1


def test_fast_flights_without_provider_has_no_priced_offer():
    raw = FakeResult(flights=[FakeFlight(price="$296")])

    normalized = fast_flights_adapter._normalize_flights(raw, query())

    assert normalized["offers"] == []
    assert normalized["unpriced_result_count"] == 1


def test_fast_flights_without_numeric_price_has_no_priced_offer():
    raw = FakeResult(flights=[FakeFlight(name="American", price="low")])

    normalized = fast_flights_adapter._normalize_flights(raw, query())

    assert normalized["offers"] == []
    assert normalized["unpriced_result_count"] == 1


def test_upstream_fast_flights_exception_returns_error(monkeypatch):
    module = FakeFastFlightsModule(error=RuntimeError("No flights found: " + ("html " * 500)))
    install_fake_module(monkeypatch, module)

    result = fast_flights_adapter.search_fast_flights(query(), enabled=True)

    assert result["status"] == "error"
    assert "No flights found" in result["error_message"]
    assert len(result["error_message"]) < 1300
    assert result["raw_result"]["diagnostic_error_excerpt"].endswith("[truncated]")
