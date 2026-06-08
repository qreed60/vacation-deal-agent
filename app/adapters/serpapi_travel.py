from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

import httpx


def skipped_result(source_name: str, result_type: str, reason: str) -> dict[str, Any]:
    return {
        "status": "skipped",
        "normalized_result": {"source_name": source_name, "result_type": result_type, "reason": reason},
        "raw_result": {},
        "error_message": reason,
    }


def build_search_reference_url(component_type: str, query: dict[str, Any], provider: str | None = None) -> str:
    destination = str(query.get("destination") or "")
    origin = str(query.get("origin") or "")
    start = str(query.get("start_date") or "")
    end = str(query.get("end_date") or "")
    if component_type == "flight":
        text = " ".join(part for part in [provider or "", "flight", origin, destination, start, end] if part)
    elif component_type == "hotel":
        text = " ".join(part for part in [provider or "", "hotel", destination, start, end] if part)
    else:
        text = " ".join(part for part in [provider or "", component_type, destination, start, end] if part)
    return f"https://www.google.com/search?q={quote_plus(text)}"


def _configured(source_name: str, result_type: str, *, enabled: bool, api_key: str, base_url: str) -> str | None:
    if not enabled:
        return "SERPAPI_ENABLED=false"
    if not api_key:
        return "SerpAPI API key is missing"
    if not base_url:
        return "SERPAPI_BASE_URL is empty"
    return None


