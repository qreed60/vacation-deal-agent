from __future__ import annotations

from typing import Any


def search(query_entry: dict[str, Any]) -> dict[str, Any]:
    result_type = query_entry["result_type"]
    query = query_entry["query"]
    destination = query["destination"]

    if result_type == "flight":
        label = f"MOCK flight {query['origin']} to {destination}"
        normalized = {
            "mock": True,
            "result_type": "flight",
            "label": label,
            "currency": "USD",
            "total_price": 425,
            "provider": "mock_travel",
            "notes": "Mock Phase 2 flight result. No live provider was called.",
        }
    elif result_type == "hotel":
        label = f"MOCK hotel stay in {destination}"
        normalized = {
            "mock": True,
            "result_type": "hotel",
            "label": label,
            "currency": "USD",
            "nightly_price": 180,
            "provider": "mock_travel",
            "notes": "Mock Phase 2 hotel result. No live provider was called.",
        }
    elif result_type == "rental_car":
        label = f"MOCK rental car in {destination}"
        normalized = {
            "mock": True,
            "result_type": "rental_car",
            "label": label,
            "currency": "USD",
            "daily_price": 52,
            "provider": "mock_travel",
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
