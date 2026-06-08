from __future__ import annotations

import json
from datetime import date
from typing import Any
from urllib.parse import quote_plus

from app.db.models import PriceSnapshot, SourceResult, Vacation, utc_now
from app.services.search_planner import deterministic_json


PRICED_STATUSES = {"completed", "mock"}
QUOTE_TYPES = {"flight", "hotel", "rental_car", "package"}
QUOTE_TYPE_LABELS = {
    "flight": "Airfare",
    "hotel": "Hotel",
    "rental_car": "Rental car",
    "package": "Package",
}


def _load_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _float_price(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date_span_days(start: date | None, end: date | None, fallback: int | None = None) -> int | None:
    if start and end:
        return max(1, (end - start).days)
    if fallback:
        return max(1, int(fallback))
    return None


def _first_string(payload: dict[str, Any], keys: list[str]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _carrier_label(payload: dict[str, Any]) -> str | None:
    value = payload.get("airline_carrier_codes") or payload.get("carrier_codes")
    if isinstance(value, list):
        labels = [str(item) for item in value if item]
        if labels:
            return ", ".join(labels)
    return _first_string(payload, ["airline_carrier_code", "carrier_code", "carrier", "airline"])


def _provider_code(payload: dict[str, Any], quote_type: str) -> str | None:
    if quote_type == "flight":
        code = _first_string(payload, ["carrier_code", "airline_carrier_code", "carrier"])
        if code:
            return code
        value = payload.get("airline_carrier_codes") or payload.get("carrier_codes")
        if isinstance(value, list):
            for item in value:
                if item:
                    return str(item)
    if quote_type == "hotel":
        return _first_string(payload, ["chain_code", "chainCode"])
    return None


def _source_url(payload: dict[str, Any]) -> str | None:
    return _first_string(payload, ["source_url", "booking_url", "deep_link"])


def _search_reference_url(payload: dict[str, Any], quote_type: str, vacation: Vacation, provider: str | None) -> str | None:
    explicit = _first_string(payload, ["search_reference_url", "google_maps_uri", "website_uri"])
    if explicit:
        return explicit
    if _source_url(payload):
        return None
    destination = vacation.destination or _first_string(payload, ["destination"]) or ""
    start = vacation.start_date.isoformat() if vacation.start_date else str(payload.get("departure_date") or payload.get("check_in") or payload.get("pickup_date") or "")
    end = vacation.end_date.isoformat() if vacation.end_date else str(payload.get("return_date") or payload.get("check_out") or payload.get("dropoff_date") or "")
    if quote_type == "flight":
        origin = vacation.origin or str(payload.get("origin") or "")
        query = " ".join(part for part in [provider or "", "flight", origin, destination, start, end] if part)
    elif quote_type == "hotel":
        hotel = _first_string(payload, ["hotel_name", "display_name", "name"]) or provider or ""
        query = " ".join(part for part in [hotel, destination, start, end] if part)
    elif quote_type == "rental_car":
        company = _first_string(payload, ["rental_company", "company", "company_name"]) or provider or ""
        query = " ".join(part for part in [company, "rental car", destination, start, end] if part)
    else:
        query = " ".join(part for part in [provider or "", destination, start, end] if part)
    return f"https://www.google.com/search?q={quote_plus(query)}" if query else None


def _link_fields(payload: dict[str, Any], quote_type: str, vacation: Vacation, provider: str | None) -> tuple[str | None, str | None, str, str | None]:
    source_url = _source_url(payload)
    if source_url:
        return source_url, None, "exact_source", "View source price"
    reference_url = _search_reference_url(payload, quote_type, vacation, provider)
    if reference_url:
        return None, reference_url, "search_reference", "Search reference"
    return None, None, "none", None


def _provider(payload: dict[str, Any], source_name: str) -> str | None:
    result_type = payload.get("result_type")
    if result_type == "flight":
        provider = _first_string(payload, ["airline_name", "provider", "airline"]) or _carrier_label(payload)
    elif result_type == "hotel":
        provider = _first_string(payload, ["hotel_name", "provider", "chain_name", "brand", "display_name", "source_name"])
    elif result_type == "rental_car":
        provider = _first_string(payload, ["rental_company", "company", "company_name", "provider", "source_name"])
    else:
        provider = _first_string(payload, ["provider", "source_name"])
    return provider or source_name or "Unknown provider"


def _label(payload: dict[str, Any], quote_type: str) -> str:
    flight_numbers = payload.get("flight_numbers")
    if quote_type == "flight" and isinstance(flight_numbers, list) and flight_numbers:
        route = _first_string(payload, ["itinerary_summary"])
        return f"{', '.join(str(number) for number in flight_numbers if number)} {route or ''}".strip()
    if quote_type == "rental_car":
        vehicle = _first_string(payload, ["vehicle_class", "vehicle_label", "car_class", "label"])
        if vehicle:
            return vehicle
    label = _first_string(
        payload,
        ["label", "title", "name", "hotel_name", "itinerary_summary", "room_offer_summary", "provider"],
    )
    return label or quote_type.replace("_", " ").title()


def _flight_numbers_from_offer(offer: dict[str, Any]) -> list[str]:
    flight_numbers: list[str] = []
    for itinerary in offer.get("itineraries", []):
        if not isinstance(itinerary, dict):
            continue
        for segment in itinerary.get("segments", []):
            if not isinstance(segment, dict):
                continue
            carrier = segment.get("carrierCode")
            number = segment.get("number")
            if carrier and number:
                flight_number = f"{carrier} {number}"
                if flight_number not in flight_numbers:
                    flight_numbers.append(flight_number)
    return flight_numbers


def _enriched_payload(payload: dict[str, Any], quote_type: str, raw_result: dict[str, Any]) -> dict[str, Any]:
    if quote_type != "flight":
        return payload
    enriched = dict(payload)
    raw_offer = payload.get("raw_offer_reference")
    raw_offer = raw_offer if isinstance(raw_offer, dict) else {}
    carrier_code = _provider_code(enriched, "flight")
    if carrier_code and not enriched.get("carrier_code"):
        enriched["carrier_code"] = carrier_code
    carrier_names = raw_result.get("dictionaries", {}).get("carriers", {})
    if carrier_code and isinstance(carrier_names, dict) and not enriched.get("airline_name"):
        enriched["airline_name"] = carrier_names.get(carrier_code)
    validating_codes = raw_offer.get("validatingAirlineCodes")
    if isinstance(validating_codes, list) and not enriched.get("validating_airline_codes"):
        enriched["validating_airline_codes"] = validating_codes
    if raw_offer and not enriched.get("flight_numbers"):
        enriched["flight_numbers"] = _flight_numbers_from_offer(raw_offer)
    return enriched


def _priced_payloads(normalized: dict[str, Any], source_result: SourceResult) -> list[dict[str, Any]]:
    result_type = normalized.get("result_type") or source_result.result_type
    if result_type == "flight":
        offers = normalized.get("offers")
        return offers if isinstance(offers, list) else [normalized]
    if result_type == "hotel":
        hotels = normalized.get("hotels")
        return hotels if isinstance(hotels, list) else [normalized]
    if result_type == "rental_car":
        cars = normalized.get("cars") or normalized.get("offers")
        return cars if isinstance(cars, list) else [normalized]
    if result_type == "package":
        packages = normalized.get("packages") or normalized.get("offers")
        return packages if isinstance(packages, list) else [normalized]
    return []


def _total_price(payload: dict[str, Any], quote_type: str, vacation: Vacation) -> float | None:
    explicit = _float_price(payload.get("total_price"))
    if explicit is not None:
        return explicit
    if quote_type == "hotel":
        nightly = _float_price(payload.get("nightly_price"))
        nights = _date_span_days(vacation.start_date, vacation.end_date, vacation.nights_target)
        if nightly is not None and nights is not None:
            return nightly * nights
    if quote_type == "rental_car":
        daily = _float_price(payload.get("daily_price"))
        days = _date_span_days(vacation.start_date, vacation.end_date, vacation.nights_target)
        if daily is not None and days is not None:
            return daily * days
    return None


def snapshots_from_source_result(vacation: Vacation, source_result: SourceResult) -> list[PriceSnapshot]:
    if source_result.status not in PRICED_STATUSES:
        return []
    normalized = _load_json(source_result.normalized_result_json)
    raw_result = _load_json(source_result.raw_result_json)
    result_type = normalized.get("result_type") or source_result.result_type
    if result_type not in QUOTE_TYPES:
        return []

    snapshots: list[PriceSnapshot] = []
    captured_at = source_result.created_at or utc_now()
    for payload in _priced_payloads(normalized, source_result):
        if not isinstance(payload, dict):
            continue
        quote_type = str(payload.get("result_type") or result_type)
        if quote_type not in QUOTE_TYPES:
            continue
        payload = _enriched_payload(payload, quote_type, raw_result)
        total = _total_price(payload, quote_type, vacation)
        if total is None:
            continue
        currency = str(payload.get("currency") or normalized.get("currency") or "USD")
        provider = _provider({**payload, "result_type": quote_type}, source_result.source_name)
        provider_code = _provider_code(payload, quote_type)
        source_url, search_reference_url, link_type, link_label = _link_fields(payload, quote_type, vacation, provider)
        source_payload = {
            "airline_name": _first_string(payload, ["airline_name"]) if quote_type == "flight" else None,
            "captured_at": captured_at.isoformat() if captured_at else None,
            "carrier_code": _first_string(payload, ["carrier_code", "airline_carrier_code", "carrier"]) if quote_type == "flight" else None,
            "chain_code": _first_string(payload, ["chain_code", "chainCode"]) if quote_type == "hotel" else None,
            "component_type": quote_type,
            "component_type_label": QUOTE_TYPE_LABELS.get(quote_type, quote_type.replace("_", " ").title()),
            "currency": currency,
            "flight_numbers": payload.get("flight_numbers") if quote_type == "flight" else None,
            "google_maps_uri": payload.get("google_maps_uri") if quote_type == "hotel" else None,
            "hotel_name": _first_string(payload, ["hotel_name", "display_name"]) if quote_type == "hotel" else None,
            "itinerary_summary": payload.get("itinerary_summary") if quote_type == "flight" else None,
            "label": _label(payload, quote_type),
            "link_label": payload.get("link_label") or link_label,
            "link_type": payload.get("link_type") or link_type,
            "mock": bool(payload.get("mock") or source_result.status == "mock" or source_result.source_name == "mock_travel"),
            "provider": provider,
            "provider_code": provider_code,
            "quote_type": quote_type,
            "rating": payload.get("rating") if quote_type == "hotel" else None,
            "rental_company": _first_string(payload, ["rental_company", "company", "company_name"]) if quote_type == "rental_car" else None,
            "source_result_id": source_result.id,
            "source_name": source_result.source_name,
            "source_status": source_result.status,
            "source_url": source_url,
            "search_reference_url": payload.get("search_reference_url") or search_reference_url,
            "total_price": total,
            "validating_airline_codes": payload.get("validating_airline_codes") if quote_type == "flight" else None,
            "vehicle_label": _first_string(payload, ["vehicle_label", "vehicle_class", "car_class"]) if quote_type == "rental_car" else None,
            "website_uri": payload.get("website_uri") if quote_type == "hotel" else None,
            "quote": payload,
        }
        snapshots.append(
            PriceSnapshot(
                vacation_id=vacation.id,
                search_run_id=source_result.search_run_id,
                source_result_id=source_result.id,
                quote_type=quote_type,
                source_name=source_result.source_name,
                provider=provider,
                label=_label(payload, quote_type),
                total_price=total,
                currency=currency,
                source_url=source_url,
                normalized_json=deterministic_json(source_payload),
                captured_at=captured_at,
            )
        )
    return snapshots
