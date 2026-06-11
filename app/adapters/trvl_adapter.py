from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Any

from app.services.source_config import resolve_airport_code


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


def _resolution_payload(resolution: Any) -> dict[str, Any]:
    return {
        "input_value": resolution.input_value,
        "resolved_airport_code": resolution.resolved_airport_code,
        "status": resolution.status,
        "source": resolution.source,
        "reason": resolution.reason,
    }


def _traveler_counts(query: dict[str, Any]) -> dict[str, int]:
    travelers = query.get("travelers")
    if isinstance(travelers, list) and travelers:
        adult_count = 0
        child_count = 0
        infant_count = 0
        unknown_age_count = 0
        for traveler in travelers:
            age_value = traveler.get("age") if isinstance(traveler, dict) else None
            try:
                age = int(age_value)
            except (TypeError, ValueError):
                unknown_age_count += 1
                continue
            if age <= 1:
                infant_count += 1
            elif age < 18:
                child_count += 1
            else:
                adult_count += 1
        adult_count += unknown_age_count
        if adult_count < 1:
            adult_count = 1
        return {
            "traveler_count": len(travelers),
            "adult_count": adult_count,
            "child_count": child_count,
            "infant_count": infant_count,
        }
    traveler_count = int(query.get("number_of_travelers") or 1)
    traveler_count = max(1, traveler_count)
    return {
        "traveler_count": traveler_count,
        "adult_count": traveler_count,
        "child_count": int(query.get("children") or 0),
        "infant_count": 0,
    }


