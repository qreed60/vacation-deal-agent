"""Phase 4D-4 tests: local city and airport autocomplete."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.manifest_io import normalize_manifest
from app.web.routes import form_manifest, location_suggestions


def suggestions_for(query: str) -> list[dict]:
    response = location_suggestions(q=query)
    assert response.status_code == 200
    payload = json.loads(response.body)
    assert isinstance(payload["suggestions"], list)
    return payload["suggestions"]


def test_location_suggest_returns_city_and_airport_suggestions():
    suggestions = suggestions_for("pitt")

    kinds = {suggestion["kind"] for suggestion in suggestions}
    labels = {suggestion["display_label"] for suggestion in suggestions}

    assert "city" in kinds
    assert "airport" in kinds
    assert "Pittsburgh, PA" in labels
    assert "Pittsburgh, PA (PIT)" in labels


@pytest.mark.parametrize(
    ("query", "expected_code"),
    [
        ("pit", "PIT"),
        ("pitt", "PIT"),
        ("min", "MOT"),
        ("ord", "ORD"),
    ],
)
def test_location_suggest_partial_queries_include_expected_airports(query, expected_code):
    suggestions = suggestions_for(query)

    assert any(suggestion["airport_code"] == expected_code for suggestion in suggestions)


def test_airport_suggestion_includes_iata_code():
    suggestions = suggestions_for("ord")
    ord_suggestion = next(suggestion for suggestion in suggestions if suggestion["airport_code"] == "ORD")

    assert ord_suggestion["kind"] == "airport"
    assert ord_suggestion["iata"] == "ORD"
    assert ord_suggestion["value"] == "ORD"
    assert ord_suggestion["city"] == "Chicago"


def test_city_suggestion_works_without_iata():
    suggestions = suggestions_for("chicago")
    city_suggestion = next(suggestion for suggestion in suggestions if suggestion["kind"] == "city")

    assert city_suggestion["airport_code"] is None
    assert city_suggestion["value"] == "Chicago, IL"


def test_new_york_query_includes_ewr_airport_alias():
    suggestions = suggestions_for("new york")

    assert any(suggestion["airport_code"] == "EWR" for suggestion in suggestions)


def test_vacation_form_still_accepts_manual_freeform_values():
    manifest = form_manifest(
        None,
        "Manual Location Trip",
        "active",
        1,
        '[{"name":"Ada","age":""}]',
        "My Home Airport",
        "Somewhere Warm",
        "fixed_dates",
        "2026-07-01",
        "2026-07-07",
        "",
        "",
        "",
        True,
        True,
        False,
        "",
    )
    normalized = normalize_manifest(manifest)

    assert normalized["origin"] == "My Home Airport"
    assert normalized["destination"] == "Somewhere Warm"
