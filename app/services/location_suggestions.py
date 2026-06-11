from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


ALLOWED_PROVIDERS = {"auto", "open_meteo", "airport_index", "seed"}


@dataclass(frozen=True)
class LocationAutocompleteConfig:
    provider: str = "auto"
    limit: int = 10
    min_query_length: int = 2
    cache_enabled: bool = True
    cache_ttl_seconds: int = 86400
    cache_db_path: str = "data/location_autocomplete_cache.sqlite3"
    open_meteo_enabled: bool = True
    open_meteo_base_url: str = "https://geocoding-api.open-meteo.com/v1/search"
    open_meteo_timeout_seconds: float = 6.0
    airport_autocomplete_enabled: bool = True
    airport_index_db_path: str = "data/airport_index.sqlite3"


@dataclass(frozen=True)
class LocationSeed:
    kind: str
    city: str
    region: str | None
    country: str
    airport_code: str | None = None
    airport_name: str | None = None
    aliases: tuple[str, ...] = ()

    @property
    def city_label(self) -> str:
        return ", ".join(part for part in [self.city, self.region] if part)


LOCATION_SEEDS: tuple[LocationSeed, ...] = (
    LocationSeed("city", "Pittsburgh", "PA", "United States"),
    LocationSeed("airport", "Pittsburgh", "PA", "United States", "PIT", "Pittsburgh International Airport"),
    LocationSeed("city", "Minot", "ND", "United States"),
    LocationSeed("airport", "Minot", "ND", "United States", "MOT", "Minot International Airport"),
    LocationSeed("city", "Chicago", "IL", "United States"),
    LocationSeed("airport", "Chicago", "IL", "United States", "ORD", "Chicago O'Hare International Airport"),
    LocationSeed("airport", "Chicago", "IL", "United States", "MDW", "Chicago Midway International Airport"),
    LocationSeed("city", "New York", "NY", "United States"),
    LocationSeed("airport", "New York", "NY", "United States", "JFK", "John F. Kennedy International Airport"),
    LocationSeed("airport", "New York", "NY", "United States", "LGA", "LaGuardia Airport"),
    LocationSeed("airport", "Newark", "NJ", "United States", "EWR", "Newark Liberty International Airport", ("New York", "NYC")),
    LocationSeed("city", "Boston", "MA", "United States"),
    LocationSeed("airport", "Boston", "MA", "United States", "BOS", "Boston Logan International Airport"),
    LocationSeed("city", "Orlando", "FL", "United States"),
    LocationSeed("airport", "Orlando", "FL", "United States", "MCO", "Orlando International Airport"),
    LocationSeed("city", "Los Angeles", "CA", "United States"),
    LocationSeed("airport", "Los Angeles", "CA", "United States", "LAX", "Los Angeles International Airport"),
    LocationSeed("city", "Dallas", "TX", "United States"),
    LocationSeed("airport", "Dallas", "TX", "United States", "DFW", "Dallas Fort Worth International Airport"),
    LocationSeed("airport", "Dallas", "TX", "United States", "DAL", "Dallas Love Field"),
    LocationSeed("city", "Atlanta", "GA", "United States"),
    LocationSeed("airport", "Atlanta", "GA", "United States", "ATL", "Hartsfield-Jackson Atlanta International Airport"),
    LocationSeed("city", "Denver", "CO", "United States"),
    LocationSeed("airport", "Denver", "CO", "United States", "DEN", "Denver International Airport"),
    LocationSeed("city", "Minneapolis", "MN", "United States"),
    LocationSeed("airport", "Minneapolis", "MN", "United States", "MSP", "Minneapolis-Saint Paul International Airport"),
)


CITY_DEFAULT_AIRPORT_MAP: dict[str, str] = {}
for city_seed in LOCATION_SEEDS:
    if city_seed.kind != "city":
        continue
    default_airport = next(
        (
            airport.airport_code
            for airport in LOCATION_SEEDS
            if airport.kind == "airport"
            and airport.city == city_seed.city
            and airport.region == city_seed.region
            and airport.airport_code
        ),
        None,
    )
    if default_airport:
        CITY_DEFAULT_AIRPORT_MAP[city_seed.city] = default_airport
        CITY_DEFAULT_AIRPORT_MAP[city_seed.city_label] = default_airport


