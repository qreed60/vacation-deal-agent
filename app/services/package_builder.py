from __future__ import annotations

import itertools
import json
from typing import Any

from sqlmodel import Session, select

from app.db.models import DealCandidate, PriceSnapshot, SourceResult, Vacation
from app.services.deal_scoring import score_candidate
from app.services.search_planner import deterministic_json


SERVICE_TO_CANDIDATE = {
    "flight": "flight_only",
    "hotel": "hotel_only",
    "rental_car": "rental_car_only",
}

QUOTE_TYPE_LABELS = {
    "flight": "Airfare",
    "hotel": "Hotel",
    "rental_car": "Rental car",
    "package": "Package",
}

SOURCE_NAME_LABELS = {
    "amadeus": "Amadeus",
    "google_places": "Google Places",
    "mock_travel": "mock_travel",
    "searxng": "SearXNG",
    "serpapi_google_flights": "SerpAPI Google Flights",
    "serpapi_google_hotels": "SerpAPI Google Hotels",
    "structured_rental_car": "Structured rental car",
    "trvl": "trvl",
}


def required_quote_types(vacation: Vacation) -> list[str]:
    required = []
    if vacation.airfare_needed:
        required.append("flight")
    if vacation.hotel_needed:
        required.append("hotel")
    if vacation.rental_car_needed:
        required.append("rental_car")
    return required


def _load_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _source_name_label(source_name: str | None) -> str:
    if not source_name:
        return "Unknown source"
    return SOURCE_NAME_LABELS.get(source_name, source_name)


def _snapshot_details(snapshot: PriceSnapshot) -> dict[str, Any]:
    normalized = _load_json(snapshot.normalized_json)
    return normalized if normalized else {}


def _component_payload(snapshot: PriceSnapshot) -> dict[str, Any]:
    details = _snapshot_details(snapshot)
    source_name = snapshot.source_name or details.get("source_name")
    source_status = details.get("source_status")
    provider = _provider_label(snapshot)
    payload = {
        "component_type": snapshot.quote_type,
        "component_type_label": QUOTE_TYPE_LABELS.get(snapshot.quote_type, snapshot.quote_type.replace("_", " ").title()),
        "provider": provider,
        "provider_code": details.get("provider_code"),
        "source_name": source_name or "Unknown source",
        "source_name_label": _source_name_label(source_name),
        "source_result_id": snapshot.source_result_id,
        "source_url": snapshot.source_url,
        "search_reference_url": details.get("search_reference_url"),
        "link_type": details.get("link_type") or ("exact_source" if snapshot.source_url else ("search_reference" if details.get("search_reference_url") else "none")),
        "link_label": details.get("link_label") or ("View source price" if snapshot.source_url else ("Search reference" if details.get("search_reference_url") else None)),
        "captured_at": snapshot.captured_at.isoformat() if snapshot.captured_at else details.get("captured_at"),
        "label": snapshot.label,
        "total_price": snapshot.total_price,
        "currency": snapshot.currency,
        "snapshot_id": snapshot.id,
        "mock": bool(details.get("mock") or source_name == "mock_travel" or source_status == "mock"),
        "is_mock": bool(details.get("mock") or source_name == "mock_travel" or source_status == "mock"),
        "source_status": source_status,
    }
    for key in (
        "airline_name",
        "carrier_code",
        "chain_code",
        "flight_numbers",
        "google_maps_uri",
        "hotel_name",
        "itinerary_summary",
        "rating",
        "rental_company",
        "validating_airline_codes",
        "vehicle_label",
        "website_uri",
    ):
        if details.get(key) is not None:
            payload[key] = details[key]
    return payload


def _source_links(snapshots: list[PriceSnapshot]) -> list[dict[str, Any]]:
    return [_component_payload(snapshot) for snapshot in snapshots]


def _provider_label(snapshot: PriceSnapshot) -> str:
    return snapshot.provider or snapshot.source_name or "Unknown provider"


