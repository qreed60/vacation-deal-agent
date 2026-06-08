from __future__ import annotations

from urllib.parse import quote_plus
from typing import Any


def _search_reference_url(*parts: str) -> str:
    query = " ".join(part for part in parts if part)
    return f"https://www.google.com/search?q={quote_plus(query)}"


def search(query_entry: dict[str, Any]) -> dict[str, Any]:
    result_type = query_entry["result_type"]
    query = query_entry["query"]
    destination = query["destination"]

    if result_type == "flight":
        label = f"Mock Air flight {query['origin']} to {destination}"
        normalized = {
            "mock": True,
            "source_name": "mock_travel",
            "result_type": "flight",
            "label": label,
            "currency": "USD",
            "total_price": 425,
            "provider": "Mock Air",
            "provider_code": "MA",
            "carrier_code": "MA",
            "airline_name": "Mock Air",
            "itinerary_summary": f"{query['origin']}->{destination}",
            "departure_date": query.get("start_date"),
            "return_date": query.get("end_date"),
            "search_reference_url": _search_reference_url("Mock Air flight", query.get("origin", ""), destination, query.get("start_date") or "", query.get("end_date") or ""),
            "link_type": "search_reference",
            "link_label": "Search reference",
            "notes": "Mock Phase 2 flight result. No live provider was called.",
        }
    elif result_type == "hotel":
        label = f"Mock Harbor Hotel stay in {destination}"
        normalized = {
            "mock": True,
            "source_name": "mock_travel",
            "result_type": "hotel",
            "label": label,
            "currency": "USD",
            "nightly_price": 180,
            "provider": "Mock Harbor Hotel",
            "hotel_name": "Mock Harbor Hotel",
            "check_in": query.get("start_date"),
            "check_out": query.get("end_date"),
            "search_reference_url": _search_reference_url("Mock Harbor Hotel", destination, query.get("start_date") or "", query.get("end_date") or ""),
            "link_type": "search_reference",
            "link_label": "Search reference",
            "notes": "Mock Phase 2 hotel result. No live provider was called.",
        }
    elif result_type == "rental_car":
        label = f"Mock Rent-A-Car in {destination}"
        normalized = {
            "mock": True,
            "source_name": "mock_travel",
            "result_type": "rental_car",
            "label": label,
            "currency": "USD",
            "daily_price": 52,
            "provider": "Mock Rent-A-Car",
            "rental_company": "Mock Rent-A-Car",
            "vehicle_label": "Mock compact car",
            "pickup_date": query.get("start_date"),
            "dropoff_date": query.get("end_date"),
            "search_reference_url": _search_reference_url("Mock Rent-A-Car", destination, query.get("start_date") or "", query.get("end_date") or ""),
            "link_type": "search_reference",
            "link_label": "Search reference",
            "notes": "Mock Phase 2 rental car result. No live provider was called.",
        }
    else:
        raise ValueError(f"Unsupported mock result type: {result_type}")

    raw = {
        "mock": True,
        "source": "mock_travel",
        "query_echo": query,
        "payload": normalized,
    }
    return {
        "status": "mock",
        "normalized_result": normalized,
        "raw_result": raw,
    }