def load_config() -> LocationAutocompleteConfig:
    provider = os.getenv("LOCATION_AUTOCOMPLETE_PROVIDER", "auto").strip().lower() or "auto"
    if provider not in ALLOWED_PROVIDERS:
        provider = "auto"
    return LocationAutocompleteConfig(
        provider=provider,
        limit=_env_int("LOCATION_AUTOCOMPLETE_LIMIT", 10),
        min_query_length=_env_int("LOCATION_AUTOCOMPLETE_MIN_QUERY_LENGTH", 2),
        cache_enabled=_env_bool("LOCATION_AUTOCOMPLETE_CACHE_ENABLED", True),
        cache_ttl_seconds=_env_int("LOCATION_AUTOCOMPLETE_CACHE_TTL_SECONDS", 86400),
        cache_db_path=os.getenv("LOCATION_AUTOCOMPLETE_CACHE_DB_PATH", "data/location_autocomplete_cache.sqlite3"),
        open_meteo_enabled=_env_bool("OPEN_METEO_GEOCODING_ENABLED", True),
        open_meteo_base_url=os.getenv("OPEN_METEO_GEOCODING_BASE_URL", "https://geocoding-api.open-meteo.com/v1/search"),
        open_meteo_timeout_seconds=_env_float("OPEN_METEO_GEOCODING_TIMEOUT_SECONDS", 6.0),
        airport_autocomplete_enabled=_env_bool("AIRPORT_AUTOCOMPLETE_ENABLED", True),
        airport_index_db_path=os.getenv("AIRPORT_INDEX_DB_PATH", "data/airport_index.sqlite3"),
    )


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _normalized(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _is_postal_query(query: str) -> bool:
    q = query.strip()
    return bool(re.search(r"\d", q)) and len(q) >= 2


def _clean_label(parts: list[Any]) -> str:
    clean_parts: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = str(part or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        clean_parts.append(text)
    return ", ".join(clean_parts)


def _suggestion(
    *,
    display_label: str,
    kind: str,
    city: str | None = None,
    region: str | None = None,
    country: str | None = None,
    postal_code: str | None = None,
    airport_code: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    value: str | None = None,
    source: str,
    score: float = 0.0,
    population: int | None = None,
) -> dict[str, Any]:
    return {
        "display_label": display_label,
        "kind": kind,
        "city": city,
        "region": region,
        "state": region,
        "country": country,
        "postal_code": postal_code,
        "airport_code": airport_code,
        "iata": airport_code,
        "latitude": latitude,
        "longitude": longitude,
        "value": value or display_label,
        "source": source,
        "score": score,
        "population": population,
    }


def _search_text(seed: LocationSeed) -> str:
    parts = [seed.city, seed.region or "", seed.country]
    if seed.airport_code:
        parts.append(seed.airport_code)
    if seed.airport_name:
        parts.append(seed.airport_name)
    parts.extend(seed.aliases)
    return _normalized(" ".join(parts))


def _seed_display_label(seed: LocationSeed) -> str:
    if seed.kind == "airport" and seed.airport_code:
        return f"{seed.city_label} ({seed.airport_code})"
    return seed.city_label


def _seed_value(seed: LocationSeed) -> str:
    if seed.kind == "airport" and seed.airport_code:
        return seed.airport_code
    return seed.city_label


def _base_rank(suggestion: dict[str, Any], query: str) -> float:
    q = _normalized(query)
    q_compact = re.sub(r"[^a-z0-9]", "", q)
    kind = suggestion.get("kind")
    city = _normalized(str(suggestion.get("city") or ""))
    postal = _normalized(str(suggestion.get("postal_code") or ""))
    airport_code = _normalized(str(suggestion.get("airport_code") or ""))
    display = _normalized(str(suggestion.get("display_label") or ""))
    source = suggestion.get("source")
    score = 0.0
    if kind == "airport" and airport_code and airport_code == q:
        score += 10000
    elif kind == "airport" and airport_code and airport_code.startswith(q):
        score += 8500
    if kind == "city" and city == q:
        score += 7600
    elif kind == "city" and city.startswith(q):
        score += 7000
    if kind == "postal" and postal and (postal == q or postal.replace(" ", "") == q_compact):
        score += 7400
    elif kind == "postal" and postal and postal.startswith(q):
        score += 6900
    if display.startswith(q):
        score += 1200
    if q and q in display:
        score += 500
    if source == "airport_index":
        score += 120
    elif source == "open_meteo":
        score += 80
    elif source == "seed":
        score += 40
    population = suggestion.get("population")
    if isinstance(population, int) and population > 0:
        score += min(1800, population / 1000)
    return score


def suggestion_for_seed(seed: LocationSeed, query: str = "") -> dict[str, Any]:
    display_label = _seed_display_label(seed)
    suggestion = _suggestion(
        display_label=display_label,
        kind=seed.kind,
        city=seed.city,
        region=seed.region,
        country=seed.country,
        airport_code=seed.airport_code,
        value=_seed_value(seed),
        source="seed",
    )
    suggestion["score"] = _base_rank(suggestion, query)
    return suggestion


def suggest_seed_locations(query: str, limit: int = 10) -> list[dict[str, Any]]:
    q = _normalized(query)
    if not q:
        return []
    matches = [seed for seed in LOCATION_SEEDS if q in _search_text(seed)]
    suggestions = [suggestion_for_seed(seed, q) for seed in matches]
    return _rank_and_dedupe(suggestions, q, limit)


def _cache_connect(path: str) -> sqlite3.Connection:
    db_path = Path(path)
    if db_path.parent and str(db_path.parent) != ".":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS location_autocomplete_cache (
            cache_key TEXT PRIMARY KEY,
            expires_at INTEGER NOT NULL,
            value_json TEXT NOT NULL
        )
        """
    )
    return conn


def _cache_get(config: LocationAutocompleteConfig, cache_key: str) -> list[dict[str, Any]] | None:
    if not config.cache_enabled:
        return None
    try:
        with _cache_connect(config.cache_db_path) as conn:
            row = conn.execute(
                "SELECT expires_at, value_json FROM location_autocomplete_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    expires_at, value_json = row
    if int(expires_at) < int(time.time()):
        return None
    try:
        parsed = json.loads(value_json)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def _cache_set(config: LocationAutocompleteConfig, cache_key: str, value: list[dict[str, Any]]) -> None:
    if not config.cache_enabled:
        return
    expires_at = int(time.time()) + max(1, config.cache_ttl_seconds)
    try:
        with _cache_connect(config.cache_db_path) as conn:
            conn.execute(
                """
                INSERT INTO location_autocomplete_cache (cache_key, expires_at, value_json)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET expires_at = excluded.expires_at, value_json = excluded.value_json
                """,
                (cache_key, expires_at, json.dumps(value)),
            )
    except sqlite3.Error:
        return


def _open_meteo_cache_key(query: str, limit: int) -> str:
    return f"open_meteo:v1:{_normalized(query)}:{limit}"


def suggest_open_meteo_locations(
    query: str,
    limit: int,
    config: LocationAutocompleteConfig | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    config = config or load_config()
    if not config.open_meteo_enabled:
        return [], False
    cache_key = _open_meteo_cache_key(query, limit)
    cached = _cache_get(config, cache_key)
    if cached is not None:
        return cached[:limit], True
    try:
        response = httpx.get(
            config.open_meteo_base_url,
            params={"name": query, "count": max(limit, 10), "language": "en", "format": "json"},
            timeout=config.open_meteo_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return [], False
    raw_results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(raw_results, list):
        return [], False
    suggestions = _normalize_open_meteo_results(raw_results, query)
    suggestions = _rank_and_dedupe(suggestions, query, limit)
    _cache_set(config, cache_key, suggestions)
    return suggestions, False


def _normalize_open_meteo_results(results: list[Any], query: str) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    postal_query = _is_postal_query(query)
    for result in results:
        if not isinstance(result, dict):
            continue
        name = _first_string(result, ("name", "city", "place_name"))
        country = _first_string(result, ("country", "country_code"))
        region = _first_string(result, ("admin1", "admin2", "region"))
        postcodes = result.get("postcodes") or result.get("post_codes") or result.get("postal_codes")
        explicit_postal_code = _first_string(result, ("postcode", "postal_code", "zip"))
        postal_code = explicit_postal_code or _first_postal_code(postcodes)
        kind = "postal" if postal_code and (postal_query or explicit_postal_code) else "city"
        if kind == "postal":
            display_label = _clean_label([postal_code, name, region, country])
            value = _clean_label([postal_code, name, region, country])
        else:
            display_label = _clean_label([name, region, country])
            value = display_label
        if not display_label:
            continue
        suggestion = _suggestion(
            display_label=display_label,
            kind=kind,
            city=name,
            region=region,
            country=country,
            postal_code=postal_code,
            airport_code=None,
            latitude=_optional_float(result.get("latitude")),
            longitude=_optional_float(result.get("longitude")),
            value=value,
            source="open_meteo",
            population=_optional_int(result.get("population")),
        )
        suggestion["score"] = _base_rank(suggestion, query)
        suggestions.append(suggestion)
    return suggestions


def _first_string(mapping: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _first_postal_code(value: Any) -> str | None:
    if isinstance(value, list):
        for item in value:
            text = str(item or "").strip()
            if text:
                return text
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def airport_index_available(config: LocationAutocompleteConfig | None = None) -> bool:
    config = config or load_config()
    return bool(config.airport_autocomplete_enabled and Path(config.airport_index_db_path).exists())


def suggest_airport_index_locations(
    query: str,
    limit: int,
    config: LocationAutocompleteConfig | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    config = config or load_config()
    if not airport_index_available(config):
        return [], False
    like = f"%{_normalized_key(query)}%"
    prefix = f"{_normalized_key(query)}%"
    try:
        with sqlite3.connect(config.airport_index_db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT ident, type, name, latitude, longitude, municipality, iso_country,
                       iso_region, iata_code, local_code, keywords, search_text
                FROM airports
                WHERE iata_code_normalized LIKE ?
                   OR name_normalized LIKE ?
                   OR municipality_normalized LIKE ?
                   OR search_text LIKE ?
                LIMIT ?
                """,
                (prefix, like, like, like, max(limit * 4, 20)),
            ).fetchall()
    except sqlite3.Error:
        return [], False
    suggestions: list[dict[str, Any]] = []
    for row in rows:
        airport_code = str(row["iata_code"] or "").strip().upper()
        if not airport_code:
            continue
        city = str(row["municipality"] or "").strip() or None
        country = str(row["iso_country"] or "").strip() or None
        region = str(row["iso_region"] or "").strip() or None
        name = str(row["name"] or "").strip()
        display_label = _clean_label([name, f"({airport_code})", city, country]).replace(", (", " (")
        suggestion = _suggestion(
            display_label=display_label,
            kind="airport",
            city=city,
            region=region,
            country=country,
            airport_code=airport_code,
            latitude=_optional_float(row["latitude"]),
            longitude=_optional_float(row["longitude"]),
            value=airport_code,
            source="airport_index",
        )
        suggestion["score"] = _base_rank(suggestion, query)
        suggestions.append(suggestion)
    return _rank_and_dedupe(suggestions, query, limit), False


def _dedupe_key(suggestion: dict[str, Any]) -> str:
    if suggestion.get("kind") == "airport" and suggestion.get("airport_code"):
        return f"airport:{suggestion['airport_code']}"
    if suggestion.get("kind") == "postal" and suggestion.get("postal_code"):
        return "postal:" + _normalized_key(
            _clean_label([suggestion.get("postal_code"), suggestion.get("city"), suggestion.get("region"), suggestion.get("country")])
        )
    return "place:" + _normalized_key(
        _clean_label([suggestion.get("city") or suggestion.get("display_label"), suggestion.get("region"), suggestion.get("country")])
    )


def _rank_and_dedupe(suggestions: list[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for suggestion in suggestions:
        suggestion["score"] = _base_rank(suggestion, query)
        key = _dedupe_key(suggestion)
        if key not in best or float(suggestion["score"]) > float(best[key].get("score") or 0):
            best[key] = suggestion
    ranked = sorted(
        best.values(),
        key=lambda item: (-float(item.get("score") or 0), str(item.get("display_label") or "")),
    )
    return ranked[: max(0, limit)]


def suggest_locations_response(query: str, provider: str | None = None, limit: int | None = None) -> dict[str, Any]:
    config = load_config()
    selected_provider = (provider or config.provider).strip().lower()
    if selected_provider not in ALLOWED_PROVIDERS:
        selected_provider = config.provider
    selected_limit = max(1, min(int(limit or config.limit), 50))
    q = str(query or "").strip()
    if len(q) < config.min_query_length:
        return {
            "query": q,
            "provider": selected_provider,
            "fallback_used": False,
            "cached": False,
            "suggestions": [],
        }

    cached = False
    fallback_used = False
    suggestions: list[dict[str, Any]] = []

    if selected_provider == "seed":
        suggestions = suggest_seed_locations(q, selected_limit)
    elif selected_provider == "open_meteo":
        suggestions, cached = suggest_open_meteo_locations(q, selected_limit, config)
    elif selected_provider == "airport_index":
        suggestions, cached = suggest_airport_index_locations(q, selected_limit, config)
        if not airport_index_available(config):
            suggestions = suggest_seed_locations(q, selected_limit)
            fallback_used = bool(suggestions)
    else:
        open_meteo_suggestions, cached = suggest_open_meteo_locations(q, selected_limit, config)
        airport_suggestions, _ = suggest_airport_index_locations(q, selected_limit, config)
        suggestions = _rank_and_dedupe(open_meteo_suggestions + airport_suggestions, q, selected_limit)
        if not suggestions:
            suggestions = suggest_seed_locations(q, selected_limit)
            fallback_used = bool(suggestions)

    return {
        "query": q,
        "provider": selected_provider,
        "fallback_used": fallback_used,
        "cached": cached,
        "suggestions": suggestions,
    }


def suggest_locations(query: str, limit: int = 10) -> list[dict[str, Any]]:
    return suggest_locations_response(query, provider="seed", limit=limit)["suggestions"]