def _component_summary(snapshot: PriceSnapshot) -> dict[str, Any]:
    return _component_payload(snapshot)


def _candidate(
    vacation: Vacation,
    search_run_id: int,
    candidate_type: str,
    snapshots: list[PriceSnapshot],
    *,
    status: str,
    missing_quote_types: list[str] | None = None,
) -> DealCandidate:
    total = None
    if snapshots:
        total = round(sum(float(snapshot.total_price or 0) for snapshot in snapshots), 2)
    currencies = {snapshot.currency for snapshot in snapshots if snapshot.currency}
    currency = sorted(currencies)[0] if currencies else "USD"
    labels = [snapshot.label for snapshot in snapshots]
    title = " + ".join(labels) if labels else f"{vacation.title} {candidate_type.replace('_', ' ')}"
    normalized = {
        "candidate_type": candidate_type,
        "status": status,
        "missing_quote_types": missing_quote_types or [],
        "component_summary": [_component_summary(snapshot) for snapshot in snapshots],
        "components": [_load_json(snapshot.normalized_json) for snapshot in snapshots],
    }
    is_mock = any(
        bool(snapshot.is_mock or _component_payload(snapshot).get("is_mock"))
        for snapshot in snapshots
    )
    return DealCandidate(
        vacation_id=vacation.id,
        search_run_id=search_run_id,
        candidate_type=candidate_type,
        title=title,
        status=status,
        total_price=total,
        currency=currency,
        component_snapshot_ids_json=json.dumps([snapshot.id for snapshot in snapshots]),
        source_links_json=json.dumps(_source_links(snapshots), sort_keys=True),
        normalized_result_json=deterministic_json(normalized),
        is_mock=is_mock,
    )


def build_deal_candidates(
    session: Session,
    vacation: Vacation,
    search_run_id: int,
    snapshots: list[PriceSnapshot] | None = None,
) -> list[DealCandidate]:
    if snapshots is None:
        snapshots = list(
            session.exec(
                select(PriceSnapshot)
                .where(PriceSnapshot.search_run_id == search_run_id)
                .where(PriceSnapshot.total_price.is_not(None))
                .order_by(PriceSnapshot.quote_type.asc(), PriceSnapshot.total_price.asc(), PriceSnapshot.id.asc())
            ).all()
        )

    source_results = list(session.exec(select(SourceResult).where(SourceResult.search_run_id == search_run_id)).all())
    priced_by_type: dict[str, list[PriceSnapshot]] = {"flight": [], "hotel": [], "rental_car": [], "package": []}
    for snapshot in snapshots:
        if snapshot.total_price is not None and snapshot.quote_type in priced_by_type:
            priced_by_type[snapshot.quote_type].append(snapshot)
    for quote_type in priced_by_type:
        priced_by_type[quote_type].sort(key=lambda item: (float(item.total_price or 0), item.id or 0))

    required = required_quote_types(vacation)
    candidates: list[DealCandidate] = []
    if len(required) == 1:
        quote_type = required[0]
        for snapshot in priced_by_type[quote_type]:
            candidates.append(_candidate(vacation, search_run_id, SERVICE_TO_CANDIDATE[quote_type], [snapshot], status="valid"))
    elif len(required) > 1:
        if all(priced_by_type[quote_type] for quote_type in required):
            for combination in itertools.product(*(priced_by_type[quote_type] for quote_type in required)):
                candidates.append(_candidate(vacation, search_run_id, "package", list(combination), status="valid"))
        else:
            present = [priced_by_type[quote_type][0] for quote_type in required if priced_by_type[quote_type]]
            missing = [quote_type for quote_type in required if not priced_by_type[quote_type]]
            status = "partial" if present else "skipped"
            candidates.append(
                _candidate(
                    vacation,
                    search_run_id,
                    "package",
                    present,
                    status=status,
                    missing_quote_types=missing,
                )
            )

    for candidate in candidates:
        score_candidate(candidate, source_results)
    return candidates
