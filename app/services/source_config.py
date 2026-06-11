from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.services.location_suggestions import CITY_DEFAULT_AIRPORT_MAP, LOCATION_SEEDS, load_config as load_location_config


# Small fallback city map for known common values.
_FALLBACK_CITY_MAP: dict[str, str] = {
    **CITY_DEFAULT_AIRPORT_MAP,
}

_STATE_ABBREVIATIONS: dict[str, str] = {
    "ALABAMA": "AL",
    "ALASKA": "AK",
    "ARIZONA": "AZ",
    "ARKANSAS": "AR",
    "CALIFORNIA": "CA",
    "COLORADO": "CO",
    "CONNECTICUT": "CT",
    "DELAWARE": "DE",
    "FLORIDA": "FL",
    "GEORGIA": "GA",
    "HAWAII": "HI",
    "IDAHO": "ID",
    "ILLINOIS": "IL",
    "INDIANA": "IN",
    "IOWA": "IA",
    "KANSAS": "KS",
    "KENTUCKY": "KY",
    "LOUISIANA": "LA",
    "MAINE": "ME",
    "MARYLAND": "MD",
    "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI",
    "MINNESOTA": "MN",
    "MISSISSIPPI": "MS",
    "MISSOURI": "MO",
    "MONTANA": "MT",
    "NEBRASKA": "NE",
    "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM",
    "NEW YORK": "NY",
    "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND",
    "OHIO": "OH",
    "OKLAHOMA": "OK",
    "OREGON": "OR",
    "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN",
    "TEXAS": "TX",
    "UTAH": "UT",
    "VERMONT": "VT",
    "VIRGINIA": "VA",
    "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI",
    "WYOMING": "WY",
    "DISTRICT OF COLUMBIA": "DC",
}

_COUNTRY_SUFFIXES = {"UNITED STATES", "UNITED STATES OF AMERICA", "USA", "US", "U S A", "U S"}


@dataclass(frozen=True)
class ResolutionResult:
    input_value: str
    resolved_airport_code: str | None
    status: str
    source: str
    reason: str


def _is_iata_code(value: str) -> bool:
    """Check if a string looks like a 3-letter IATA airport code."""
    return bool(re.fullmatch(r"[A-Z]{3}", value.strip()))


