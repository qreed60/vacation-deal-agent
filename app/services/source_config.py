from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.services.location_suggestions import CITY_DEFAULT_AIRPORT_MAP


# Small fallback city map for known common values.
_FALLBACK_CITY_MAP: dict[str, str] = {
    **CITY_DEFAULT_AIRPORT_MAP,
}


def _is_iata_code(value: str) -> bool:
    """Check if a string looks like a 3-letter IATA airport code."""
    return bool(re.fullmatch(r"[A-Z]{3}", value.strip()))


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

    # C. Raw value is already IATA
    stripped = raw_value.strip().upper()
    if _is_iata_code(stripped):
        return stripped

    # D. Fallback city map (case-insensitive)
    for key, code in _FALLBACK_CITY_MAP.items():
        if raw_value.strip().lower() == key.lower():
            return code

    # E. Unresolved
    return None


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
    searxng_base_url: str
    searxng_timeout_seconds: float
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
    mock_search_enabled: bool


def load_source_config() -> SourceConfig:
    return SourceConfig(
        searxng_base_url=env_value("SEARXNG_BASE_URL", "http://127.0.0.1:8888").strip(),
        searxng_timeout_seconds=env_float("SEARXNG_TIMEOUT_SECONDS", 5.0),
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
        mock_search_enabled=env_bool("MOCK_SEARCH_ENABLED", False),
    )
