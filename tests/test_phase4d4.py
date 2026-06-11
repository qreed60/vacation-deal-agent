"""Phase 4D-4 tests: local city and airport autocomplete."""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services import location_suggestions as location_service
from app.services.manifest_io import normalize_manifest
from app.services.source_config import resolve_airport_code
from app.web.routes import form_manifest, location_suggestions
from scripts.build_airport_index import build_index


class FakeOpenMeteoResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self.payload


def suggestions_for(query: str, provider: str = "seed", limit: int | None = None) -> list[dict]:
    response = location_suggestions(q=query, provider=provider, limit=limit)
    assert response.status_code == 200
    payload = json.loads(response.body)
    assert isinstance(payload["suggestions"], list)
    return payload["suggestions"]


def response_payload(query: str, provider: str = "seed", limit: int | None = None) -> dict:
    response = location_suggestions(q=query, provider=provider, limit=limit)
    assert response.status_code == 200
    return json.loads(response.body)


@pytest.fixture(autouse=True)
def disable_location_cache(monkeypatch):
    monkeypatch.setenv("LOCATION_AUTOCOMPLETE_CACHE_ENABLED", "false")
    monkeypatch.setenv("OPEN_METEO_GEOCODING_ENABLED", "true")
    monkeypatch.setenv("AIRPORT_AUTOCOMPLETE_ENABLED", "true")
    monkeypatch.delenv("LOCATION_AUTOCOMPLETE_PROVIDER", raising=False)


