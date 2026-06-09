from __future__ import annotations

import contextlib
import importlib
import importlib.util
import inspect
import re
from dataclasses import asdict, is_dataclass
from typing import Any
from urllib.parse import quote_plus


SOURCE_NAME = "fast_flights"
SAFE_FETCH_MODE = "common"
SAFE_SEATS = {"economy", "premium-economy", "business", "first"}


def _skip(reason: str) -> dict[str, Any]:
    return {
        "status": "skipped",
        "normalized_result": {"source_name": SOURCE_NAME, "result_type": "flight", "offers": [], "reason": reason},
        "raw_result": {},
        "error_message": reason,
    }


def _error(message: str, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    concise = _concise_error(message)
    return {
        "status": "error",
        "normalized_result": {"source_name": SOURCE_NAME, "result_type": "flight", "offers": [], "reason": concise},
        "raw_result": raw or {"diagnostic_error_excerpt": _concise_error(message, 1800)},
        "error_message": concise,
    }


def _module_exists(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _concise_error(value: str, limit: int = 1200) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}... [truncated]"


def _truncate_string(value: str, limit: int = 1000) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit].rstrip()}... [truncated]"


def _bounded_public_data(value: Any, *, max_depth: int = 3, max_items: int = 10) -> Any:
    if max_depth < 0:
        return _truncate_string(repr(value))
    if isinstance(value, str):
        return _truncate_string(value)
    if isinstance(value, (int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        return [_bounded_public_data(item, max_depth=max_depth - 1, max_items=max_items) for item in value[:max_items]]
    if isinstance(value, dict):
        return {
            str(key): _bounded_public_data(item, max_depth=max_depth - 1, max_items=max_items)
            for key, item in list(value.items())[:max_items]
            if not str(key).startswith("_")
        }
    if is_dataclass(value) and not isinstance(value, type):
        return _bounded_public_data(asdict(value), max_depth=max_depth, max_items=max_items)
    if hasattr(value, "model_dump"):
        with contextlib.suppress(Exception):
            return _bounded_public_data(value.model_dump(), max_depth=max_depth, max_items=max_items)
    if hasattr(value, "dict"):
        with contextlib.suppress(Exception):
            return _bounded_public_data(value.dict(), max_depth=max_depth, max_items=max_items)
    if hasattr(value, "__dict__"):
        return _bounded_public_data(
            {key: item for key, item in vars(value).items() if not key.startswith("_")},
            max_depth=max_depth,
            max_items=max_items,
        )
    return _truncate_string(repr(value))


def _object_to_data(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, list):
        return [_object_to_data(item) for item in value]
    if isinstance(value, dict):
        return {key: _object_to_data(item) for key, item in value.items() if not str(key).startswith("_")}
    if is_dataclass(value) and not isinstance(value, type):
        return _object_to_data(asdict(value))
    if hasattr(value, "model_dump"):
        with contextlib.suppress(Exception):
            return _object_to_data(value.model_dump())
    if hasattr(value, "dict"):
        with contextlib.suppress(Exception):
            return _object_to_data(value.dict())
    if hasattr(value, "__dict__"):
        return {key: _object_to_data(item) for key, item in vars(value).items() if not key.startswith("_")}
    return _truncate_string(repr(value))


def _diagnostic_raw(raw: Any) -> dict[str, Any]:
    flights = getattr(raw, "flights", None)
    if flights is None and isinstance(raw, dict):
        flights = raw.get("flights")
    flights = flights if isinstance(flights, list) else []
    return {
        "api_style": "v2",
        "result_public_fields": _bounded_public_data(raw, max_depth=3),
        "flight_count": len(flights),
        "sample_flights": [
            {
                "public_fields": _bounded_public_data(flight, max_depth=3),
                "field_names": _field_names(flight),
                "repr": _truncate_string(repr(flight)),
            }
            for flight in flights[:5]
        ],
    }


def _field_names(value: Any) -> list[str]:
    if isinstance(value, dict):
        return sorted(str(key) for key in value if not str(key).startswith("_"))
    if is_dataclass(value) and not isinstance(value, type):
        return sorted(str(key) for key in asdict(value) if not str(key).startswith("_"))
    if hasattr(value, "model_dump"):
        with contextlib.suppress(Exception):
            return sorted(str(key) for key in value.model_dump() if not str(key).startswith("_"))
    if hasattr(value, "dict"):
        with contextlib.suppress(Exception):
            return sorted(str(key) for key in value.dict() if not str(key).startswith("_"))
    if hasattr(value, "__dict__"):
        return sorted(str(key) for key in vars(value) if not str(key).startswith("_"))
    return []


def _parse_price(value: Any) -> tuple[float | None, str | None]:
    if value in (None, ""):
        return None, None
    if isinstance(value, dict):
        return _price_and_currency(value)
    if isinstance(value, (int, float)):
        return float(value), None
    text = str(value).strip()
    currency = "USD" if "$" in text or "US$" in text.upper() or "USD" in text.upper() else None
    match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", text)
    if not match:
        return None, currency
    try:
        return float(match.group(0).replace(",", "")), currency
    except ValueError:
        return None, currency


def _price_and_currency(payload: dict[str, Any]) -> tuple[float | None, str | None]:
    for key in ("total_price", "price", "price_raw", "amount", "total", "fare", "cost", "extracted_price", "extracted_lowest"):
        price, currency = _parse_price(payload.get(key))
        if price is not None:
            return price, currency
    return None, None


def _first_string(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _looks_like_airport_code(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{3}", value.strip()))


def _looks_like_route(value: str) -> bool:
    return bool(re.search(r"\b[A-Z]{3}\b\s*(?:to|->|-|→)\s*\b[A-Z]{3}\b", value.strip(), flags=re.IGNORECASE))


def _looks_like_flight_number(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z0-9]{2,3}\s*\d{1,4}[A-Z]?", value.strip()))


def _valid_provider(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if not text or _looks_like_airport_code(text) or _looks_like_route(text) or _looks_like_flight_number(text):
        return None
    return text


def _iter_dicts(raw: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        found.append(raw)
        for value in raw.values():
            if isinstance(value, (dict, list)):
                found.extend(_iter_dicts(value))
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, (dict, list)):
                found.extend(_iter_dicts(item))
    return found


def _provider_value(payload: dict[str, Any]) -> str | None:
    for key in ("provider", "airline", "airline_name", "airlines", "carrier", "carriers", "name", "company", "flight_name", "title"):
        value = payload.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, list):
            labels = [_valid_provider(str(item)) for item in value if item]
            labels = [label for label in labels if label]
            if labels:
                return ", ".join(dict.fromkeys(labels))
            continue
        provider = _valid_provider(str(value))
        if provider:
            return provider
    nested_providers: list[str] = []
    for key in ("segments", "legs", "flights", "details", "itinerary", "flight_details"):
        for nested in _iter_dicts(payload.get(key)):
            nested_provider = _provider_value({nested_key: nested_value for nested_key, nested_value in nested.items() if nested_key != key})
            if nested_provider:
                for part in [item.strip() for item in nested_provider.split(",")]:
                    if part and part not in nested_providers:
                        nested_providers.append(part)
    return ", ".join(nested_providers) if nested_providers else None


def _search_reference_url(provider: str, origin: str | None, destination: str | None, departure: str | None, return_date: str | None) -> str:
    query = " ".join(part for part in [provider, "flight", origin or "", destination or "", departure or "", return_date or ""] if part)
    return f"https://www.google.com/search?q={quote_plus(query)}"


def _normalize_flights(raw: Any, query: dict[str, Any]) -> dict[str, Any]:
    data = _object_to_data(raw)
    flights = data.get("flights") if isinstance(data, dict) else []
    flights = flights if isinstance(flights, list) else []
    offers: list[dict[str, Any]] = []
    diagnostic = _diagnostic_raw(raw)
    origin = query.get("origin")
    destination = query.get("destination")
    departure_date = query.get("start_date")
    return_date = query.get("end_date")
    for flight in flights:
        if not isinstance(flight, dict):
            continue
        provider = _provider_value(flight)
        price, price_currency = _price_and_currency(flight)
        if not provider or price is None:
            continue
        currency = _first_string(flight, ("currency", "currency_code")) or price_currency
        label = f"{provider} {origin} to {destination}".strip()
        offers.append(
            {
                "component_type": "flight",
                "component_type_label": "Airfare",
                "source_name": SOURCE_NAME,
                "result_type": "flight",
                "provider": provider,
                "airline_name": provider,
                "provider_code": _first_string(flight, ("provider_code", "airline_code", "carrier_code", "code")),
                "label": label,
                "total_price": price,
                "currency": currency,
                "origin": origin,
                "destination": destination,
                "departure": flight.get("departure"),
                "arrival": flight.get("arrival"),
                "departure_date": departure_date,
                "return_date": return_date,
                "duration": flight.get("duration"),
                "stops": flight.get("stops"),
                "source_url": None,
                "search_reference_url": _search_reference_url(provider, origin, destination, departure_date, return_date),
                "link_type": "search_reference",
                "link_label": "Search reference",
                "mock": False,
                "diagnostic_raw": diagnostic,
                "raw_offer_reference": flight,
            }
        )
    return {
        "source_name": SOURCE_NAME,
        "result_type": "flight",
        "offers": offers,
        "diagnostic_raw": diagnostic,
        "unpriced_result_count": max(0, len(flights) - len(offers)),
    }


def _safe_fetch_mode(fetch_mode: str | None) -> tuple[str, str | None]:
    if not fetch_mode:
        return SAFE_FETCH_MODE, None
    normalized = fetch_mode.strip().lower()
    if normalized != SAFE_FETCH_MODE:
        return SAFE_FETCH_MODE, f"Unsafe FAST_FLIGHTS_FETCH_MODE={fetch_mode!r}; forced to common"
    return normalized, None


def _safe_seat(seat: str | None) -> str:
    normalized = (seat or "economy").strip().lower()
    return normalized if normalized in SAFE_SEATS else "economy"


def search_fast_flights(
    query: dict[str, Any],
    *,
    enabled: bool,
    fetch_mode: str = SAFE_FETCH_MODE,
    seat: str = "economy",
    max_stops: int | None = None,
) -> dict[str, Any]:
    if not enabled:
        return _skip("FAST_FLIGHTS_ENABLED=false")
    if not _module_exists("fast_flights"):
        return _skip("fast-flights package is not installed")
    origin = query.get("origin")
    destination = query.get("destination")
    departure_date = query.get("start_date")
    return_date = query.get("end_date")
    if not origin or not destination or not departure_date:
        return _skip("fast-flights requires origin, destination, and start_date")

    safe_fetch_mode, fetch_mode_note = _safe_fetch_mode(fetch_mode)
    safe_seat = _safe_seat(seat)
    try:
        module = importlib.import_module("fast_flights")
        if not all(hasattr(module, name) for name in ("FlightData", "Passengers", "get_flights")):
            return _skip("fast-flights API shape is unsupported")
        signature = inspect.signature(module.get_flights)
        if not {"flight_data", "trip", "passengers", "seat"}.issubset(signature.parameters):
            return _skip("fast-flights get_flights API shape is unsupported")
        flight_data = [module.FlightData(date=departure_date, from_airport=origin, to_airport=destination, max_stops=max_stops)]
        if return_date:
            flight_data.append(module.FlightData(date=return_date, from_airport=destination, to_airport=origin, max_stops=max_stops))
        passengers = module.Passengers(adults=max(1, int(query.get("number_of_travelers") or 1)), children=0)
        raw = module.get_flights(
            flight_data=flight_data,
            trip="round-trip" if return_date else "one-way",
            passengers=passengers,
            seat=safe_seat,
            fetch_mode=safe_fetch_mode,
            max_stops=max_stops,
        )
    except Exception as exc:
        return _error(str(exc))

    normalized = _normalize_flights(raw, query)
    if fetch_mode_note:
        normalized.setdefault("notes", []).append(fetch_mode_note)
    return {
        "status": "completed",
        "normalized_result": normalized,
        "raw_result": {"diagnostic_raw": normalized["diagnostic_raw"], "notes": normalized.get("notes", [])},
        "error_message": None,
    }