def _command_diagnostics(
    *,
    command_label: str,
    command: list[str] | Any,
    result: subprocess.CompletedProcess[str] | None = None,
    raw: Any = None,
    elapsed_seconds: float | None = None,
    exception: Exception | None = None,
    query_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stdout = result.stdout if result is not None else ""
    stderr = result.stderr if result is not None else ""
    exit_code = result.returncode if result is not None else None
    diagnostics = {
        "command_label": command_label,
        "argv": list(command) if isinstance(command, list) else command,
        "exit_code": exit_code,
        "elapsed_seconds": round(float(elapsed_seconds if elapsed_seconds is not None else getattr(result, "elapsed_seconds", 0) if result is not None else 0), 3),
        "stdout_json_success": _success(raw),
        "stdout_json_count": len(_candidate_items(raw, ("flights", "offers", "results", "itineraries"))) if isinstance(raw, dict) else 0,
        "stderr_preview": _bounded_string(stderr, 500),
        "stdout_preview": _bounded_string(stdout, 500),
    }
    if exception is not None:
        diagnostics["exception_type"] = type(exception).__name__
        diagnostics["exception_message"] = _bounded_string(str(exception), 500)
    if query_json:
        diagnostics["resolved_origin_airport"] = query_json.get("origin_airport")
        diagnostics["resolved_destination_airport"] = query_json.get("destination_airport")
        diagnostics["departure_date"] = query_json.get("departure_date")
        diagnostics["return_date"] = query_json.get("return_date")
        diagnostics["trvl_adults_passed"] = query_json.get("trvl_adults_passed")
    return diagnostics


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


def _trvl_provider_failure(stderr: str = "", stdout: str = "", message: str = "") -> dict[str, str] | None:
    combined = "\n".join(part for part in (stderr, stdout, message) if part).lower()
    if not combined:
        return None
    provider_markers = (
        "429",
        "unexpected flight data format",
        "skiplagged",
        "request failed",
    )
    if any(marker in combined for marker in provider_markers):
        return {
            "source_failure_category": "provider_error",
            "provider_failure_reason": "trvl_provider_rate_limited_or_format_error",
        }
    return None


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
    counts = _traveler_counts(query)
    origin_resolution = resolve_airport_code(str(origin_raw)) if origin_raw else resolve_airport_code("")
    destination_resolution = resolve_airport_code(str(destination_raw)) if destination_raw else resolve_airport_code("")
    resolved_origin = origin_resolution.resolved_airport_code
    resolved_destination = destination_resolution.resolved_airport_code
    if preferred_airports:
        first = str(preferred_airports[0]).strip().upper()
        if len(first) == 3:
            resolved_origin = first
    if alternate_airports and not resolved_origin:
        first = str(alternate_airports[0]).strip().upper()
        if len(first) == 3:
            resolved_origin = first
    return {
        "origin_value": origin_raw,
        "destination_value": destination_raw,
        "origin_airport": resolved_origin,
        "destination_airport": resolved_destination,
        "origin_resolution": _resolution_payload(origin_resolution),
        "destination_resolution": _resolution_payload(destination_resolution),
        "origin_resolution_status": origin_resolution.status,
        "destination_resolution_status": destination_resolution.status,
        "origin_resolution_source": origin_resolution.source,
        "destination_resolution_source": destination_resolution.source,
        "departure_date": query.get("start_date"),
        "return_date": query.get("end_date"),
        "travelers_requested": counts["traveler_count"],
        "traveler_count": counts["traveler_count"],
        "adult_count": counts["adult_count"],
        "child_count": counts["child_count"],
        "infant_count": counts["infant_count"],
        "adults_arg": max(1, counts["traveler_count"]),
        "trvl_adults_passed": max(1, counts["traveler_count"]),
        "trvl_passenger_model": "all_travelers_as_adults",
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


def _is_risky_offer(flight: dict[str, Any], stderr_warnings: list[str]) -> bool:
    """Check if a flight offer exhibits risky/hack-style behavior.

    Returns True when the offer appears to be hidden-city, throwaway,
    skiplagged-hack, nested, self-connecting, or separate-tickets style.
    """
    # Check stderr warnings for risky terms
    risky_terms = (
        "hidden_city",
        "throwaway",
        "skiplagged_hack",
        "nested",
        "self_connect",
        "separate_tickets",
    )
    for warning in stderr_warnings:
        lower_warning = warning.lower()
        if any(term in lower_warning for term in risky_terms):
            return True

    # Check raw offer fields
    if flight.get("self_connect") is True:
        return True

    # Check provider/cheapest_source for hidden-city indicators
    trvl_provider = _first_string(flight, ("provider", "source_provider", "source")) or ""
    cheapest_source = _first_string(flight, ("cheapest_source", "cheapestSource")) or ""
    combined = (trvl_provider + " " + cheapest_source).lower()

    hidden_city_indicators = (
        "hidden_city",
        "hidden city",
        "hiddencity",
        "throwaway",
        "skiplagged_hack",
        "skiplagged hack",
        "skiplaggedhack",
        "skiplagged",
        "hack",
    )
    if any(indicator in combined for indicator in hidden_city_indicators):
        return True

    # Check warnings field on the flight object itself
    warnings = flight.get("warnings") or flight.get("warning")
    if isinstance(warnings, str):
        warnings_list = [w.strip() for w in warnings.split(",") if w.strip()]
    elif isinstance(warnings, list):
        warnings_list = [str(w).strip() for w in warnings if w]
    else:
        warnings_list = []

    for warning in warnings_list:
        lower_warning = warning.lower()
        if any(term in lower_warning for term in risky_terms + ("hidden_city", "throwaway", "skiplagged_hack")):
            return True

    # Check nested legs/segments for self-connect patterns
    for leg_key in ("legs", "segments"):
        legs = flight.get(leg_key)
        if isinstance(legs, list):
            for i, leg in enumerate(legs[:-1]):
                if not isinstance(leg, dict):
                    continue
                next_leg = legs[i + 1] if i + 1 < len(legs) else None
                if not isinstance(next_leg, dict):
                    continue
                # Self-connect: arrival airport of leg == departure airport of next leg
                # but they're on different tickets (different carriers or explicit flag)
                arr_airport = _first_string(leg, ("arrival", "arrival_airport", "arrivalAirport"))
                dep_airport = _first_string(next_leg, ("departure", "departure_airport", "departureAirport"))
                if arr_airport and dep_airport:
                    # If the same airport appears as both arrival and departure
                    # but carriers differ, it's a self-connect pattern
                    carrier1 = _first_string(leg, ("carrier", "carrier_code", "carrierCode", "airline_code"))
                    carrier2 = _first_string(next_leg, ("carrier", "carrier_code", "carrierCode", "airline_code"))
                    if carrier1 and carrier2 and str(carrier1).upper() != str(carrier2).upper():
                        return True

    return False


def _classify_risk(flight: dict[str, Any], stderr_warnings: list[str]) -> str:
    """Classify the specific risk type of a flight offer."""
    # Check stderr warnings first for specificity
    risky_terms = {
        "hidden_city": "hidden_city",
        "throwaway": "throwaway",
        "skiplagged_hack": "provider_skiplagged",
        "nested": "nested",
        "self_connect": "self_connect",
        "separate_tickets": "separate_tickets",
    }
    for warning in stderr_warnings:
        lower_warning = warning.lower()
        for term, label in risky_terms.items():
            if term in lower_warning:
                return label

    # Check flight fields
    if flight.get("self_connect") is True:
        return "self_connect"

    trvl_provider = _first_string(flight, ("provider", "source_provider", "source")) or ""
    cheapest_source = _first_string(flight, ("cheapest_source", "cheapestSource")) or ""
    combined = (trvl_provider + " " + cheapest_source).lower()

    if any(ind in combined for ind in ("hidden_city", "hidden city", "hiddencity")):
        return "hidden_city"
    if any(ind in combined for ind in ("throwaway",)):
        return "throwaway"
    if any(ind in combined for ind in ("skiplagged_hack", "skiplagged hack", "skiplaggedhack", "skiplagged")):
        return "provider_skiplagged"

    # Check warnings on flight object
    warnings = flight.get("warnings") or flight.get("warning")
    if isinstance(warnings, str):
        warnings_list = [w.strip() for w in warnings.split(",") if w.strip()]
    elif isinstance(warnings, list):
        warnings_list = [str(w).strip() for w in warnings if w]
    else:
        warnings_list = []

    for warning in warnings_list:
        lower_warning = warning.lower()
        if "hidden_city" in lower_warning or "throwaway" in lower_warning:
            return "hidden_city"
        if "skiplagged_hack" in lower_warning:
            return "provider_skiplagged"

    return "risky_offer"


def _build_risky_round_trip_alternatives(
    risky_offers: list[dict[str, Any]],
    query_json: dict[str, Any],
    *,
    stderr: str = "",
    command_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a broad_alternatives entry from captured risky round-trip offers."""
    alternatives: list[dict[str, Any]] = []
    skipped_reasons: list[dict[str, str]] = []

    for ro in risky_offers:
        risk_type = ro.get("risk_type", "risky_offer")
        # Determine offer_category based on risk type
        if risk_type == "self_connect":
            offer_category = "risky_round_trip"
        elif risk_type in ("hidden_city", "throwaway"):
            offer_category = "risky_round_trip"
        else:
            offer_category = "risky_round_trip"

        # Determine broad_reason from risk type
        broad_reason_map = {
            "self_connect": "self_connect",
            "hidden_city": "hidden_city",
            "throwaway": "throwaway",
            "provider_skiplagged": "provider_skiplagged",
            "nested": "nested",
            "separate_tickets": "separate_tickets",
        }
        broad_reason = broad_reason_map.get(risk_type, "risky_offer")

        offer = {
            "component_type": "flight",
            "component_type_label": "Airfare (Broad Alternative)",
            "source_name": SOURCE_NAME,
            "result_type": "flight",
            "provider": ro["provider"],
            "airline_name": ro["provider"],
            "label": ro.get("label", ""),
            "total_price": ro["price"],
            "currency": ro["currency"],
            "origin": ro.get("origin") or "",
            "destination": ro.get("destination") or "",
            "departure": ro.get("departure"),
            "arrival": ro.get("arrival"),
            "departure_date": ro.get("departure_date"),
            "return_date": ro.get("return_date"),
            "duration": ro.get("duration"),
            "stops": ro.get("stops"),
            "flight_numbers": [ro["flight_signature"]] if ro.get("flight_signature") else None,
            "flight_signature": ro.get("flight_signature"),
            "trvl_provider": ro.get("trvl_provider"),
            "cheapest_source": ro.get("cheapest_source"),
            "source_url": ro.get("source_url"),
            "link_type": "exact_source" if ro.get("source_url") else "none",
            "link_label": "View source price" if ro.get("source_url") else None,
            "mock": False,
            "search_type": "round_trip",
            "offer_category": offer_category,
            "broad_reason": broad_reason,
            "eligibility_for_best_deal": False,
            "is_risky": True,
            "raw_offer_reference": ro.get("raw_offer_reference"),
        }
        alternatives.append(offer)

    return {
        "source_name": SOURCE_NAME,
        "result_type": "flight",
        "search_type": "round_trip",
        "alternatives": alternatives,
        "raw_count": len(risky_offers),
        "normalized_count": len(alternatives),
        "skipped_count": 0,
        "skipped_reasons": skipped_reasons,
        "stderr_warnings": _stderr_warnings(stderr),
        "command": _bounded_public_data(command_metadata or {}, max_depth=3, max_items=20),
        "query": query_json,
    }


def _clean_airport_code(value: str | None) -> str | None:
    """Clean airport code strings by removing embedded quotes.

    For example, "'PIT'" becomes "PIT", and '"ORD"' becomes "ORD".
    """
    if value is None:
        return None
    cleaned = str(value).strip()
    # Remove surrounding single or double quotes that may be part of the string content
    while len(cleaned) >= 2 and ((cleaned[0] == "'" and cleaned[-1] == "'") or (cleaned[0] == '"' and cleaned[-1] == '"')):
        cleaned = cleaned[1:-1].strip()
    return cleaned if cleaned else None


def normalize_flights(raw: Any, query_json: dict[str, Any], *, max_results: int, stderr: str = "", command_metadata: dict[str, Any] | None = None, traveler_pricing_note: str | None = None, captured_risky_offers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    flights = _candidate_items(raw, ("flights", "offers", "results", "itineraries"))
    offers: list[dict[str, Any]] = []
    skipped_count = 0
    skipped_reasons: list[dict[str, str]] = []

    # Read risk/currency config from environment
    allow_risky = os.environ.get("TRVL_ALLOW_RISKY_FLIGHT_OFFERS", "false").strip().lower() in {"1", "true", "yes", "on"}
    require_currency = os.environ.get("TRVL_REQUIRE_CONFIGURED_CURRENCY", "true").strip().lower() in {"1", "true", "yes", "on"}

    # Get the configured currency from query_json (set by build_flight_query)
    configured_currency = query_json.get("currency") or DEFAULT_CURRENCY

    stderr_warnings = _stderr_warnings(stderr)

    for flight in flights:
        price, currency = _price_and_currency(
            flight,
            ("total_price", "totalPrice", "price", "amount", "fare", "cost", "extracted_price", "cheapest_price"),
        )
        provider, trvl_provider, cheapest_source = _flight_provider(flight)
        if price is None or not currency or not provider:
            skipped_count += 1
            skipped_reasons.append({"reason": "missing_data", "provider": str(provider or "")})
            continue

        # Risk filtering
        if not allow_risky and _is_risky_offer(flight, stderr_warnings):
            skipped_count += 1
            reason_parts = []
            if flight.get("self_connect") is True:
                reason_parts.append("self_connect")
            warnings = flight.get("warnings") or flight.get("warning")
            if isinstance(warnings, str):
                for w in [x.strip() for x in warnings.split(",") if x.strip()]:
                    if any(t in w.lower() for t in ("hidden_city", "throwaway", "skiplagged_hack")):
                        reason_parts.append(w)
            elif isinstance(warnings, list):
                for w in [str(x).strip() for x in warnings if x]:
                    if any(t in w.lower() for t in ("hidden_city", "throwaway", "skiplagged_hack")):
                        reason_parts.append(w)
            cheapest = cheapest_source or trvl_provider or provider
            risk_reason = "; ".join(reason_parts) or "risky_pattern"
            if any(ind in cheapest.lower() for ind in ("hidden_city", "throwaway", "skiplagged", "hack")):
                reason_parts.append(f"provider={cheapest}")
                risk_reason = "; ".join(reason_parts)
            skipped_reasons.append({"reason": "risky_offer", "details": risk_reason})

            # Capture risky offer for broad_alternatives when requested
            if captured_risky_offers is not None:
                departure, arrival = _flight_times(flight)
                source_url = _source_url(flight)
                flight_signature = _flight_number_signature(flight)
                origin = query_json.get("origin_airport") or query_json.get("origin_value", "")
                destination = query_json.get("destination_airport") or query_json.get("destination_value", "")
                label_parts = [str(provider), str(origin), "to", str(destination)]
                label = " ".join(part for part in label_parts if part)

                # Determine specific risk type for broad_reason
                risk_type = _classify_risk(flight, stderr_warnings)

                captured_risky_offers.append({
                    "price": price,
                    "currency": currency,
                    "provider": provider,
                    "trvl_provider": trvl_provider,
                    "cheapest_source": cheapest_source,
                    "departure": departure,
                    "arrival": arrival,
                    "source_url": source_url,
                    "flight_signature": flight_signature,
                    "label": label,
                    "origin": origin,
                    "destination": destination,
                    "duration": flight.get("duration"),
                    "stops": flight.get("stops"),
                    "departure_date": query_json.get("departure_date"),
                    "return_date": query_json.get("return_date"),
                    "risk_type": risk_type,
                    "raw_offer_reference": _clean_raw_airport_codes(_bounded_public_data(flight, max_depth=3, max_items=12)),
                })
            continue

        # Currency filtering
        if require_currency and configured_currency and currency.upper() != configured_currency.upper():
            skipped_count += 1
            skipped_reasons.append({
                "reason": "currency_mismatch",
                "offer_currency": currency,
                "configured_currency": configured_currency,
            })
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

        # Clean airport code metadata (remove embedded quotes)
        origin = _clean_airport_code(query_json.get("origin_airport")) or _clean_airport_code(query_json.get("origin_value"))
        destination = _clean_airport_code(query_json.get("destination_airport")) or _clean_airport_code(query_json.get("destination_value"))

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
            "origin": origin,
            "destination": destination,
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
            "raw_offer_reference": _clean_raw_airport_codes(_bounded_public_data(flight, max_depth=3, max_items=12)),
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
        "skipped_reasons": skipped_reasons,
        "provider_statuses": raw.get("provider_statuses") if isinstance(raw, dict) else None,
        "stderr_warnings": stderr_warnings,
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


def _run_trvl(command: list[str], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    """Run a trvl command once and return the captured result with timing info.

    The returned CompletedProcess has an ``elapsed_seconds`` attribute attached
    so callers can inspect elapsed time without losing stdout/stderr/exit_code.
    """
    start = time.monotonic()
    result = subprocess.run(command, text=True, capture_output=True, timeout=timeout_seconds, check=False)
    result.elapsed_seconds = time.monotonic() - start  # type: ignore[attr-defined]
    return result


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
    broad_discovery_enabled: bool = False,
    broad_include_one_way_fallbacks: bool = True,
    broad_max_alternatives: int = 50,
    broad_allow_risky_alternatives: bool = True,
) -> dict[str, Any]:
    query_json = build_flight_query(query, currency=currency, preferred_airports=preferred_airports, alternate_airports=alternate_airports)
    if not enabled:
        return _skip("TRVL_ENABLED=false", "flight", query_json)
    if not query_json.get("origin_airport"):
        return _skip(f"Could not resolve origin to an airport code: {query_json.get('origin_value')}", "flight", query_json)
    if not query_json.get("destination_airport"):
        return _skip(f"Could not resolve destination to an airport code: {query_json.get('destination_value')}", "flight", query_json)
    if not query_json.get("departure_date"):
        return _skip("trvl flights requires departure_date", "flight", query_json)
    binary = resolve_trvl_binary(binary_path)
    if not binary:
        return _skip("TRVL_ENABLED=true but trvl binary was not found", "flight", query_json)

    has_return = bool(query_json.get("return_date"))

    # ── Normal round-trip search (primary source of safe offers) ──────────
    rt_command = [
        binary,
        "flights",
        str(query_json["origin_airport"]),
        str(query_json["destination_airport"]),
        str(query_json["departure_date"]),
    ]
    if has_return:
        rt_command.extend(["--return", str(query_json["return_date"])])
    rt_command.extend(["--adults", str(query_json["adults_arg"]), "--currency", currency, "--format", "json"])

    traveler_pricing_note = None
    if int(query_json["travelers_requested"] or 1) > 1 and (
        int(query_json.get("child_count") or 0) > 0 or int(query_json.get("infant_count") or 0) > 0
    ):
        traveler_pricing_note = "trvl flight search priced all travelers as adults because trvl CLI only exposes --adults"

    # Run round-trip command (captured once, used everywhere)
    try:
        rt_result = _run_trvl(rt_command, timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        concise = f"trvl flights timed out after {timeout_seconds} seconds"
        diagnostics = _command_diagnostics(
            command_label="round_trip",
            command=rt_command,
            elapsed_seconds=timeout_seconds,
            exception=exc,
            query_json=query_json,
        )
        return {
            "status": "error",
            "normalized_result": {
                "source_name": SOURCE_NAME,
                "result_type": "flight",
                "offers": [],
                "reason": concise,
                "command_results": [diagnostics],
                "query": query_json,
            },
            "raw_result": {"diagnostic_error_excerpt": concise, "command_results": [diagnostics]},
            "error_message": concise,
        }
    except OSError as exc:
        concise = f"trvl flights failed to start: {exc}"
        diagnostics = _command_diagnostics(
            command_label="round_trip",
            command=rt_command,
            exception=exc,
            query_json=query_json,
        )
        return {
            "status": "error",
            "normalized_result": {
                "source_name": SOURCE_NAME,
                "result_type": "flight",
                "offers": [],
                "reason": concise,
                "command_results": [diagnostics],
                "query": query_json,
            },
            "raw_result": {"diagnostic_error_excerpt": concise, "command_results": [diagnostics]},
            "error_message": _concise_text(concise),
        }
    rt_raw = _load_json_text(rt_result.stdout)
    rt_raw_summary = _raw_summary(
        rt_raw, rt_result.stdout, rt_result.stderr,
        rt_result.returncode, getattr(rt_result, "elapsed_seconds", 0),
    )

    if rt_result.returncode != 0 and not _success(rt_raw):
        # RT command failed with no usable JSON. Still run one-way commands for diagnostics,
        # then return error status with bounded command_results explaining each failure.
        broad_alternatives: list[dict[str, Any]] = []
        broad_skipped_reasons: list[dict[str, str]] = []
        command_results: list[dict[str, Any]] = [
            _command_diagnostics(
                command_label="round_trip",
                command=rt_command,
                result=rt_result,
                raw=rt_raw,
                query_json=query_json,
            )
        ]

        # Run one-way commands to collect diagnostics even when RT failed
        if broad_discovery_enabled and has_return:
            outbound_command = [
                binary, "flights",
                str(query_json["origin_airport"]),
                str(query_json["destination_airport"]),
                str(query_json["departure_date"]),
                "--adults", str(query_json["adults_arg"]),
                "--currency", currency, "--format", "json",
            ]
            outbound_result = _run_trvl(outbound_command, timeout_seconds)
            bw_raw_out = _load_json_text(outbound_result.stdout)
            command_results.append(
                _command_diagnostics(
                    command_label="outbound_one_way",
                    command=outbound_command,
                    result=outbound_result,
                    raw=bw_raw_out,
                    query_json=query_json,
                )
            )

            return_query_json = dict(query_json)
            return_query_json["origin_airport"] = query_json["destination_airport"]
            return_query_json["destination_airport"] = query_json["origin_airport"]
            return_query_json["departure_date"] = query_json["return_date"]
            return_query_json.pop("return_date", None)

            return_command = [
                binary, "flights",
                str(return_query_json["origin_airport"]),
                str(return_query_json["destination_airport"]),
                str(return_query_json["departure_date"]),
                "--adults", str(query_json["adults_arg"]),
                "--currency", currency, "--format", "json",
            ]
            return_result = _run_trvl(return_command, timeout_seconds)
            bw_raw_ret = _load_json_text(return_result.stdout)
            command_results.append(
                _command_diagnostics(
                    command_label="return_one_way",
                    command=return_command,
                    result=return_result,
                    raw=bw_raw_ret,
                    query_json=return_query_json,
                )
            )

        # Build error result with bounded diagnostics for all commands
        concise = f"trvl flights exited with code {rt_result.returncode}"
        fallback_commands_attempted = len(command_results) > 1
        fallback_usable_offer_count = sum(
            int(command_result.get("stdout_json_count") or 0)
            for command_result in command_results
            if command_result.get("command_label") in ("outbound_one_way", "return_one_way")
            and int(command_result.get("exit_code") or 0) == 0
            and command_result.get("stdout_json_success")
        )
        provider_failure = _trvl_provider_failure(rt_result.stderr, rt_result.stdout, concise) or {}
        latest_error_message = _concise_text(rt_result.stderr or rt_result.stdout or concise)
        normalized_result = {
            "source_name": SOURCE_NAME,
            "result_type": "flight",
            "offers": [],
            "reason": concise,
            "latest_trvl_exit_code": rt_result.returncode,
            "latest_trvl_error_message": latest_error_message,
            "command_results": command_results[:10],  # bounded to last 10 failures
            "query": query_json,
            "resolved_origin_airport": query_json.get("origin_airport"),
            "resolved_destination_airport": query_json.get("destination_airport"),
            "origin_resolution_status": query_json.get("origin_resolution_status"),
            "destination_resolution_status": query_json.get("destination_resolution_status"),
            "origin_resolution_source": query_json.get("origin_resolution_source"),
            "destination_resolution_source": query_json.get("destination_resolution_source"),
            "traveler_count": query_json.get("traveler_count"),
            "adult_count": query_json.get("adult_count"),
            "child_count": query_json.get("child_count"),
            "infant_count": query_json.get("infant_count"),
            "trvl_adults_passed": query_json.get("trvl_adults_passed"),
            "trvl_passenger_model": query_json.get("trvl_passenger_model"),
            "fallback_commands_attempted": fallback_commands_attempted,
            "fallback_command_count": max(0, len(command_results) - 1),
            "fallback_usable_offers": fallback_usable_offer_count > 0,
            "fallback_usable_offer_count": fallback_usable_offer_count,
        }
        normalized_result.update(provider_failure)
        return {
            "status": "error",
            "normalized_result": normalized_result,
            "raw_result": rt_raw_summary,
            "error_message": concise,
        }

    # Normalize round-trip results as safe offers (existing behavior).
    # When broad_discovery_enabled, also capture risky offers for broad_alternatives.
    captured_risky_offers: list[dict[str, Any]] = []
    normalized = normalize_flights(
        rt_raw,
        query_json,
        max_results=max_results,
        stderr=rt_result.stderr,
        command_metadata={
            "argv": rt_command,
            "exit_code": rt_result.returncode,
            "elapsed_seconds": round(getattr(rt_result, "elapsed_seconds", 0), 3),
            "stdout_json_success": _success(rt_raw),
            "stdout_json_count": len(_candidate_items(rt_raw, ("flights", "offers", "results", "itineraries"))) if isinstance(rt_raw, dict) else 0,
        },
        traveler_pricing_note=traveler_pricing_note,
        captured_risky_offers=captured_risky_offers,
    )

    # ── Broad discovery (optional fallback) ────────────────────────────────
    broad_alternatives: list[dict[str, Any]] = []
    broad_summary: dict[str, Any] = {}
    broad_skipped_reasons: list[dict[str, str]] = []
    broad_command_results: list[dict[str, Any]] = []

    if broad_discovery_enabled and has_return:
        # Store full command results (not just stdout/stderr) to avoid double-runs
        one_way_results: list[tuple[str, subprocess.CompletedProcess[str], dict[str, Any]]] = []  # (search_type, result, query_json)

        # Outbound one-way: ORIGIN -> DESTINATION on depart date
        outbound_command = [
            binary, "flights",
            str(query_json["origin_airport"]),
            str(query_json["destination_airport"]),
            str(query_json["departure_date"]),
            "--adults", str(query_json["adults_arg"]),
            "--currency", currency, "--format", "json",
        ]
        outbound_result = _run_trvl(outbound_command, timeout_seconds)
        one_way_results.append(("outbound_one_way", outbound_result, query_json))

        # Return one-way: DESTINATION -> ORIGIN on return date
        return_query_json = dict(query_json)
        return_query_json["origin_airport"] = query_json["destination_airport"]
        return_query_json["destination_airport"] = query_json["origin_airport"]
        return_query_json["departure_date"] = query_json["return_date"]
        return_query_json.pop("return_date", None)

        return_command = [
            binary, "flights",
            str(return_query_json["origin_airport"]),
            str(return_query_json["destination_airport"]),
            str(return_query_json["departure_date"]),
            "--adults", str(query_json["adults_arg"]),
            "--currency", currency, "--format", "json",
        ]
        return_result = _run_trvl(return_command, timeout_seconds)
        one_way_results.append(("return_one_way", return_result, return_query_json))

        # Normalize each one-way search as broad alternatives (no double-runs)
        for stype, cmd_result, bw_query in one_way_results:
            bw_raw = _load_json_text(cmd_result.stdout)
            bw_meta = {
                "argv": cmd_result.args,
                "exit_code": cmd_result.returncode,
                "elapsed_seconds": round(getattr(cmd_result, "elapsed_seconds", 0), 3),
                "stdout_json_success": _success(bw_raw),
                "stdout_json_count": len(_candidate_items(bw_raw, ("flights", "offers", "results", "itineraries"))) if isinstance(bw_raw, dict) else 0,
            }

            # Always capture command diagnostics for every executed one-way command
            broad_command_results.append({
                "label": stype,
                "command": cmd_result.args,
                "exit_code": cmd_result.returncode,
                "elapsed_seconds": round(getattr(cmd_result, "elapsed_seconds", 0), 3),
                "stdout_json_success": bw_meta["stdout_json_success"],
                "stdout_json_count": bw_meta["stdout_json_count"],
                "stderr_preview": _bounded_string(cmd_result.stderr, 500),
            })

            if broad_include_one_way_fallbacks or (stype == "outbound_one_way" and len(normalized.get("offers", [])) == 0):
                bw_normalized = normalize_broad_alternatives(
                    bw_raw,
                    bw_query,
                    search_type=stype,
                    allow_risky=broad_allow_risky_alternatives,
                    stderr=cmd_result.stderr,
                    command_metadata=bw_meta,
                )
                broad_alternatives.append(bw_normalized)
                broad_skipped_reasons.extend(bw_normalized.get("skipped_reasons", []))

        # Include captured risky offers from primary round-trip as broad alternatives
        if broad_allow_risky_alternatives and captured_risky_offers:
            risky_alt = _build_risky_round_trip_alternatives(
                captured_risky_offers,
                query_json,
                stderr=rt_result.stderr,
                command_metadata={
                    "argv": rt_command,
                    "exit_code": rt_result.returncode,
                    "elapsed_seconds": round(getattr(rt_result, "elapsed_seconds", 0), 3),
                    "stdout_json_success": _success(rt_raw),
                    "stdout_json_count": len(_candidate_items(rt_raw, ("flights", "offers", "results", "itineraries"))) if isinstance(rt_raw, dict) else 0,
                },
            )
            broad_alternatives.append(risky_alt)
            broad_skipped_reasons.extend(risky_alt.get("skipped_reasons", []))

        # Build broad summary (always present when broad discovery is enabled and has_return)
        total_broad = sum(a["raw_count"] for a in broad_alternatives) if broad_alternatives else 0
        total_broad_norm = sum(a["normalized_count"] for a in broad_alternatives) if broad_alternatives else 0
        broad_summary = {
            "enabled": True,
            "one_way_searches_run": len(one_way_results),
            "total_raw_alternatives": total_broad,
            "total_normalized_alternatives": total_broad_norm,
            "search_types": [a["search_type"] for a in broad_alternatives] if broad_alternatives else [],
        }

    # Merge broad data into normalized result diagnostics
    if broad_discovery_enabled and has_return:
        # Always include broad_summary when broad discovery is enabled with return date
        normalized["broad_summary"] = broad_summary
        if broad_alternatives:
            normalized["broad_alternatives"] = broad_alternatives[:broad_max_alternatives]
        if broad_skipped_reasons:
            normalized["broad_skipped_reasons"] = broad_skipped_reasons
        # Always include command diagnostics when broad discovery is enabled
        if broad_command_results:
            normalized["command_results"] = broad_command_results[:10]

    return {"status": "completed", "normalized_result": normalized, "raw_result": rt_raw_summary, "error_message": None}


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
        result = _run_trvl(command, timeout_seconds)
    except Exception as exc:
        return _error(str(exc), "hotel")
    raw = _load_json_text(result.stdout)
    elapsed = getattr(result, "elapsed_seconds", 0)
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


def _clean_raw_airport_codes(raw: Any) -> Any:
    """Recursively clean embedded quotes from airport codes and names in raw data."""
    if isinstance(raw, dict):
        cleaned: dict[str, Any] = {}
        for key, value in raw.items():
            if key in ("code", "name") and isinstance(value, str):
                cleaned[key] = _clean_airport_code(value) or value
            else:
                cleaned[key] = _clean_raw_airport_codes(value)
        return cleaned
    if isinstance(raw, list):
        return [_clean_raw_airport_codes(item) for item in raw]
    return raw


def normalize_broad_alternatives(
    raw: Any,
    query_json: dict[str, Any],
    *,
    search_type: str = "one_way",
    allow_risky: bool = True,
    stderr: str = "",
    command_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize broad discovery alternatives (one-way, risky, etc.).

    Unlike normalize_flights, this function does NOT filter out risky offers.
    It stores them separately under broad_alternatives so they are visible in
    diagnostics but do not become normal best_deal candidates.
    """
    flights = _candidate_items(raw, ("flights", "offers", "results", "itineraries"))
    alternatives: list[dict[str, Any]] = []
    skipped_count = 0
    skipped_reasons: list[dict[str, str]] = []

    configured_currency = query_json.get("currency") or DEFAULT_CURRENCY
    stderr_warnings = _stderr_warnings(stderr)

    for flight in flights:
        price, currency = _price_and_currency(
            flight,
            ("total_price", "totalPrice", "price", "amount", "fare", "cost", "extracted_price", "cheapest_price"),
        )
        provider, trvl_provider, cheapest_source = _flight_provider(flight)

        if price is None or not currency:
            skipped_count += 1
            skipped_reasons.append({"reason": "missing_data", "provider": str(provider or ""), "search_type": search_type})
            continue

        if not provider:
            skipped_count += 1
            skipped_reasons.append({"reason": "no_provider", "search_type": search_type})
            continue

        departure, arrival = _flight_times(flight)
        source_url = _source_url(flight)
        flight_signature = _flight_number_signature(flight)

        origin = query_json.get("origin_airport") or query_json.get("origin_value", "")
        destination = query_json.get("destination_airport") or query_json.get("destination_value", "")
        label_parts = [str(provider), str(origin), "to", str(destination)]
        label = " ".join(part for part in label_parts if part)

        is_one_way = search_type == "one_way"
        is_risky = _is_risky_offer(flight, stderr_warnings)

        # Determine offer_category and broad_reason with more specificity
        if is_risky:
            risk_type = _classify_risk(flight, stderr_warnings)
            if search_type in ("outbound_one_way", "return_one_way"):
                offer_category = "risky_one_way"
            else:
                offer_category = "risky_round_trip"
            broad_reason_map = {
                "self_connect": "self_connect",
                "hidden_city": "hidden_city",
                "throwaway": "throwaway",
                "provider_skiplagged": "provider_skiplagged",
                "nested": "nested",
                "separate_tickets": "separate_tickets",
            }
            broad_reason = broad_reason_map.get(risk_type, "risky_offer")
        elif is_one_way:
            offer_category = "one_way"
            broad_reason = "one_way_fallback"
        else:
            offer_category = "safe_alternative"
            broad_reason = "round_trip_safe"


        # Add eligibility_for_best_deal metadata (always False for broad alternatives)
        offer = {
            "component_type": "flight",
            "component_type_label": "Airfare (Broad Alternative)",
            "source_name": SOURCE_NAME,
            "result_type": "flight",
            "provider": provider,
            "airline_name": provider,
            "label": label,
            "total_price": price,
            "currency": currency,
            "origin": origin,
            "destination": destination,
            "departure": departure,
            "arrival": arrival,
            "departure_date": query_json.get("departure_date"),
            "return_date": None,
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
            "search_type": search_type,
            "offer_category": offer_category,
            "broad_reason": broad_reason,
            "eligibility_for_best_deal": False,
            "is_risky": is_risky,
            "raw_offer_reference": _clean_raw_airport_codes(_bounded_public_data(flight, max_depth=3, max_items=12)),
        }

        alternatives.append(offer)

    return {
        "source_name": SOURCE_NAME,
        "result_type": "flight",
        "search_type": search_type,
        "alternatives": alternatives,
        "raw_count": len(flights),
        "normalized_count": len(alternatives),
        "skipped_count": skipped_count,
        "skipped_reasons": skipped_reasons,
        "stderr_warnings": stderr_warnings,
        "command": _bounded_public_data(command_metadata or {}, max_depth=3, max_items=20),
        "query": query_json,
    }
