#!/usr/bin/env python
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.location_suggestions import LOCATION_SEEDS, _normalized_key


USEFUL_AIRPORT_TYPES = {"large_airport", "medium_airport", "small_airport"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a local airport autocomplete SQLite index.")
    parser.add_argument("--airports-csv", required=True, help="Path to an OurAirports-style airports.csv file.")
    parser.add_argument("--output", default="data/airport_index.sqlite3", help="Output SQLite database path.")
    parser.add_argument("--with-seed", action="store_true", help="Also insert built-in seed airport rows.")
    return parser.parse_args()


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS airports;
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
        CREATE INDEX idx_airports_iata ON airports(iata_code_normalized);
        CREATE INDEX idx_airports_name ON airports(name_normalized);
        CREATE INDEX idx_airports_municipality ON airports(municipality_normalized);
        CREATE INDEX idx_airports_region ON airports(iso_country, iso_region);
        CREATE INDEX idx_airports_search_text ON airports(search_text);
        """
    )


def should_index(row: dict[str, str]) -> bool:
    iata_code = clean(row.get("iata_code")).upper()
    airport_type = clean(row.get("type")).lower()
    status = clean(row.get("status")).lower()
    if not iata_code:
        return False
    if airport_type == "closed" or status == "closed":
        return False
    return airport_type in USEFUL_AIRPORT_TYPES or airport_type == ""


def clean(value: str | None) -> str:
    return str(value or "").strip()


def optional_float(value: str | None) -> float | None:
    try:
        return float(str(value or "").strip())
    except ValueError:
        return None


def airport_record(row: dict[str, str]) -> tuple:
    name = clean(row.get("name"))
    municipality = clean(row.get("municipality"))
    iso_country = clean(row.get("iso_country")).upper()
    iso_region = clean(row.get("iso_region")).upper()
    iata_code = clean(row.get("iata_code")).upper()
    keywords = clean(row.get("keywords"))
    local_code = clean(row.get("local_code")).upper()
    search_text = _normalized_key(" ".join([iata_code, name, municipality, iso_country, iso_region, local_code, keywords]))
    return (
        clean(row.get("ident")),
        clean(row.get("type")),
        name,
        optional_float(row.get("latitude_deg")),
        optional_float(row.get("longitude_deg")),
        municipality,
        iso_country,
        iso_region,
        iata_code,
        _normalized_key(iata_code),
        local_code,
        keywords,
        _normalized_key(name),
        _normalized_key(municipality),
        search_text,
    )


def seed_records() -> list[tuple]:
    records: list[tuple] = []
    for seed in LOCATION_SEEDS:
        if seed.kind != "airport" or not seed.airport_code:
            continue
        name = seed.airport_name or f"{seed.city} Airport"
        search_text = _normalized_key(" ".join([seed.airport_code, name, seed.city, seed.country, seed.region or "", *seed.aliases]))
        records.append(
            (
                f"seed-{seed.airport_code}",
                "seed",
                name,
                None,
                None,
                seed.city,
                seed.country,
                seed.region or "",
                seed.airport_code,
                _normalized_key(seed.airport_code),
                seed.airport_code,
                " ".join(seed.aliases),
                _normalized_key(name),
                _normalized_key(seed.city),
                search_text,
            )
        )
    return records


def insert_records(conn: sqlite3.Connection, records: list[tuple]) -> None:
    conn.executemany(
        """
        INSERT INTO airports (
            ident, type, name, latitude, longitude, municipality, iso_country, iso_region,
            iata_code, iata_code_normalized, local_code, keywords, name_normalized,
            municipality_normalized, search_text
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        records,
    )


def build_index(airports_csv: Path, output: Path, with_seed: bool = False) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(output) as conn:
        create_schema(conn)
        records: list[tuple] = []
        with airports_csv.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if should_index(row):
                    records.append(airport_record(row))
        if with_seed:
            records.extend(seed_records())
        insert_records(conn, records)
    return len(records)


def main() -> int:
    args = parse_args()
    count = build_index(Path(args.airports_csv), Path(args.output), with_seed=args.with_seed)
    print(f"Indexed {count} airports into {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
