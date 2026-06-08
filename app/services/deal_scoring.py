from __future__ import annotations

import json
from typing import Any

from app.db.models import DealCandidate, SourceResult
from app.services.search_planner import deterministic_json


def _load_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _float_value(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _rating_penalty(normalized: dict[str, Any]) -> float:
    ratings = []
    for item in _walk_dicts(normalized):
        rating = _float_value(item.get("rating") or item.get("hotel_rating"))
        if rating is not None:
            ratings.append(rating)
    if not ratings:
        return 0.0
    lowest = min(ratings)
    if lowest >= 4.0:
        return 0.0
    return round((4.0 - lowest) * 50.0, 2)


def _distance_penalty(normalized: dict[str, Any]) -> float:
    distances = []
    for item in _walk_dicts(normalized):
        distance = _float_value(
            item.get("distance")
            or item.get("distance_miles")
            or item.get("distance_from_center_miles")
            or item.get("distance_km")
        )
        if distance is not None:
            distances.append(distance)
    if not distances:
        return 0.0
    closest = min(distances)
    if closest <= 10:
        return 0.0
    return round((closest - 10.0) * 5.0, 2)


def score_candidate(candidate: DealCandidate, source_results: list[SourceResult] | None = None) -> DealCandidate:
    breakdown: dict[str, Any] = {
        "base_total_price": candidate.total_price,
        "penalties": {},
        "score_direction": "lower_is_better",
    }
    if candidate.total_price is None:
        candidate.score = None
        breakdown["reason"] = "Candidate has no total price."
        candidate.score_breakdown_json = deterministic_json(breakdown)
        return candidate

    normalized = _load_json(candidate.normalized_result_json)
    penalties = breakdown["penalties"]
    penalties["low_hotel_rating"] = _rating_penalty(normalized)
    penalties["high_distance"] = _distance_penalty(normalized)
    penalties["missing_required_component"] = 250.0 if candidate.status in {"partial", "skipped"} else 0.0
    skipped_or_error = 0
    for result in source_results or []:
        if result.status in {"skipped", "error", "failed"}:
            skipped_or_error += 1
    penalties["skipped_or_error_source"] = float(skipped_or_error * 25)
    total_penalty = round(sum(float(value) for value in penalties.values()), 2)
    breakdown["total_penalty"] = total_penalty
    candidate.score = round(float(candidate.total_price) + total_penalty, 2)
    candidate.score_breakdown_json = deterministic_json(breakdown)
    return candidate
