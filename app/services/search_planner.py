from __future__ import annotations

import json
from typing import Any

from app.db.models import Vacation
from app.services.manifest_io import manifest_for_vacation


def deterministic_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def build_search_plan(vacation: Vacation) -> dict[str, Any]:
    manifest = manifest_for_vacation(vacation)
    base_context = {
        "origin": manifest["origin"],
        "destination": manifest["destination"],
        "date_mode": manifest["date_mode"],
        "start_date": manifest["start_date"],
        "end_date": manifest["end_date"],
        "nights_min": manifest["nights_min"],
        "nights_target": manifest["nights_target"],
        "nights_max": manifest["nights_max"],
        "number_of_travelers": manifest["number_of_travelers"],
        "travelers": manifest["travelers"],
        "special_accommodations": manifest["special_accommodations"],
    }
    queries: list[dict[str, Any]] = []

    if manifest["airfare_needed"]:
        queries.append(
            {
                "source_name": "mock_travel",
                "result_type": "flight",
                "query": {
                    **base_context,
                    "service": "flight",
                    "mock": True,
                },
            }
        )
    if manifest["hotel_needed"]:
        queries.append(
            {
                "source_name": "mock_travel",
                "result_type": "hotel",
                "query": {
                    **base_context,
                    "service": "hotel",
                    "mock": True,
                },
            }
        )
    if manifest["rental_car_needed"]:
        queries.append(
            {
                "source_name": "mock_travel",
                "result_type": "rental_car",
                "query": {
                    **base_context,
                    "service": "rental_car",
                    "mock": True,
                },
            }
        )

    return {
        "planner_version": 1,
        "vacation": {
            "id": vacation.id,
            "slug": vacation.slug,
            "title": vacation.title,
            "status": vacation.status,
        },
        "requested_services": {
            "flight": manifest["airfare_needed"],
            "hotel": manifest["hotel_needed"],
            "rental_car": manifest["rental_car_needed"],
        },
        "queries": queries,
    }
