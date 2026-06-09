from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Any

from app.services.source_config import resolve_airport


SOURCE_NAME = "trvl"
DEFAULT_BINARY_PATH = ".tools/trvl/trvl"
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_CURRENCY = "USD"
MAX_EXCERPT_CHARS = 1800
MAX_WARNING_LINES = 20


def _skip(reason: str, result_type: str, query_json: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": "skipped",
        "normalized_result": {
            "source_name": SOURCE_NAME,
            "result_type": result_type,
            "offers": [] if result_type == "flight" else None,
            "hotels": [] if result_type == "hotel" else None,
            "reason": reason,
            "status_reason": "missing_dependency" if "binary was not found" in reason else "disabled",
            "query": query_json or {},
        },
        "raw_result": {},
        "error_message": reason,
    }


def _error(message: str, result_type: str, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    concise = _concise_text(message)
    return {
        "status": "error",
        "normalized_result": {
            "source_name": SOURCE_NAME,
            "result_type": result_type,
            "offers": [] if result_type == "flight" else None,
            "hotels": [] if result_type == "hotel" else None,
            "reason": concise,
        },
        "raw_result": raw or {"diagnostic_error_excerpt": concise},
        "error_message": concise,
    }


def _concise_text(value: str, limit: int = 1200) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}... [truncated]"


def _bounded_string(value: str, limit: int = MAX_EXCERPT_CHARS) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit].rstrip()}... [truncated]"


def _bounded_public_data(value: Any, *, max_depth: int = 4, max_items: int = 20) -> Any:
    if max_depth < 0:
        return _bounded_string(repr(value), 500)
    if isinstance(value, str):
        return _bounded_string(value, 500)
    if isinstance(value, (int, float, bool, type(None))):
        return value
    if isinstance(value, list):
        return [_bounded_public_data(item, max_depth=max_depth - 1, max_items=max_items) for item in value[:max_items]]
    if isinstance(value, dict):
        return {
            str(key): _bounded_public_data(item, max_depth=max_depth - 1, max_items=max_items)
            for key, item in list(value.items())[:max_items]
            if not str(key).startswith("_")
        }
    return _bounded_string(repr(value), 500)


def _stderr_warnings(stderr: str) -> list[str]:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return [_bounded_string(line, 500) for line in lines[:MAX_WARNING_LINES]]


def _load_json_text(text: str) -> Any | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def resolve_trvl_binary(configured_path: str | None = None) -> str | None:
    candidates: list[str] = []
    if configured_path:
        candidates.append(configured_path)
    candidates.append(DEFAULT_BINARY_PATH)
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        path = Path(candidate)
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    return shutil.which("trvl")