def write_temp_airport_index(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE airports (
                ident TEXT,
                type TEXT,
                name TEXT NOT NULL,
                latitude REAL,
                longitude REAL,
                municipality TEXT,
                iso_country TEXT,
                iso_region TEXT,
                iata_code TEXT NOT NULL,
                iata_code_normalized TEXT NOT NULL,
                local_code TEXT,
                keywords TEXT,
                name_normalized TEXT NOT NULL,
                municipality_normalized TEXT NOT NULL,
                search_text TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO airports (
                ident, type, name, latitude, longitude, municipality, iso_country,
                iso_region, iata_code, iata_code_normalized, local_code, keywords,
                name_normalized, municipality_normalized, search_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "KPIT",
                "large_airport",
                "Pittsburgh International Airport",
                40.4915,
                -80.2329,
                "Pittsburgh",
                "US",
                "US-PA",
                "PIT",
                "pit",
                "PIT",
                "Pittsburgh",
                "pittsburgh international airport",
                "pittsburgh",
                "pit pittsburgh international airport pittsburgh us us pa",
            ),
        )
        conn.execute(
            """
            INSERT INTO airports (
                ident, type, name, latitude, longitude, municipality, iso_country,
                iso_region, iata_code, iata_code_normalized, local_code, keywords,
                name_normalized, municipality_normalized, search_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "LFPG",
                "large_airport",
                "Charles de Gaulle Airport",
                49.0097,
                2.5479,
                "Paris",
                "FR",
                "FR-IDF",
                "CDG",
                "cdg",
                "CDG",
                "Paris",
                "charles de gaulle airport",
                "paris",
                "cdg charles de gaulle airport paris fr fr idf",
            ),
        )


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


def test_seed_provider_still_returns_pit_mot_ord():
    for query, code in [("pit", "PIT"), ("mot", "MOT"), ("ord", "ORD")]:
        suggestions = suggestions_for(query, provider="seed")
        assert any(suggestion["airport_code"] == code for suggestion in suggestions)


def test_auto_falls_back_to_seed_when_open_meteo_fails_and_no_airport_index(monkeypatch, tmp_path):
    monkeypatch.setenv("AIRPORT_INDEX_DB_PATH", str(tmp_path / "missing.sqlite3"))
    monkeypatch.setattr(location_service.httpx, "get", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("down")))

    payload = response_payload("pit", provider="auto")

    assert payload["fallback_used"] is True
    assert any(suggestion["airport_code"] == "PIT" for suggestion in payload["suggestions"])


def test_open_meteo_provider_normalizes_global_city_results(monkeypatch):
    def fake_get(*args, **kwargs):
        return FakeOpenMeteoResponse(
            {
                "results": [
                    {
                        "name": "Paris",
                        "admin1": "Ile-de-France",
                        "country": "France",
                        "latitude": 48.8534,
                        "longitude": 2.3488,
                        "population": 2138551,
                    }
                ]
            }
        )

    monkeypatch.setattr(location_service.httpx, "get", fake_get)

    suggestions = suggestions_for("par", provider="open_meteo")

    assert suggestions[0]["kind"] == "city"
    assert suggestions[0]["display_label"] == "Paris, Ile-de-France, France"
    assert suggestions[0]["value"] == "Paris, Ile-de-France, France"
    assert suggestions[0]["source"] == "open_meteo"
    assert suggestions[0]["airport_code"] is None


def test_open_meteo_provider_normalizes_postal_results(monkeypatch):
    def fake_get(*args, **kwargs):
        return FakeOpenMeteoResponse(
            {
                "results": [
                    {
                        "name": "Pittsburgh",
                        "admin1": "Pennsylvania",
                        "country": "United States",
                        "postal_code": "15212",
                        "latitude": 40.46,
                        "longitude": -80.01,
                    }
                ]
            }
        )

    monkeypatch.setattr(location_service.httpx, "get", fake_get)

    suggestions = suggestions_for("15212", provider="open_meteo")

    assert suggestions[0]["kind"] == "postal"
    assert suggestions[0]["postal_code"] == "15212"
    assert suggestions[0]["display_label"] == "15212, Pittsburgh, Pennsylvania, United States"
    assert suggestions[0]["value"] == "15212, Pittsburgh, Pennsylvania, United States"


def test_endpoint_supports_provider_open_meteo_with_mocked_response(monkeypatch):
    monkeypatch.setattr(
        location_service.httpx,
        "get",
        lambda *args, **kwargs: FakeOpenMeteoResponse({"results": [{"name": "Tokyo", "country": "Japan"}]}),
    )

    payload = response_payload("tok", provider="open_meteo")

    assert payload["provider"] == "open_meteo"
    assert payload["suggestions"][0]["display_label"] == "Tokyo, Japan"


def test_endpoint_supports_provider_seed():
    payload = response_payload("ord", provider="seed")

    assert payload["provider"] == "seed"
    assert payload["suggestions"][0]["airport_code"] == "ORD"


def test_auto_mode_returns_mixed_city_postal_airport_suggestions(monkeypatch, tmp_path):
    index_path = tmp_path / "airports.sqlite3"
    write_temp_airport_index(index_path)
    monkeypatch.setenv("AIRPORT_INDEX_DB_PATH", str(index_path))

    def fake_get(*args, **kwargs):
        return FakeOpenMeteoResponse(
            {
                "results": [
                    {"name": "Paris", "admin1": "Ile-de-France", "country": "France", "population": 2138551},
                    {"name": "Paris 75001", "country": "France", "postal_code": "75001"},
                ]
            }
        )

    monkeypatch.setattr(location_service.httpx, "get", fake_get)

    suggestions = suggestions_for("par", provider="auto", limit=10)
    kinds = {suggestion["kind"] for suggestion in suggestions}
    sources = {suggestion["source"] for suggestion in suggestions}

    assert {"city", "postal", "airport"}.issubset(kinds)
    assert {"open_meteo", "airport_index"}.issubset(sources)


def test_airport_index_provider_returns_iata_suggestions(monkeypatch, tmp_path):
    index_path = tmp_path / "airports.sqlite3"
    write_temp_airport_index(index_path)
    monkeypatch.setenv("AIRPORT_INDEX_DB_PATH", str(index_path))

    suggestions = suggestions_for("pit", provider="airport_index")

    assert suggestions[0]["kind"] == "airport"
    assert suggestions[0]["airport_code"] == "PIT"
    assert suggestions[0]["value"] == "PIT"
    assert suggestions[0]["source"] == "airport_index"


def test_build_airport_index_imports_ourairports_rows(tmp_path):
    csv_path = tmp_path / "airports.csv"
    output_path = tmp_path / "airport_index.sqlite3"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "ident",
                "type",
                "name",
                "latitude_deg",
                "longitude_deg",
                "municipality",
                "iso_country",
                "iso_region",
                "iata_code",
                "local_code",
                "keywords",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "ident": "KPIT",
                "type": "large_airport",
                "name": "Pittsburgh International Airport",
                "latitude_deg": "40.4915",
                "longitude_deg": "-80.2329",
                "municipality": "Pittsburgh",
                "iso_country": "US",
                "iso_region": "US-PA",
                "iata_code": "PIT",
                "local_code": "PIT",
                "keywords": "Pittsburgh",
            }
        )
        writer.writerow(
            {
                "ident": "ZZZZ",
                "type": "closed",
                "name": "Closed Airport",
                "iata_code": "ZZZ",
            }
        )

    count = build_index(csv_path, output_path, with_seed=True)

    assert count > 1
    with sqlite3.connect(output_path) as conn:
        row = conn.execute("SELECT name, iata_code FROM airports WHERE iata_code = 'PIT'").fetchone()
        closed = conn.execute("SELECT iata_code FROM airports WHERE iata_code = 'ZZZ'").fetchone()
    assert row == ("Pittsburgh International Airport", "PIT")
    assert closed is None


def test_exact_iata_match_ranks_above_loose_city_matches(monkeypatch, tmp_path):
    index_path = tmp_path / "airports.sqlite3"
    write_temp_airport_index(index_path)
    monkeypatch.setenv("AIRPORT_INDEX_DB_PATH", str(index_path))

    def fake_get(*args, **kwargs):
        return FakeOpenMeteoResponse({"results": [{"name": "Pitangui", "country": "Brazil", "population": 20000}]})

    monkeypatch.setattr(location_service.httpx, "get", fake_get)

    suggestions = suggestions_for("pit", provider="auto")

    assert suggestions[0]["kind"] == "airport"
    assert suggestions[0]["airport_code"] == "PIT"


def test_popular_city_ranks_above_obscure_exact_name_match(monkeypatch):
    def fake_get(*args, **kwargs):
        return FakeOpenMeteoResponse(
            {
                "results": [
                    {"name": "Par", "admin1": "England", "country": "United Kingdom", "population": 9462},
                    {"name": "Paris", "admin1": "Ile-de-France", "country": "France", "population": 2138551},
                ]
            }
        )

    monkeypatch.setattr(location_service.httpx, "get", fake_get)

    suggestions = suggestions_for("par", provider="open_meteo")

    assert suggestions[0]["city"] == "Paris"


def test_cache_returns_cached_open_meteo_response_when_enabled(monkeypatch, tmp_path):
    cache_path = tmp_path / "cache.sqlite3"
    monkeypatch.setenv("LOCATION_AUTOCOMPLETE_CACHE_ENABLED", "true")
    monkeypatch.setenv("LOCATION_AUTOCOMPLETE_CACHE_DB_PATH", str(cache_path))
    calls = {"count": 0}

    def fake_get(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] > 1:
            raise RuntimeError("network unavailable")
        return FakeOpenMeteoResponse({"results": [{"name": "Paris", "country": "France"}]})

    monkeypatch.setattr(location_service.httpx, "get", fake_get)

    first = response_payload("par", provider="open_meteo")
    second = response_payload("par", provider="open_meteo")

    assert first["cached"] is False
    assert second["cached"] is True
    assert second["suggestions"][0]["display_label"] == "Paris, France"
    assert calls["count"] == 1


def test_missing_or_failed_providers_do_not_500(monkeypatch, tmp_path):
    monkeypatch.setenv("AIRPORT_INDEX_DB_PATH", str(tmp_path / "missing.sqlite3"))
    monkeypatch.setattr(location_service.httpx, "get", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("down")))

    open_meteo_payload = response_payload("par", provider="open_meteo")
    airport_payload = response_payload("pit", provider="airport_index")
    auto_payload = response_payload("pit", provider="auto")

    assert open_meteo_payload["suggestions"] == []
    assert any(suggestion["airport_code"] == "PIT" for suggestion in airport_payload["suggestions"])
    assert any(suggestion["airport_code"] == "PIT" for suggestion in auto_payload["suggestions"])


def test_vacation_form_ui_handles_new_suggestion_shape():
    template = Path("app/web/templates/vacation_form.html").read_text(encoding="utf-8")

    assert "suggestion.airport_code" in template
    assert "suggestion.value || suggestion.display_label" in template
    assert "payload.suggestions || []" in template


@pytest.mark.parametrize(
    ("value", "expected_code", "expected_status", "expected_source"),
    [
        ("PIT", "PIT", "direct_iata", "direct_iata"),
        ("MOT", "MOT", "direct_iata", "direct_iata"),
        ("ORD", "ORD", "direct_iata", "direct_iata"),
        ("Pittsburgh, Pennsylvania, United States", "PIT", "resolved", "seed"),
        ("Pittsburgh, PA", "PIT", "resolved", "seed"),
        ("Minot, North Dakota, United States", "MOT", "resolved", "seed"),
        ("Minot, ND", "MOT", "resolved", "seed"),
        ("Chicago, Illinois, United States", "ORD", "resolved", "seed"),
    ],
)
def test_resolve_airport_code_handles_iata_and_open_meteo_style_city_labels(
    monkeypatch,
    tmp_path,
    value,
    expected_code,
    expected_status,
    expected_source,
):
    monkeypatch.setenv("AIRPORT_INDEX_DB_PATH", str(tmp_path / "missing.sqlite3"))

    result = resolve_airport_code(value)

    assert result.resolved_airport_code == expected_code
    assert result.status == expected_status
    assert result.source == expected_source


def test_resolve_airport_code_prefers_airport_index_when_available(monkeypatch, tmp_path):
    index_path = tmp_path / "airports.sqlite3"
    write_temp_airport_index(index_path)
    monkeypatch.setenv("AIRPORT_INDEX_DB_PATH", str(index_path))

    result = resolve_airport_code("Pittsburgh, Pennsylvania, United States")

    assert result.resolved_airport_code == "PIT"
    assert result.status == "resolved"
    assert result.source == "airport_index"


def test_resolve_airport_code_unresolved_is_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("AIRPORT_INDEX_DB_PATH", str(tmp_path / "missing.sqlite3"))

    result = resolve_airport_code("Unknownville, ZZ, United States")

    assert result.resolved_airport_code is None
    assert result.status == "unresolved"
    assert result.source == "none"
