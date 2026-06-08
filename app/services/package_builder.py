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


def _source_links(snapshots: list[PriceSnapshot]) -> list[dict[str, Any]]:
    links = []
    for snapshot in snapshots:
        links.append(
            {
                "component_type": snapshot.quote_type,
                "component_type_label": QUOTE_TYPE_LABELS.get(snapshot.quote_type, snapshot.quote_type.replace("_", " ").title()),
                "currency": snapshot.currency,
                "provider": _provider_label(snapshot),
                "source_name": snapshot.source_name,
                "source_result_id": snapshot.source_result_id,
                "source_url": snapshot.source_url,
                "captured_at": snapshot.captured_at.isoformat() if snapshot.captured_at else None,
                "label": snapshot.label,
                "total_price": snapshot.total_price,
            }
        )
    return links


def _provider_label(snapshot: PriceSnapshot) -> str:
    return snapshot.provider or snapshot.source_name or "Unknown provider"


def _component_summary(snapshot: PriceSnapshot) -> dict[str, Any]:
    return {
        "component_type": snapshot.quote_type,
        "component_type_label": QUOTE_TYPE_LABELS.get(snapshot.quote_type, snapshot.quote_type.replace("_", " ").title()),
        "provider": _provider_label(snapshot),
        "label": snapshot.label,
        "total_price": snapshot.total_price,
        "currency": snapshot.currency,
        "source_name": snapshot.source_name or "Unknown provider",
        "source_result_id": snapshot.source_result_id,
        "source_url": snapshot.source_url,
        "captured_at": snapshot.captured_at.isoformat() if snapshot.captured_at else None,
        "snapshot_id": snapshot.id,
        "is_mock": snapshot.source_name == "mock_travel" or (_load_json(snapshot.normalized_json).get("source_status") == "mock"),
    }


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
