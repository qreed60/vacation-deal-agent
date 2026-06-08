from app.adapters.amadeus import (
    AmadeusClient,
    normalize_flight_offers,
    normalize_hotel_list,
    normalize_hotel_offers,
    resolve_iata_code,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class TokenClient:
    posts = 0

    def __init__(self, timeout):
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def post(self, url, data):
        self.__class__.posts += 1
        return FakeResponse({"access_token": "token-123", "expires_in": 3600})


def test_iata_resolution_from_text_and_fallbacks():
    assert resolve_iata_code("Pittsburgh (PIT)") == "PIT"
    assert resolve_iata_code("PIT") == "PIT"
    assert resolve_iata_code("Virginia Beach") == "ORF"
    assert resolve_iata_code("Unknown place") is None


def test_amadeus_token_is_cached(monkeypatch):
    TokenClient.posts = 0
    import app.adapters.amadeus as amadeus_module

    monkeypatch.setattr(amadeus_module.httpx, "Client", TokenClient)
    client = AmadeusClient(
        base_url="https://amadeus.test",
        client_id="id",
        client_secret="secret",
        enabled=True,
    )

    assert client.access_token() == "token-123"
    assert client.access_token() == "token-123"
    assert TokenClient.posts == 1


def test_missing_amadeus_credentials_skip_flight():
    client = AmadeusClient(base_url="https://amadeus.test", client_id="", client_secret="", enabled=True)

    result = client.flight_offers_search({"origin": "PIT", "destination": "MCO", "start_date": "2026-07-01"})

    assert result["status"] == "skipped"
    assert "credentials" in result["error_message"]


def test_flight_offer_normalization():
    raw = {
        "data": [
            {
                "price": {"grandTotal": "321.10", "currency": "USD"},
                "itineraries": [
                    {
                        "segments": [
                            {
                                "carrierCode": "AA",
                                "departure": {"iataCode": "PIT"},
                                "arrival": {"iataCode": "MCO"},
                            }
                        ]
                    }
                ],
            }
        ]
    }

    normalized = normalize_flight_offers(raw, "PIT", "MCO", "2026-07-01", "2026-07-08")

    offer = normalized["offers"][0]
    assert offer["source_name"] == "amadeus"
    assert offer["result_type"] == "flight"
    assert offer["total_price"] == "321.10"
    assert offer["currency"] == "USD"
    assert offer["airline_carrier_codes"] == ["AA"]
    assert offer["itinerary_summary"] == "PIT->MCO"
    assert offer["raw_offer_reference"] == raw["data"][0]


def test_hotel_list_normalization():
    raw = {"data": [{"hotelId": "H1", "name": "Source Hotel", "geoCode": {"latitude": 1.2, "longitude": 3.4}}]}

    normalized = normalize_hotel_list(raw, "2026-07-01", "2026-07-08")

    hotel = normalized["hotels"][0]
    assert hotel["hotel_id"] == "H1"
    assert hotel["hotel_name"] == "Source Hotel"
    assert hotel["location"] == {"latitude": 1.2, "longitude": 3.4}
    assert hotel["total_price"] is None
    assert hotel["raw_hotel_reference"] == raw["data"][0]


def test_hotel_offer_normalization():
    raw = {
        "data": [
            {
                "hotel": {"hotelId": "H1", "name": "Source Hotel", "geoCode": {"latitude": 1.2, "longitude": 3.4}},
                "offers": [
                    {
                        "price": {"total": "900.00", "currency": "USD"},
                        "room": {"description": {"text": "King room"}},
                    }
                ],
            }
        ]
    }

    normalized = normalize_hotel_offers(raw, "2026-07-01", "2026-07-08")

    hotel = normalized["hotels"][0]
    assert hotel["hotel_id"] == "H1"
    assert hotel["total_price"] == "900.00"
    assert hotel["currency"] == "USD"
    assert hotel["room_offer_summary"] == "King room"
    assert hotel["raw_offer_reference"] == raw["data"][0]["offers"][0]
