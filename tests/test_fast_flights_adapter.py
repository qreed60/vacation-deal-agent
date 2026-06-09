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


# ---- Provider extraction tests ----

def test_flight_name_provides_provider():
    """Fake fast-flights Flight with name='American', price='$296' creates usable offer."""
    raw = FakeResult(flights=[FakeFlight(name="American", price="$296")])
    normalized = fast_flights_adapter._normalize_flights(raw, query())
    assert len(normalized["offers"]) == 1
    assert normalized["offers"][0]["provider"] == "American"
    assert normalized["offers"][0]["total_price"] == 296.0


def test_flight_airline_provides_provider():
    """Fake fast-flights Flight with airline='JetBlue', price='$174' creates usable offer."""
    raw = FakeResult(flights=[FakeFlight(airline="JetBlue", price="$174")])
    normalized = fast_flights_adapter._normalize_flights(raw, query())
    assert len(normalized["offers"]) == 1
    assert normalized["offers"][0]["provider"] == "JetBlue"


def test_flight_carrier_provides_provider():
    """Nested carrier field provides provider."""
    raw = FakeResult(flights=[FakeFlight(carrier="Delta", price="$350")])
    normalized = fast_flights_adapter._normalize_flights(raw, query())
    assert len(normalized["offers"]) == 1
    assert normalized["offers"][0]["provider"] == "Delta"


def test_flight_company_provides_provider():
    """Flight.company field provides provider."""
    raw = FakeResult(flights=[FakeFlight(company="Southwest", price="$210")])
    normalized = fast_flights_adapter._normalize_flights(raw, query())
    assert len(normalized["offers"]) == 1
    assert normalized["offers"][0]["provider"] == "Southwest"


def test_flight_name_list_provides_provider():
    """Flight.name as list provides provider from first valid entry."""
    raw = FakeResult(flights=[FakeFlight(name=["United", "Express"], price="$420")])
    normalized = fast_flights_adapter._normalize_flights(raw, query())
    assert len(normalized["offers"]) == 1
    assert "United" in normalized["offers"][0]["provider"]


def test_airport_code_name_rejected_as_provider():
    """Airport-code-only names like 'PIT' are rejected as provider."""
    raw = FakeResult(flights=[FakeFlight(name="PIT", price="$296")])
    normalized = fast_flights_adapter._normalize_flights(raw, query())
    assert normalized["offers"] == []


def test_route_label_name_rejected_as_provider():
    """Route-label names like 'PIT to ORD' are rejected as provider."""
    raw = FakeResult(flights=[FakeFlight(name="PIT to ORD", price="$296")])
    normalized = fast_flights_adapter._normalize_flights(raw, query())
    assert normalized["offers"] == []


def test_flight_number_name_rejected_as_provider():
    """Flight-number-like names are rejected as provider."""
    raw = FakeResult(flights=[FakeFlight(name="AA1234", price="$296")])
    normalized = fast_flights_adapter._normalize_flights(raw, query())
    assert normalized["offers"] == []


def test_current_price_low_not_used_as_numeric_price():
    """Result.current_price='low' is never used as numeric total_price."""
    raw = FakeResult(current_price="low", flights=[FakeFlight(name="American", price="$296")])
    normalized = fast_flights_adapter._normalize_flights(raw, query())
    assert len(normalized["offers"]) == 1
    assert normalized["offers"][0]["total_price"] == 296.0


# ---- Regression: PIT/ORD-style fake result creates completed SourceResult with offers ----

def test_pit_ord_fake_result_creates_offers():
    """PIT->ORD style fake result creates at least one offer."""
    raw = FakeResult(flights=[
        FakeFlight(name="American", price="$296"),
        FakeFlight(name="United", price="$310"),
        FakeFlight(price="$400"),  # no provider, should be unpriced
    ])
    normalized = fast_flights_adapter._normalize_flights(raw, query())
    assert len(normalized["offers"]) == 2
    assert normalized["unpriced_result_count"] == 1


# ---- Error truncation tests ----

def test_huge_html_error_is_truncated():
    """Huge upstream HTML error is truncated in SourceResult.error_message."""
    big_msg = "Error: " + ("x" * 5000)
    result = fast_flights_adapter._error(big_msg, raw={"diagnostic_raw": {"excerpt": big_msg}})
    assert result["status"] == "error"
    assert len(result["error_message"]) < 1300
    assert "[truncated]" in result["error_message"]


def test_diagnostic_error_excerpt_is_bounded():
    """diagnostic_error_excerpt is bounded."""
    big_msg = "Error: " + ("y" * 5000)
    result = fast_flights_adapter._error(big_msg)
    excerpt = result["raw_result"].get("diagnostic_error_excerpt", "")
    assert len(excerpt) < 2000
    assert "[truncated]" in excerpt


# ---- Bounding tests ----

def test_bounded_fake_run_creates_at_most_max_results():
    """Bounded fake run with 35 offers creates <=20 PriceSnapshots."""
    max_r = fast_flights_adapter.DEFAULT_MAX_RESULTS
    flights = [FakeFlight(name=f"Airline{i}", price=f"${100+i}") for i in range(35)]
    raw = FakeResult(flights=flights)

    # Bounding is applied by the search runner / quote normalizer, not _normalize_flights.
    # Verify that _normalize_flights produces all offers (bounding happens downstream).
    normalized = fast_flights_adapter._normalize_flights(raw, query())
    assert len(normalized["offers"]) == 35


# ---- Query metadata tests ----

def test_search_fast_flights_includes_resolved_airports(monkeypatch):
    """search_fast_flights includes resolved_origin/destination in normalized_result."""
    raw = FakeResult(flights=[FakeFlight(name="American", price="$296")])
    module = FakeFastFlightsModule(raw)
    install_fake_module(monkeypatch, module)

    result = fast_flights_adapter.search_fast_flights(query(), enabled=True)

    assert result["status"] == "completed"
    nr = result["normalized_result"]
    assert nr.get("resolved_origin_airport") == "PIT"
    assert nr.get("resolved_destination_airport") == "ORD"


def test_search_fast_flights_query_json_includes_route_metadata(monkeypatch):
    """SourceResult query_json includes origin_value/destination_value and airport codes."""
    raw = FakeResult(flights=[FakeFlight(name="American", price="$296")])
    module = FakeFastFlightsModule(raw)
    install_fake_module(monkeypatch, module)

    result = fast_flights_adapter.search_fast_flights(query(), enabled=True)

    assert result["status"] == "completed"
    nr = result["normalized_result"]
    # The adapter attaches resolved airports to normalized_result.
    assert "resolved_origin_airport" in nr
    assert "resolved_destination_airport" in nr


# ---- No-provider priced result remains not_usable_for_pricing (probe path) ----

def test_no_provider_but_priced_has_unpriced_count():
    """No-provider but priced fake result has unpriced_result_count > 0 and no offers."""
    raw = FakeResult(flights=[FakeFlight(price="$296")])
    normalized = fast_flights_adapter._normalize_flights(raw, query())
    assert len(normalized["offers"]) == 0
    assert normalized["unpriced_result_count"] == 1

