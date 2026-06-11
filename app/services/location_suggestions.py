from __future__ import annotations

from dataclasses import dataclass


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
        parts = [self.city]
        if self.region:
            parts.append(self.region)
        return ", ".join(parts)


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


def _normalized(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _search_text(seed: LocationSeed) -> str:
    parts = [seed.city, seed.region or "", seed.country]
    if seed.airport_code:
        parts.append(seed.airport_code)
    if seed.airport_name:
        parts.append(seed.airport_name)
    parts.extend(seed.aliases)
    return _normalized(" ".join(parts))


def _display_label(seed: LocationSeed) -> str:
    if seed.kind == "airport" and seed.airport_code:
        return f"{seed.city_label} ({seed.airport_code})"
    return seed.city_label


def _value(seed: LocationSeed) -> str:
    if seed.kind == "airport" and seed.airport_code:
        return seed.airport_code
    return seed.city_label


def _rank(seed: LocationSeed, query: str) -> tuple[int, str]:
    q = _normalized(query)
    if seed.airport_code and seed.airport_code.lower() == q:
        return (0, _display_label(seed))
    if _normalized(seed.city).startswith(q):
        return (1 if seed.kind == "city" else 2, _display_label(seed))
    if seed.airport_code and seed.airport_code.lower().startswith(q):
        return (3, _display_label(seed))
    if _search_text(seed).startswith(q):
        return (4, _display_label(seed))
    return (5, _display_label(seed))


def suggestion_for_seed(seed: LocationSeed) -> dict[str, str | None]:
    return {
        "display_label": _display_label(seed),
        "kind": seed.kind,
        "city": seed.city,
        "region": seed.region,
        "state": seed.region,
        "country": seed.country,
        "airport_code": seed.airport_code,
        "iata": seed.airport_code,
        "value": _value(seed),
    }


def suggest_locations(query: str, limit: int = 10) -> list[dict[str, str | None]]:
    q = _normalized(query)
    if not q:
        return []
    matches = [seed for seed in LOCATION_SEEDS if q in _search_text(seed)]
    matches.sort(key=lambda seed: _rank(seed, q))
    return [suggestion_for_seed(seed) for seed in matches[:limit]]
