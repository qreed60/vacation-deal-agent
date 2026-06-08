from __future__ import annotations

from typing import Any

import httpx


TEXT_SEARCH_FIELD_MASK = "places.id,places.displayName,places.formattedAddress,places.rating,places.userRatingCount,places.googleMapsUri,places.websiteUri"
DETAILS_FIELD_MASK = "id,displayName,formattedAddress,rating,userRatingCount,googleMapsUri,websiteUri"


def skipped_result(reason: str) -> dict[str, Any]:
    return {
        "status": "skipped",
        "normalized_result": {"source_name": "google_places", "result_type": "place_enrichment", "reason": reason},
        "raw_result": {},
        "error_message": reason,
    }


def _normalize_place(place: dict[str, Any]) -> dict[str, Any]:
    display_name = place.get("displayName")
    return {
        "source_name": "google_places",
        "place_id": place.get("id"),
        "display_name": display_name.get("text") if isinstance(display_name, dict) else display_name,
        "formatted_address": place.get("formattedAddress"),
        "rating": place.get("rating"),
        "user_rating_count": place.get("userRatingCount"),
        "google_maps_uri": place.get("googleMapsUri"),
        "website_uri": place.get("websiteUri"),
    }


def normalize_text_search(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_name": "google_places",
        "result_type": "place_enrichment",
        "places": [_normalize_place(place) for place in raw.get("places", [])],
    }


def normalize_place_details(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_name": "google_places",
        "result_type": "place_enrichment",
        "place": _normalize_place(raw),
    }


def text_search(
    text_query: str,
    *,
    api_key: str,
    enabled: bool,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    if not enabled:
        return skipped_result("GOOGLE_PLACES_ENABLED=false")
    if not api_key:
        return skipped_result("Google Places API key is missing")
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": TEXT_SEARCH_FIELD_MASK,
    }
    payload = {"textQuery": text_query, "maxResultCount": 5}
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.post("https://places.googleapis.com/v1/places:searchText", headers=headers, json=payload)
            response.raise_for_status()
            raw = response.json()
    except Exception as exc:
        return {
            "status": "error",
            "normalized_result": {"source_name": "google_places", "result_type": "place_enrichment"},
            "raw_result": {},
            "error_message": str(exc),
        }
    return {
        "status": "completed",
        "normalized_result": normalize_text_search(raw),
        "raw_result": raw,
        "error_message": None,
    }


def place_details(
    place_id: str,
    *,
    api_key: str,
    enabled: bool,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    if not enabled:
        return skipped_result("GOOGLE_PLACES_ENABLED=false")
    if not api_key:
        return skipped_result("Google Places API key is missing")
    if not place_id:
        return skipped_result("Google Places place_id is missing")
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": DETAILS_FIELD_MASK,
    }
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.get(f"https://places.googleapis.com/v1/places/{place_id}", headers=headers)
            response.raise_for_status()
            raw = response.json()
    except Exception as exc:
        return {
            "status": "error",
            "normalized_result": {"source_name": "google_places", "result_type": "place_enrichment"},
            "raw_result": {},
            "error_message": str(exc),
        }
    return {
        "status": "completed",
        "normalized_result": normalize_place_details(raw),
        "raw_result": raw,
        "error_message": None,
    }
