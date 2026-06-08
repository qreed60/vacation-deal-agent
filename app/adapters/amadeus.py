from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import httpx


IATA_FALLBACKS = {
    "pittsburgh": "PIT",
    "minot": "MOT",
    "virginia beach": "ORF",
    "orlando": "MCO",
    "new york": "NYC",
    "chicago": "CHI",
}


def resolve_iata_code(value: str | None) -> str | None:
    if not value:
        return None
    stripped = value.strip()
    exact = re.fullmatch(r"[A-Z]{3}", stripped)
    if exact:
        return stripped
    parenthetical = re.search(r"\(([A-Z]{3})\)", stripped)
    if parenthetical:
        return parenthetical.group(1)
    uppercase_token = re.search(r"\b([A-Z]{3})\b", stripped)
    if uppercase_token:
        return uppercase_token.group(1)
    return IATA_FALLBACKS.get(stripped.lower())


def _skip(result_type: str, reason: str) -> dict[str, Any]:
    return {
        "status": "skipped",
        "normalized_result": {"source_name": "amadeus", "result_type": result_type, "reason": reason},
        "raw_result": {},
        "error_message": reason,
    }


def _error(result_type: str, message: str, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": "error",
        "normalized_result": {"source_name": "amadeus", "result_type": result_type},
        "raw_result": raw or {},
        "error_message": message,
    }


@dataclass
class AmadeusClient:
    base_url: str
    client_id: str
    client_secret: str
    enabled: bool = False
    timeout_seconds: float = 8.0

    _access_token: str | None = None
    _token_expires_at: float = 0.0

    def configured_skip_reason(self) -> str | None:
        if not self.enabled:
            return "AMADEUS_ENABLED=false"
        if not self.client_id or not self.client_secret:
            return "Amadeus client credentials are missing"
        if not self.base_url:
            return "AMADEUS_BASE_URL is empty"
        return None

    def access_token(self) -> str:
        now = time.time()
        if self._access_token and now < self._token_expires_at - 30:
            return self._access_token
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.post(f"{self.base_url.rstrip('/')}/v1/security/oauth2/token", data=payload)
            response.raise_for_status()
            raw = response.json()
        self._access_token = raw["access_token"]
        self._token_expires_at = now + int(raw.get("expires_in", 0))
        return self._access_token

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        token = self.access_token()
        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.get(f"{self.base_url.rstrip('/')}{path}", headers=headers, params=params)
            response.raise_for_status()
            return response.json()

    def flight_offers_search(self, query: dict[str, Any]) -> dict[str, Any]:
        skip_reason = self.configured_skip_reason()
        if skip_reason:
            return _skip("flight", skip_reason)
        origin = resolve_iata_code(query.get("origin"))
        destination = resolve_iata_code(query.get("destination"))
        departure_date = query.get("start_date")
        if not origin:
            return _skip("flight", f"Could not resolve origin airport/city code from {query.get('origin')!r}")
        if not destination:
            return _skip("flight", f"Could not resolve destination airport/city code from {query.get('destination')!r}")
        if not departure_date:
            return _skip("flight", "Flight search requires a start_date")

        params = {
            "originLocationCode": origin,
            "destinationLocationCode": destination,
            "departureDate": departure_date,
            "adults": max(1, int(query.get("number_of_travelers") or 1)),
            "currencyCode": "USD",
            "max": 5,
        }
        if query.get("end_date"):
            params["returnDate"] = query["end_date"]
        try:
            raw = self._get("/v2/shopping/flight-offers", params)
        except Exception as exc:
            return _error("flight", str(exc))
        return {
            "status": "completed",
            "normalized_result": normalize_flight_offers(raw, origin, destination, departure_date, query.get("end_date")),
            "raw_result": raw,
            "error_message": None,
        }

    def hotel_list_search(self, query: dict[str, Any]) -> dict[str, Any]:
        skip_reason = self.configured_skip_reason()
        if skip_reason:
            return _skip("hotel", skip_reason)
        city_code = resolve_iata_code(query.get("destination"))
        if not city_code:
            return _skip("hotel", f"Could not resolve hotel city code from {query.get('destination')!r}")
        try:
            raw = self._get("/v1/reference-data/locations/hotels/by-city", {"cityCode": city_code, "radius": 20})
        except Exception as exc:
            return _error("hotel", str(exc))
        return {
            "status": "completed",
            "normalized_result": normalize_hotel_list(raw, query.get("start_date"), query.get("end_date")),
            "raw_result": raw,
            "error_message": None,
        }

    def hotel_offers_lookup(self, hotel_ids: list[str], query: dict[str, Any]) -> dict[str, Any]:
        skip_reason = self.configured_skip_reason()
        if skip_reason:
            return _skip("hotel", skip_reason)
        if not hotel_ids:
            return _skip("hotel", "No Amadeus hotel IDs available for offer lookup")
        if not query.get("start_date") or not query.get("end_date"):
            return _skip("hotel", "Hotel offers require check_in and check_out dates")
        params = {
            "hotelIds": ",".join(hotel_ids[:10]),
            "checkInDate": query["start_date"],
            "checkOutDate": query["end_date"],
            "adults": max(1, int(query.get("number_of_travelers") or 1)),
            "currency": "USD",
            "bestRateOnly": "true",
        }
        try:
            raw = self._get("/v3/shopping/hotel-offers", params)
        except Exception as exc:
            return _error("hotel", str(exc))
        return {
            "status": "completed",
            "normalized_result": normalize_hotel_offers(raw, query.get("start_date"), query.get("end_date")),
            "raw_result": raw,
            "error_message": None,
        }