def _get(base_url: str, params: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.get(base_url, params=params)
        response.raise_for_status()
        return response.json()


def _first_string(payload: dict[str, Any], keys: list[str]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _price_value(payload: dict[str, Any]) -> Any:
    for key in ("extracted_price", "price", "extracted_lowest", "total_price"):
        value = payload.get(key)
        if value is not None:
            return value
    return None


def _source_url(payload: dict[str, Any]) -> str | None:
    value = _first_string(payload, ["link", "url", "booking_link", "booking_url", "deep_link", "source_url"])
    if value and value.startswith(("http://", "https://")):
        return value
    return None


def _flight_numbers(flight: dict[str, Any]) -> list[str]:
    numbers: list[str] = []
    for segment in flight.get("flights") or []:
        if not isinstance(segment, dict):
            continue
        number = _first_string(segment, ["flight_number", "flight"])
        if number and number not in numbers:
            numbers.append(number)
    return numbers


def _airlines(flight: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for segment in flight.get("flights") or []:
        if not isinstance(segment, dict):
            continue
        airline = _first_string(segment, ["airline"])
        if airline and airline not in names:
            names.append(airline)
    airline = _first_string(flight, ["airline", "provider"])
    if airline and airline not in names:
        names.append(airline)
    return names


def _airport_code(segment: dict[str, Any], key: str) -> str | None:
    airport = segment.get(key)
    if isinstance(airport, dict):
        return _first_string(airport, ["id", "airport_id", "code", "name"])
    return str(airport) if airport else None


def normalize_flights(raw: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    offers: list[dict[str, Any]] = []
    currency = raw.get("search_parameters", {}).get("currency") or raw.get("currency") or "USD"
    raw_flights = []
    for key in ("best_flights", "other_flights"):
        values = raw.get(key)
        if isinstance(values, list):
            raw_flights.extend(values)
    for flight in raw_flights:
        if not isinstance(flight, dict):
            continue
        airlines = _airlines(flight)
        provider = airlines[0] if airlines else None
        source_url = _source_url(flight)
        reference_url = None if source_url else build_search_reference_url("flight", query, provider)
        segments = [segment for segment in flight.get("flights", []) if isinstance(segment, dict)]
        first_segment = segments[0] if segments else {}
        last_segment = segments[-1] if segments else {}
        offers.append(
            {
                "source_name": "serpapi_google_flights",
                "result_type": "flight",
                "provider": provider,
                "airline_name": provider,
                "provider_code": flight.get("airline_code") or flight.get("carrier_code"),
                "carrier_code": flight.get("airline_code") or flight.get("carrier_code"),
                "flight_numbers": _flight_numbers(flight),
                "origin": query.get("origin"),
                "destination": query.get("destination"),
                "departure_airport": _airport_code(first_segment, "departure_airport"),
                "arrival_airport": _airport_code(last_segment, "arrival_airport"),
                "departure_date": query.get("start_date"),
                "return_date": query.get("end_date"),
                "itinerary_summary": " / ".join(
                    label
                    for label in (
                        f"{_airport_code(segment, 'departure_airport') or ''}->{_airport_code(segment, 'arrival_airport') or ''}".strip("->")
                        for segment in segments
                    )
                    if label
                ),
                "total_price": _price_value(flight),
                "currency": flight.get("currency") or currency,
                "source_url": source_url,
                "search_reference_url": reference_url,
                "link_type": "exact_source" if source_url else ("search_reference" if reference_url else "none"),
                "link_label": "View source price" if source_url else ("Search reference" if reference_url else None),
                "booking_token": flight.get("booking_token"),
                "raw_offer_reference": flight,
            }
        )
    return {"source_name": "serpapi_google_flights", "result_type": "flight", "offers": offers}


def normalize_hotels(raw: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    hotels: list[dict[str, Any]] = []
    currency = raw.get("search_parameters", {}).get("currency") or raw.get("currency") or "USD"
    properties = raw.get("properties") if isinstance(raw.get("properties"), list) else []
    for hotel in properties:
        if not isinstance(hotel, dict):
            continue
        rate_per_night = hotel.get("rate_per_night") if isinstance(hotel.get("rate_per_night"), dict) else {}
        total_rate = hotel.get("total_rate") if isinstance(hotel.get("total_rate"), dict) else {}
        price = (
            total_rate.get("extracted_lowest")
            or total_rate.get("extracted_price")
            or rate_per_night.get("extracted_lowest")
            or rate_per_night.get("extracted_price")
            or _price_value(hotel)
        )
        provider = _first_string(hotel, ["name", "title", "provider"])
        source_url = _source_url(hotel)
        reference_url = None if source_url else build_search_reference_url("hotel", query, provider)
        hotels.append(
            {
                "source_name": "serpapi_google_hotels",
                "result_type": "hotel",
                "hotel_name": provider,
                "provider": provider,
                "hotel_id": hotel.get("property_token") or hotel.get("id"),
                "property_token": hotel.get("property_token"),
                "total_price": price,
                "currency": hotel.get("currency") or currency,
                "rating": hotel.get("overall_rating") or hotel.get("rating"),
                "user_rating_count": hotel.get("reviews") or hotel.get("reviews_count"),
                "check_in": query.get("start_date"),
                "check_out": query.get("end_date"),
                "source_url": source_url,
                "search_reference_url": reference_url,
                "link_type": "exact_source" if source_url else ("search_reference" if reference_url else "none"),
                "link_label": "View source price" if source_url else ("Search reference" if reference_url else None),
                "raw_hotel_reference": hotel,
            }
        )
    return {"source_name": "serpapi_google_hotels", "result_type": "hotel", "hotels": hotels}


def search_google_flights(
    query: dict[str, Any],
    *,
    enabled: bool,
    api_key: str,
    base_url: str,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    reason = _configured("serpapi_google_flights", "flight", enabled=enabled, api_key=api_key, base_url=base_url)
    if reason:
        return skipped_result("serpapi_google_flights", "flight", reason)
    params = {
        "engine": "google_flights",
        "departure_id": query.get("origin"),
        "arrival_id": query.get("destination"),
        "outbound_date": query.get("start_date"),
        "return_date": query.get("end_date"),
        "adults": max(1, int(query.get("number_of_travelers") or 1)),
        "currency": "USD",
        "api_key": api_key,
    }
    try:
        raw = _get(base_url, {key: value for key, value in params.items() if value}, timeout_seconds)
    except Exception as exc:
        return {
            "status": "error",
            "normalized_result": {"source_name": "serpapi_google_flights", "result_type": "flight"},
            "raw_result": {},
            "error_message": str(exc),
        }
    return {
        "status": "completed",
        "normalized_result": normalize_flights(raw, query),
        "raw_result": raw,
        "error_message": None,
    }


def search_google_hotels(
    query: dict[str, Any],
    *,
    enabled: bool,
    api_key: str,
    base_url: str,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    reason = _configured("serpapi_google_hotels", "hotel", enabled=enabled, api_key=api_key, base_url=base_url)
    if reason:
        return skipped_result("serpapi_google_hotels", "hotel", reason)
    params = {
        "engine": "google_hotels",
        "q": query.get("destination"),
        "check_in_date": query.get("start_date"),
        "check_out_date": query.get("end_date"),
        "adults": max(1, int(query.get("number_of_travelers") or 1)),
        "currency": "USD",
        "api_key": api_key,
    }
    try:
        raw = _get(base_url, {key: value for key, value in params.items() if value}, timeout_seconds)
    except Exception as exc:
        return {
            "status": "error",
            "normalized_result": {"source_name": "serpapi_google_hotels", "result_type": "hotel"},
            "raw_result": {},
            "error_message": str(exc),
        }
    return {
        "status": "completed",
        "normalized_result": normalize_hotels(raw, query),
        "raw_result": raw,
        "error_message": None,
    }
