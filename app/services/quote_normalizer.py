from __future__ import annotations

import json
from datetime import date
from typing import Any

from app.db.models import PriceSnapshot, SourceResult, Vacation, utc_now
from app.services.search_planner import deterministic_json


PRICED_STATUSES = {"completed", "mock"}
QUOTE_TYPES = {"flight", "hotel", "rental_car", "package"}


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


def _source_url(payload: dict[str, Any]) -> str | None:
    return _first_string(payload, ["source_url", "url", "booking_url", "deep_link", "google_maps_uri", "website_uri"])


def _provider(payload: dict[str, Any], source_name: str) -> str | None:
    result_type = payload.get("result_type")
    if result_type == "flight":
        provider = _first_string(payload, ["provider", "airline", "airline_name"]) or _carrier_label(payload)
    elif result_type == "hotel":
        provider = _first_string(payload, ["hotel_name", "provider", "brand", "chain_name"])
    elif result_type == "rental_car":
        provider = _first_string(payload, ["rental_company", "company", "company_name", "provider"])
    else:
        provider = _first_string(payload, ["provider", "source_name"])
    return provider or source_name or "Unknown provider"


def _label(payload: dict[str, Any], quote_type: str) -> str:
    label = _first_string(
        payload,
        ["label", "title", "name", "hotel_name", "itinerary_summary", "room_offer_summary", "provider"],
    )
    return label or quote_type.replace("_", " ").title()


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
        total = _total_price(payload, quote_type, vacation)
        if total is None:
            continue
        currency = str(payload.get("currency") or normalized.get("currency") or "USD")
        provider = _provider({**payload, "result_type": quote_type}, source_result.source_name)
        source_payload = {
            "captured_at": captured_at.isoformat() if captured_at else None,
            "currency": currency,
            "label": _label(payload, quote_type),
            "provider": provider,
            "quote_type": quote_type,
            "source_result_id": source_result.id,
            "source_name": source_result.source_name,
            "source_status": source_result.status,
            "source_url": _source_url(payload),
            "total_price": total,
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
                source_url=_source_url(payload),
                normalized_json=deterministic_json(source_payload),
                captured_at=captured_at,
            )
        )
    return snapshots
