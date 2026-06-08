from app.adapters import serpapi_travel


def test_serpapi_disabled_skips_flights():
    result = serpapi_travel.search_google_flights(
        {"origin": "PIT", "destination": "MOT", "start_date": "2026-09-18"},
        enabled=False,
        api_key="",
        base_url="https://serpapi.test/search",
    )

    assert result["status"] == "skipped"
    assert result["normalized_result"]["source_name"] == "serpapi_google_flights"
    assert "SERPAPI_ENABLED" in result["error_message"]


def test_serpapi_missing_key_skips_hotels():
    result = serpapi_travel.search_google_hotels(
        {"destination": "Minot, ND", "start_date": "2026-09-18", "end_date": "2026-09-21"},
        enabled=True,
        api_key="",
        base_url="https://serpapi.test/search",
    )

    assert result["status"] == "skipped"
    assert result["normalized_result"]["source_name"] == "serpapi_google_hotels"
    assert "API key" in result["error_message"]


def test_google_flights_fixture_normalizes_provider_price_currency_and_links():
    raw = {
        "search_parameters": {"currency": "USD"},
        "best_flights": [
            {
                "price": 312,
                "booking_token": "token-123",
                "flights": [
                    {
                        "airline": "American Airlines",
                        "flight_number": "AA 123",
                        "departure_airport": {"id": "PIT"},
                        "arrival_airport": {"id": "MOT"},
                    }
                ],
            }
        ],
    }

    normalized = serpapi_travel.normalize_flights(
        raw,
        {"origin": "PIT", "destination": "MOT", "start_date": "2026-09-18", "end_date": "2026-09-21"},
    )

    offer = normalized["offers"][0]
    assert offer["provider"] == "American Airlines"
    assert offer["total_price"] == 312
    assert offer["currency"] == "USD"
    assert offer["flight_numbers"] == ["AA 123"]
    assert offer["source_url"] is None
    assert offer["link_type"] == "search_reference"
    assert offer["link_label"] == "Search reference"
    assert offer["search_reference_url"]


def test_google_hotels_fixture_normalizes_name_price_rating_and_links():
    raw = {
        "search_parameters": {"currency": "USD"},
        "properties": [
            {
                "name": "Hampton Inn Minot",
                "total_rate": {"extracted_lowest": 620},
                "overall_rating": 4.4,
                "reviews": 881,
                "link": "https://hotel.example/source-price",
            }
        ],
    }

    normalized = serpapi_travel.normalize_hotels(
        raw,
        {"destination": "Minot, ND", "start_date": "2026-09-18", "end_date": "2026-09-21"},
    )

    hotel = normalized["hotels"][0]
    assert hotel["provider"] == "Hampton Inn Minot"
    assert hotel["hotel_name"] == "Hampton Inn Minot"
    assert hotel["total_price"] == 620
    assert hotel["currency"] == "USD"
    assert hotel["rating"] == 4.4
    assert hotel["user_rating_count"] == 881
    assert hotel["source_url"] == "https://hotel.example/source-price"
    assert hotel["link_type"] == "exact_source"
    assert hotel["link_label"] == "View source price"
