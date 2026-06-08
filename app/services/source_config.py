from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
    )