def normalize_flight_offers(
    raw: dict[str, Any],
    origin: str | None = None,
    destination: str | None = None,
    departure_date: str | None = None,
    return_date: str | None = None,
) -> dict[str, Any]:
    offers = []
    carrier_names = raw.get("dictionaries", {}).get("carriers", {})
    for offer in raw.get("data", []):
        itineraries = offer.get("itineraries", [])
        carriers: list[str] = []
        flight_numbers: list[str] = []
        summaries: list[str] = []
        for itinerary in itineraries:
            segments = itinerary.get("segments", [])
            segment_labels = []
            for segment in segments:
                carrier = segment.get("carrierCode")
                if carrier and carrier not in carriers:
                    carriers.append(carrier)
                number = segment.get("number")
                if carrier and number:
                    flight_number = f"{carrier} {number}"
                    if flight_number not in flight_numbers:
                        flight_numbers.append(flight_number)
                dep = segment.get("departure", {})
                arr = segment.get("arrival", {})
                segment_labels.append(f"{dep.get('iataCode', '')}->{arr.get('iataCode', '')}".strip("->"))
            summaries.append(" / ".join(segment_labels))
        price = offer.get("price", {})
        carrier_code = carriers[0] if carriers else None
        validating_codes = offer.get("validatingAirlineCodes")
        offers.append(
            {
                "source_name": "amadeus",
                "result_type": "flight",
                "total_price": price.get("grandTotal") or price.get("total"),
                "currency": price.get("currency"),
                "origin": origin,
                "destination": destination,
                "departure_date": departure_date,
                "return_date": return_date,
                "carrier_code": carrier_code,
                "airline_name": carrier_names.get(carrier_code) if carrier_code and isinstance(carrier_names, dict) else None,
                "airline_carrier_codes": carriers,
                "validating_airline_codes": validating_codes if isinstance(validating_codes, list) else [],
                "flight_numbers": flight_numbers,
                "itinerary_summary": " | ".join(summary for summary in summaries if summary),
                "raw_offer_reference": offer,
            }
        )
    return {"source_name": "amadeus", "result_type": "flight", "offers": offers}


def normalize_hotel_list(
    raw: dict[str, Any],
    check_in: str | None = None,
    check_out: str | None = None,
) -> dict[str, Any]:
    hotels = []
    for hotel in raw.get("data", []):
        geo = hotel.get("geoCode") or {}
        hotels.append(
            {
                "source_name": "amadeus",
                "result_type": "hotel",
                "hotel_id": hotel.get("hotelId"),
                "hotel_name": hotel.get("name"),
                "chain_code": hotel.get("chainCode"),
                "location": {"latitude": geo.get("latitude"), "longitude": geo.get("longitude")},
                "total_price": None,
                "currency": None,
                "check_in": check_in,
                "check_out": check_out,
                "room_offer_summary": None,
                "raw_hotel_reference": hotel,
            }
        )
    return {"source_name": "amadeus", "result_type": "hotel", "hotels": hotels}


def normalize_hotel_offers(
    raw: dict[str, Any],
    check_in: str | None = None,
    check_out: str | None = None,
) -> dict[str, Any]:
    hotels = []
    for item in raw.get("data", []):
        hotel = item.get("hotel", {})
        offers = item.get("offers", [])
        first_offer = offers[0] if offers else {}
        price = first_offer.get("price", {})
        room = first_offer.get("room", {})
        hotels.append(
            {
                "source_name": "amadeus",
                "result_type": "hotel",
                "hotel_id": hotel.get("hotelId"),
                "hotel_name": hotel.get("name"),
                "chain_code": hotel.get("chainCode"),
                "location": {
                    "latitude": (hotel.get("geoCode") or {}).get("latitude"),
                    "longitude": (hotel.get("geoCode") or {}).get("longitude"),
                },
                "total_price": price.get("total"),
                "currency": price.get("currency"),
                "check_in": check_in or first_offer.get("checkInDate"),
                "check_out": check_out or first_offer.get("checkOutDate"),
                "room_offer_summary": room.get("description", {}).get("text") or room.get("typeEstimated", {}).get("category"),
                "source_url": first_offer.get("self") or first_offer.get("url"),
                "raw_hotel_reference": hotel,
                "raw_offer_reference": first_offer,
            }
        )
    return {"source_name": "amadeus", "result_type": "hotel", "hotels": hotels}