def _clean_token(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _normalized_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _state_to_abbreviation(value: str | None) -> str | None:
    if not value:
        return None
    token = _clean_token(value).upper().replace(".", "")
    if len(token) == 2 and token.isalpha():
        return token
    return _STATE_ABBREVIATIONS.get(token)


def _candidate_city_state_pairs(value: str) -> list[tuple[str, str | None]]:
    parts = [_clean_token(part) for part in re.split(r",+", value) if _clean_token(part)]
    while parts and _clean_token(parts[-1]).upper().replace(".", "") in _COUNTRY_SUFFIXES:
        parts.pop()
    candidates: list[tuple[str, str | None]] = []
    if parts:
        first = parts[0]
        if re.fullmatch(r"\d{3,}(?:-\d+)?", first) and len(parts) >= 2:
            city = parts[1]
            state = _state_to_abbreviation(parts[2] if len(parts) >= 3 else None)
            candidates.append((city, state))
        else:
            state = _state_to_abbreviation(parts[1] if len(parts) >= 2 else None)
            candidates.append((first, state))
    words = _clean_token(value)
    if words:
        candidates.append((words, None))
    deduped: list[tuple[str, str | None]] = []
    seen: set[tuple[str, str | None]] = set()
    for city, state in candidates:
        key = (_normalized_label(city), state)
        if key not in seen:
            seen.add(key)
            deduped.append((city, state))
    return deduped


def _seed_city_map() -> dict[tuple[str, str | None], str]:
    mapping: dict[tuple[str, str | None], str] = {}
    for seed in LOCATION_SEEDS:
        if seed.kind != "airport" or not seed.airport_code:
            continue
        state = _state_to_abbreviation(seed.region)
        mapping.setdefault((_normalized_label(seed.city), state), seed.airport_code)
        mapping.setdefault((_normalized_label(seed.city), None), seed.airport_code)
        mapping.setdefault((_normalized_label(seed.city_label), None), seed.airport_code)
        for alias in seed.aliases:
            mapping.setdefault((_normalized_label(alias), state), seed.airport_code)
            mapping.setdefault((_normalized_label(alias), None), seed.airport_code)
    for label, code in _FALLBACK_CITY_MAP.items():
        for city, state in _candidate_city_state_pairs(label):
            mapping.setdefault((_normalized_label(city), state), code)
    return mapping


def _resolve_from_seed(value: str) -> str | None:
    mapping = _seed_city_map()
    for city, state in _candidate_city_state_pairs(value):
        key = (_normalized_label(city), state)
        if key in mapping:
            return mapping[key]
        fallback_key = (_normalized_label(city), None)
        if fallback_key in mapping:
            return mapping[fallback_key]
    return None


def _airport_index_type_rank(airport_type: str | None) -> int:
    ranks = {
        "large_airport": 0,
        "medium_airport": 1,
        "small_airport": 2,
    }
    return ranks.get(str(airport_type or ""), 9)


def _resolve_from_airport_index(value: str, config: "SourceConfig | None" = None) -> str | None:
    path = getattr(config, "airport_index_db_path", None) if config is not None else None
    if not path:
        path = load_location_config().airport_index_db_path
    if not path or not Path(path).exists():
        return None
    pairs = _candidate_city_state_pairs(value)
    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT type, municipality, iso_country, iso_region, iata_code
                FROM airports
                WHERE iata_code IS NOT NULL
                  AND iata_code != ''
                  AND municipality_normalized IN ({})
                """.format(",".join("?" for _ in pairs) or "''"),
                tuple(_normalized_label(city) for city, _state in pairs),
            ).fetchall()
    except sqlite3.Error:
        return None
    matches: list[sqlite3.Row] = []
    for row in rows:
        if str(row["iso_country"] or "").upper() not in {"US", "USA", ""}:
            continue
        row_state = _state_to_abbreviation(str(row["iso_region"] or "").split("-")[-1])
        row_city = _normalized_label(str(row["municipality"] or ""))
        for city, state in pairs:
            if row_city != _normalized_label(city):
                continue
            if state and row_state and state != row_state:
                continue
            matches.append(row)
            break
    if not matches:
        return None
    ranked = sorted(matches, key=lambda row: (_airport_index_type_rank(row["type"]), str(row["iata_code"] or "")))
    return str(ranked[0]["iata_code"] or "").strip().upper() or None


def resolve_airport_code(value: str, config: "SourceConfig | None" = None) -> ResolutionResult:
    raw_value = str(value or "").strip()
    if not raw_value:
        return ResolutionResult(raw_value, None, "unresolved", "none", "empty airport/city value")
    direct = raw_value.upper()
    if _is_iata_code(direct):
        return ResolutionResult(raw_value, direct, "direct_iata", "direct_iata", "input is a direct IATA airport code")
    indexed = _resolve_from_airport_index(raw_value, config)
    if indexed:
        return ResolutionResult(raw_value, indexed, "resolved", "airport_index", "matched airport index municipality")
    seeded = _resolve_from_seed(raw_value)
    if seeded:
        return ResolutionResult(raw_value, seeded, "resolved", "seed", "matched seed city/default airport")
    return ResolutionResult(raw_value, None, "unresolved", "none", "no airport match in airport index or seed data")


def resolve_airport(raw_value: str, preferred_airports: list | None = None, alternate_airports: list | None = None) -> str | None:
    """Resolve a raw origin/destination string to an IATA airport code.

    Priority:
      A. First entry in preferred_airports (if non-empty).
      B. First entry in alternate_airports (if preferred is empty).
      C. Raw value if it already looks like an IATA code.
      D. Fallback city map lookup (case-insensitive).
      E. None if unresolved.

    Does not guess beyond the fallback map or call external geocoding APIs.
    """
    # A. preferred_airports
    if preferred_airports:
        first = str(preferred_airports[0]).strip().upper()
        if _is_iata_code(first):
            return first

    # B. alternate_airports
    if alternate_airports:
        first = str(alternate_airports[0]).strip().upper()
        if _is_iata_code(first):
            return first

    result = resolve_airport_code(raw_value)
    return result.resolved_airport_code



def _load_dotenv(path: Path = Path(".env")) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


_DOTENV = _load_dotenv()


def env_value(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is not None:
        return value
    return _DOTENV.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    value = env_value(name, "true" if default else "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    value = env_value(name, str(default)).strip()
    try:
        return float(value)
    except ValueError:
        return default


def env_int_optional(name: str) -> int | None:
    value = env_value(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def env_int(name: str, default: int = 0) -> int:
    value = env_value(name, str(default)).strip()
    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class SourceConfig:
    searxng_enabled: bool
    searxng_base_url: str
    searxng_timeout_seconds: float
    searxng_fallback_enabled: bool
    searxng_max_results: int
    amadeus_enabled: bool
    amadeus_base_url: str
    amadeus_client_id: str
    amadeus_client_secret: str
    amadeus_timeout_seconds: float
    google_places_enabled: bool
    google_places_api_key: str
    google_places_timeout_seconds: float
    serpapi_enabled: bool
    serpapi_api_key: str
    serpapi_base_url: str
    serpapi_timeout_seconds: float
    fast_flights_enabled: bool
    fast_flights_fetch_mode: str
    fast_flights_seat: str
    fast_flights_max_stops: int | None
    fast_flights_max_results: int
    trvl_enabled: bool
    trvl_binary_path: str
    trvl_timeout_seconds: float
    trvl_max_flight_results: int
    trvl_max_hotel_results: int
    trvl_currency: str
    trvl_allow_risky_flight_offers: bool
    trvl_require_configured_currency: bool
    trvl_broad_discovery_enabled: bool
    trvl_broad_include_one_way_fallbacks: bool
    trvl_broad_max_alternatives: int
    trvl_broad_allow_risky_alternatives: bool
    airport_index_db_path: str
    mock_search_enabled: bool
    ai_search_planner_enabled: bool
    ai_search_planner_provider: str
    ai_search_planner_model: str
    ai_search_planner_base_url: str
    ai_search_planner_api_key: str
    ai_search_planner_max_structured_searches: int
    ai_search_planner_max_research_queries: int
    ai_search_planner_allow_date_flex: bool
    ai_search_planner_date_flex_days: int
    ai_search_planner_timeout_seconds: float


def load_source_config() -> SourceConfig:
    return SourceConfig(
        searxng_enabled=env_bool("SEARXNG_ENABLED", True),
        searxng_base_url=env_value("SEARXNG_BASE_URL", "http://127.0.0.1:8888").strip(),
        searxng_timeout_seconds=env_float("SEARXNG_TIMEOUT_SECONDS", 5.0),
        searxng_fallback_enabled=env_bool("SEARXNG_FALLBACK_ENABLED", True),
        searxng_max_results=int(env_value("SEARXNG_MAX_RESULTS", "10")),
        amadeus_enabled=env_bool("AMADEUS_ENABLED", False),
        amadeus_base_url=env_value("AMADEUS_BASE_URL", "https://test.api.amadeus.com").strip().rstrip("/"),
        amadeus_client_id=env_value("AMADEUS_CLIENT_ID", "").strip(),
        amadeus_client_secret=env_value("AMADEUS_CLIENT_SECRET", "").strip(),
        amadeus_timeout_seconds=env_float("AMADEUS_TIMEOUT_SECONDS", 8.0),
        google_places_enabled=env_bool("GOOGLE_PLACES_ENABLED", False),
        google_places_api_key=env_value("GOOGLE_PLACES_API_KEY", "").strip(),
        google_places_timeout_seconds=env_float("GOOGLE_PLACES_TIMEOUT_SECONDS", 8.0),
        serpapi_enabled=env_bool("SERPAPI_ENABLED", False),
        serpapi_api_key=env_value("SERPAPI_API_KEY", "").strip(),
        serpapi_base_url=env_value("SERPAPI_BASE_URL", "https://serpapi.com/search").strip(),
        serpapi_timeout_seconds=env_float("SERPAPI_TIMEOUT_SECONDS", 8.0),
        fast_flights_enabled=env_bool("FAST_FLIGHTS_ENABLED", False),
        fast_flights_fetch_mode=env_value("FAST_FLIGHTS_FETCH_MODE", "common").strip(),
        fast_flights_seat=env_value("FAST_FLIGHTS_SEAT", "economy").strip(),
        fast_flights_max_stops=env_int_optional("FAST_FLIGHTS_MAX_STOPS"),
        fast_flights_max_results=int(env_value("FAST_FLIGHTS_MAX_RESULTS", "20")),
        trvl_enabled=env_bool("TRVL_ENABLED", False),
        trvl_binary_path=env_value("TRVL_BINARY_PATH", ".tools/trvl/trvl").strip(),
        trvl_timeout_seconds=env_float("TRVL_TIMEOUT_SECONDS", 120.0),
        trvl_max_flight_results=int(env_value("TRVL_MAX_FLIGHT_RESULTS", "20")),
        trvl_max_hotel_results=int(env_value("TRVL_MAX_HOTEL_RESULTS", "20")),
        trvl_currency=env_value("TRVL_CURRENCY", "USD").strip() or "USD",
        trvl_allow_risky_flight_offers=env_bool("TRVL_ALLOW_RISKY_FLIGHT_OFFERS", False),
        trvl_require_configured_currency=env_bool("TRVL_REQUIRE_CONFIGURED_CURRENCY", True),
        trvl_broad_discovery_enabled=env_bool("TRVL_BROAD_DISCOVERY_ENABLED", False),
        trvl_broad_include_one_way_fallbacks=env_bool("TRVL_BROAD_INCLUDE_ONE_WAY_FALLBACKS", True),
        trvl_broad_max_alternatives=int(env_value("TRVL_BROAD_MAX_ALTERNATIVES", "50")),
        trvl_broad_allow_risky_alternatives=env_bool("TRVL_BROAD_ALLOW_RISKY_ALTERNATIVES", True),
        airport_index_db_path=env_value("AIRPORT_INDEX_DB_PATH", "data/airport_index.sqlite3").strip(),
        mock_search_enabled=env_bool("MOCK_SEARCH_ENABLED", False),
        ai_search_planner_enabled=env_bool("AI_SEARCH_PLANNER_ENABLED", False),
        ai_search_planner_provider=env_value("AI_SEARCH_PLANNER_PROVIDER", "disabled").strip(),
        ai_search_planner_model=env_value("AI_SEARCH_PLANNER_MODEL", "").strip(),
        ai_search_planner_base_url=env_value("AI_SEARCH_PLANNER_BASE_URL", "").strip(),
        ai_search_planner_api_key=env_value("AI_SEARCH_PLANNER_API_KEY", "").strip(),
        ai_search_planner_max_structured_searches=int(env_value("AI_SEARCH_PLANNER_MAX_STRUCTURED_SEARCHES", "8")),
        ai_search_planner_max_research_queries=int(env_value("AI_SEARCH_PLANNER_MAX_RESEARCH_QUERIES", "5")),
        ai_search_planner_allow_date_flex=env_bool("AI_SEARCH_PLANNER_ALLOW_DATE_FLEX", False),
        ai_search_planner_date_flex_days=int(env_value("AI_SEARCH_PLANNER_DATE_FLEX_DAYS", "1")),
        ai_search_planner_timeout_seconds=env_float("AI_SEARCH_PLANNER_TIMEOUT_SECONDS", 45.0),
    )