def _first_string(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _parse_price(value: Any) -> tuple[float | None, str | None]:
    if value in (None, ""):
        return None, None
    if isinstance(value, dict):
        currency = _first_string(value, ("currency", "currency_code", "currencyCode"))
        for key in ("amount", "value", "price", "total", "nightly", "raw"):
            price, nested_currency = _parse_price(value.get(key))
            if price is not None:
                return price, (currency or nested_currency)
        return None, currency
    if isinstance(value, (int, float)):
        return float(value), None
    text = str(value).strip()
    currency = None
    if "$" in text or "US$" in text.upper() or "USD" in text.upper():
        currency = "USD"
    import re

    match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", text)
    if not match:
        return None, currency
    try:
        return float(match.group(0).replace(",", "")), currency
    except ValueError:
        return None, currency


def _price_and_currency(payload: dict[str, Any], price_keys: tuple[str, ...]) -> tuple[float | None, str | None]:
    explicit_currency = _first_string(payload, ("currency", "currency_code", "currencyCode"))
    for key in price_keys:
        price, currency = _parse_price(payload.get(key))
        if price is not None:
            return price, (explicit_currency or currency)
    return None, explicit_currency


def _iter_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for item in value.values():
            if isinstance(item, (dict, list)):
                found.extend(_iter_dicts(item))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                found.extend(_iter_dicts(item))
    return found


def _candidate_items(raw: Any, preferred_keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        for key in preferred_keys:
            value = raw.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = _candidate_items(value, preferred_keys)
                if nested:
                    return nested
        data = raw.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            nested = _candidate_items(data, preferred_keys)
            if nested:
                return nested
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _first_leg_airline(flight: dict[str, Any]) -> str | None:
    for key in ("legs", "segments", "flights"):
        value = flight.get(key)
        if isinstance(value, list):
            for leg in value:
                if isinstance(leg, dict):
                    airline = _first_string(
                        leg,
                        (
                            "airline",
                            "airline_name",
                            "carrier",
                            "carrier_name",
                            "marketing_airline",
                            "marketingCarrier",
                            "operating_airline",
                        ),
                    )
                    if airline:
                        return airline
        elif isinstance(value, dict):
            airline = _first_leg_airline(value)
            if airline:
                return airline
    for itinerary_key in ("itinerary", "itineraries"):
        value = flight.get(itinerary_key)
        for nested in _iter_dicts(value):
            airline = _first_string(nested, ("airline", "airline_name", "carrier", "carrier_name"))
            if airline:
                return airline
    return None


def _flight_provider(flight: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    trvl_provider = _first_string(flight, ("provider", "source_provider", "source"))
    cheapest_source = _first_string(flight, ("cheapest_source", "cheapestSource"))
    provider = _first_leg_airline(flight) or trvl_provider or cheapest_source
    return provider, trvl_provider, cheapest_source


def _flight_number_signature(flight: dict[str, Any]) -> str | None:
    explicit = flight.get("flight_numbers") or flight.get("flight_number") or flight.get("flightNumber")
    if isinstance(explicit, list):
        labels = [str(item) for item in explicit if item]
        return ",".join(labels) if labels else None
    if explicit:
        return str(explicit)
    parts: list[str] = []
    for key in ("legs", "segments", "flights"):
        value = flight.get(key)
        if isinstance(value, list):
            for leg in value:
                if not isinstance(leg, dict):
                    continue
                label = _first_string(leg, ("flight_number", "flightNumber", "number"))
                airline = _first_string(leg, ("airline_code", "carrier_code", "carrierCode", "carrier"))
                if label and airline and not str(label).upper().startswith(str(airline).upper()):
                    parts.append(f"{airline} {label}")
                elif label:
                    parts.append(label)
    return ",".join(dict.fromkeys(parts)) if parts else None


def _flight_times(flight: dict[str, Any]) -> tuple[str | None, str | None]:
    departure = _first_string(flight, ("departure_time", "departure", "depart", "depart_at", "departureTime"))
    arrival = _first_string(flight, ("arrival_time", "arrival", "arrive_at", "arrivalTime"))
    if departure and arrival:
        return departure, arrival
    for key in ("legs", "segments", "flights"):
        value = flight.get(key)
        if isinstance(value, list) and value:
            first = value[0] if isinstance(value[0], dict) else {}
            last = value[-1] if isinstance(value[-1], dict) else {}
            departure = departure or _first_string(first, ("departure_time", "departure", "depart", "departureTime"))
            arrival = arrival or _first_string(last, ("arrival_time", "arrival", "arrivalTime"))
    return departure, arrival


def _source_url(payload: dict[str, Any]) -> str | None:
    value = _first_string(payload, ("booking_url", "bookingUrl", "source_url", "url", "link", "deep_link"))
    if value and value.startswith(("http://", "https://")):
        return value
    return None


def _success(raw: Any) -> bool:
    if isinstance(raw, dict) and raw.get("success") is True:
        return True
    return False


def _flight_dedupe_key(offer: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        str(offer.get("provider") or ""),
        str(offer.get("total_price") or ""),
        str(offer.get("departure") or ""),
        str(offer.get("arrival") or ""),
        str(offer.get("stops") or ""),
        str(offer.get("flight_signature") or ""),
    )


def _sort_dedupe_limit(offers: list[dict[str, Any]], max_results: int) -> list[dict[str, Any]]:
    sorted_offers = sorted(offers, key=lambda item: (float(item.get("total_price") or 0), str(item.get("provider") or "")))
    seen: set[tuple[str, str, str, str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for offer in sorted_offers:
        key = _flight_dedupe_key(offer)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(offer)
        if len(deduped) >= max_results:
            break
    return deduped


def _hotel_dedupe_key(hotel: dict[str, Any]) -> tuple[str, str, str, str, str]:
    hotel_id = str(hotel.get("hotel_id") or "")
    if hotel_id:
        return ("id", hotel_id, "", "", "")
    return (
        "fields",
        str(hotel.get("hotel_name") or ""),
        str(hotel.get("nightly_price") or hotel.get("total_price") or ""),
        str(hotel.get("currency") or ""),
        str(hotel.get("source_url") or ""),
    )


def _nights(checkin: str | None, checkout: str | None) -> int | None:
    if not checkin or not checkout:
        return None
    try:
        return max(1, (date.fromisoformat(checkout) - date.fromisoformat(checkin)).days)
    except ValueError:
        return None


def build_flight_query(
    query: dict[str, Any],
    *,
    currency: str,
    preferred_airports: list | None = None,
    alternate_airports: list | None = None,
) -> dict[str, Any]:
    origin_raw = query.get("origin")
    destination_raw = query.get("destination")
    travelers = int(query.get("number_of_travelers") or 1)
    resolved_origin = resolve_airport(str(origin_raw), preferred_airports, alternate_airports) if origin_raw else None
    resolved_destination = resolve_airport(str(destination_raw), preferred_airports, alternate_airports) if destination_raw else None
    return {
        "origin_value": origin_raw,
        "destination_value": destination_raw,
        "origin_airport": resolved_origin,
        "destination_airport": resolved_destination,
        "departure_date": query.get("start_date"),
        "return_date": query.get("end_date"),
        "travelers_requested": travelers,
        "adults_arg": max(1, travelers),
        "currency": currency,
    }


def build_hotel_query(query: dict[str, Any], *, currency: str) -> dict[str, Any]:
    travelers = int(query.get("number_of_travelers") or 1)
    children = int(query.get("children") or 0)
    return {
        "destination_value": query.get("destination"),
        "checkin": query.get("start_date"),
        "checkout": query.get("end_date"),
        "guests": max(1, travelers),
        "children": max(0, children),
        "currency": currency,
    }


def normalize_flights(raw: Any, query_json: dict[str, Any], *, max_results: int, stderr: str = "", command_metadata: dict[str, Any] | None = None, traveler_pricing_note: str | None = None) -> dict[str, Any]:
    flights = _candidate_items(raw, ("flights", "offers", "results", "itineraries"))
    offers: list[dict[str, Any]] = []
    skipped_count = 0
    for flight in flights:
        price, currency = _price_and_currency(
            flight,
            ("total_price", "totalPrice", "price", "amount", "fare", "cost", "extracted_price", "cheapest_price"),
        )
        provider, trvl_provider, cheapest_source = _flight_provider(flight)
        if price is None or not currency or not provider:
            skipped_count += 1
            continue
        departure, arrival = _flight_times(flight)
        source_url = _source_url(flight)
        flight_signature = _flight_number_signature(flight)
        label = " ".join(
            part
            for part in [
                provider,
                str(query_json.get("origin_airport") or query_json.get("origin_value") or ""),
                "to",
                str(query_json.get("destination_airport") or query_json.get("destination_value") or ""),
            ]
            if part
        )
        offer = {
            "component_type": "flight",
            "component_type_label": "Airfare",
            "source_name": SOURCE_NAME,
            "result_type": "flight",
            "provider": provider,
            "airline_name": provider,
            "label": label,
            "total_price": price,
            "currency": currency,
            "origin": query_json.get("origin_airport") or query_json.get("origin_value"),
            "destination": query_json.get("destination_airport") or query_json.get("destination_value"),
            "departure": departure,
            "arrival": arrival,
            "departure_date": query_json.get("departure_date"),
            "return_date": query_json.get("return_date"),
            "duration": flight.get("duration"),
            "stops": flight.get("stops"),
            "flight_numbers": [flight_signature] if flight_signature else None,
            "flight_signature": flight_signature,
            "trvl_provider": trvl_provider,
            "cheapest_source": cheapest_source,
            "source_url": source_url,
            "link_type": "exact_source" if source_url else "none",
            "link_label": "View source price" if source_url else None,
            "mock": False,
            "raw_offer_reference": _bounded_public_data(flight, max_depth=3, max_items=12),
        }
        if traveler_pricing_note:
            offer["traveler_pricing_note"] = traveler_pricing_note
        offers.append(offer)
    bounded = _sort_dedupe_limit(offers, max_results=max_results)
    return {
        "source_name": SOURCE_NAME,
        "result_type": "flight",
        "offers": bounded,
        "raw_count": len(flights),
        "normalized_count": len(bounded),
        "skipped_count": skipped_count + max(0, len(offers) - len(bounded)),
        "provider_statuses": raw.get("provider_statuses") if isinstance(raw, dict) else None,
        "stderr_warnings": _stderr_warnings(stderr),
        "command": _bounded_public_data(command_metadata or {}, max_depth=3, max_items=20),
        "query": query_json,
    }


def normalize_hotels(raw: Any, query_json: dict[str, Any], *, max_results: int, stderr: str = "", command_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    raw_hotels = _candidate_items(raw, ("hotels", "properties", "results", "offers"))
    nights = _nights(str(query_json.get("checkin") or ""), str(query_json.get("checkout") or "")) or 1
    hotels: list[dict[str, Any]] = []
    skipped_count = 0
    for hotel in raw_hotels:
        name = _first_string(hotel, ("name", "hotel_name", "hotelName", "title", "display_name"))
        nightly_price, currency = _price_and_currency(
            hotel,
            ("nightly_price", "nightlyPrice", "price_per_night", "pricePerNight", "price", "rate", "amount"),
        )
        if not name or nightly_price is None or not currency:
            skipped_count += 1
            continue
        source_url = _source_url(hotel)
        sources = hotel.get("sources")
        if isinstance(sources, list):
            bounded_sources = [
                {
                    "provider": _first_string(source, ("provider", "source", "name")) if isinstance(source, dict) else None,
                    "price": _price_and_currency(source, ("price", "amount", "rate"))[0] if isinstance(source, dict) else None,
                    "currency": _price_and_currency(source, ("price", "amount", "rate"))[1] if isinstance(source, dict) else None,
                    "url": _source_url(source) if isinstance(source, dict) else None,
                }
                for source in sources[:10]
                if isinstance(source, dict)
            ]
        else:
            bounded_sources = []
        hotels.append(
            {
                "component_type": "hotel",
                "component_type_label": "Hotel",
                "source_name": SOURCE_NAME,
                "result_type": "hotel",
                "hotel_id": _first_string(hotel, ("hotel_id", "hotelId", "id")),
                "hotel_name": name,
                "provider": name,
                "label": name,
                "nightly_price": nightly_price,
                "nights": nights,
                "total_price": round(nightly_price * nights, 2),
                "price_basis": "nightly",
                "currency": currency,
                "check_in": query_json.get("checkin"),
                "check_out": query_json.get("checkout"),
                "rating": hotel.get("rating"),
                "source_url": source_url,
                "link_type": "exact_source" if source_url else "none",
                "link_label": "View source price" if source_url else None,
                "cheapest_source": _first_string(hotel, ("cheapest_source", "cheapestSource", "provider", "source")),
                "sources": bounded_sources,
                "raw_price": hotel.get("price"),
                "mock": False,
                "raw_offer_reference": _bounded_public_data(hotel, max_depth=3, max_items=12),
            }
        )
    hotels = sorted(hotels, key=lambda item: (float(item.get("total_price") or item.get("nightly_price") or 0), str(item.get("hotel_name") or "")))
    seen: set[tuple[str, str, str, str, str]] = set()
    bounded: list[dict[str, Any]] = []
    for hotel in hotels:
        key = _hotel_dedupe_key(hotel)
        if key in seen:
            continue
        seen.add(key)
        bounded.append(hotel)
        if len(bounded) >= max_results:
            break
    return {
        "source_name": SOURCE_NAME,
        "result_type": "hotel",
        "hotels": bounded,
        "offers": bounded,
        "raw_count": len(raw_hotels),
        "normalized_count": len(bounded),
        "skipped_count": skipped_count + max(0, len(hotels) - len(bounded)),
        "total_available": raw.get("total_available") if isinstance(raw, dict) else None,
        "stderr_warnings": _stderr_warnings(stderr),
        "command": _bounded_public_data(command_metadata or {}, max_depth=3, max_items=20),
        "query": query_json,
    }


def _raw_summary(raw: Any, stdout: str, stderr: str, exit_code: int, elapsed_seconds: float) -> dict[str, Any]:
    summary = {
        "success": raw.get("success") if isinstance(raw, dict) else None,
        "top_level_keys": sorted(str(key) for key in raw.keys())[:30] if isinstance(raw, dict) else [],
        "stdout_json_available": raw is not None,
        "stdout_excerpt": _bounded_string(stdout),
        "stderr_warnings": _stderr_warnings(stderr),
        "stderr_excerpt": _bounded_string(stderr),
        "exit_code": exit_code,
        "elapsed_seconds": round(elapsed_seconds, 3),
    }
    return summary


def _run_trvl(command: list[str], timeout_seconds: float) -> tuple[subprocess.CompletedProcess[str], float]:
    start = time.monotonic()
    result = subprocess.run(command, text=True, capture_output=True, timeout=timeout_seconds, check=False)
    return result, time.monotonic() - start


def search_trvl_flights(
    query: dict[str, Any],
    *,
    enabled: bool,
    binary_path: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_results: int = 20,
    currency: str = DEFAULT_CURRENCY,
    preferred_airports: list | None = None,
    alternate_airports: list | None = None,
) -> dict[str, Any]:
    query_json = build_flight_query(query, currency=currency, preferred_airports=preferred_airports, alternate_airports=alternate_airports)
    if not enabled:
        return _skip("TRVL_ENABLED=false", "flight", query_json)
    binary = resolve_trvl_binary(binary_path)
    if not binary:
        return _skip("TRVL_ENABLED=true but trvl binary was not found", "flight", query_json)
    if not query_json.get("origin_airport") or not query_json.get("destination_airport") or not query_json.get("departure_date"):
        return _skip("trvl flights requires resolved origin_airport, destination_airport, and departure_date", "flight", query_json)
    command = [
        binary,
        "flights",
        str(query_json["origin_airport"]),
        str(query_json["destination_airport"]),
        str(query_json["departure_date"]),
    ]
    if query_json.get("return_date"):
        command.extend(["--return", str(query_json["return_date"])])
    command.extend(["--adults", str(query_json["adults_arg"]), "--currency", currency, "--format", "json"])
    try:
        result, elapsed = _run_trvl(command, timeout_seconds)
    except Exception as exc:
        return _error(str(exc), "flight")
    raw = _load_json_text(result.stdout)
    raw_result = _raw_summary(raw, result.stdout, result.stderr, result.returncode, elapsed)
    if result.returncode != 0 and not _success(raw):
        return _error(f"trvl flights exited with code {result.returncode}", "flight", raw_result)
    traveler_pricing_note = None
    if int(query_json["travelers_requested"] or 1) > 1 and int(query.get("children") or 0) > 0:
        traveler_pricing_note = "trvl flight search priced all travelers as adults because trvl CLI only exposes --adults"
    normalized = normalize_flights(
        raw,
        query_json,
        max_results=max_results,
        stderr=result.stderr,
        command_metadata={"argv": command, "exit_code": result.returncode, "elapsed_seconds": round(elapsed, 3)},
        traveler_pricing_note=traveler_pricing_note,
    )
    return {"status": "completed", "normalized_result": normalized, "raw_result": raw_result, "error_message": None}


def search_trvl_hotels(
    query: dict[str, Any],
    *,
    enabled: bool,
    binary_path: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_results: int = 20,
    currency: str = DEFAULT_CURRENCY,
) -> dict[str, Any]:
    query_json = build_hotel_query(query, currency=currency)
    if not enabled:
        return _skip("TRVL_ENABLED=false", "hotel", query_json)
    binary = resolve_trvl_binary(binary_path)
    if not binary:
        return _skip("TRVL_ENABLED=true but trvl binary was not found", "hotel", query_json)
    if not query_json.get("destination_value") or not query_json.get("checkin") or not query_json.get("checkout"):
        return _skip("trvl hotels requires destination, checkin, and checkout", "hotel", query_json)
    command = [
        binary,
        "hotels",
        str(query_json["destination_value"]),
        "--checkin",
        str(query_json["checkin"]),
        "--checkout",
        str(query_json["checkout"]),
        "--guests",
        str(query_json["guests"]),
        "--children",
        str(query_json["children"]),
        "--currency",
        currency,
        "--format",
        "json",
    ]
    try:
        result, elapsed = _run_trvl(command, timeout_seconds)
    except Exception as exc:
        return _error(str(exc), "hotel")
    raw = _load_json_text(result.stdout)
    raw_result = _raw_summary(raw, result.stdout, result.stderr, result.returncode, elapsed)
    if result.returncode != 0 and not _success(raw):
        return _error(f"trvl hotels exited with code {result.returncode}", "hotel", raw_result)
    normalized = normalize_hotels(
        raw,
        query_json,
        max_results=max_results,
        stderr=result.stderr,
        command_metadata={"argv": command, "exit_code": result.returncode, "elapsed_seconds": round(elapsed, 3)},
    )
    return {"status": "completed", "normalized_result": normalized, "raw_result": raw_result, "error_message": None}
