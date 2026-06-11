from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from sqlmodel import Session, select

from app.db.models import Vacation, utc_now


REQUIRED_FIELDS = {
    "title",
    "number_of_travelers",
    "origin",
    "destination",
    "date_mode",
    "hotel_needed",
    "airfare_needed",
    "rental_car_needed",
}

DATE_MODES = {"fixed_dates", "flexible_window"}
EXPORT_FIELDS = [
    "slug",
    "title",
    "status",
    "number_of_travelers",
    "travelers",
    "origin",
    "destination",
    "date_mode",
    "start_date",
    "end_date",
    "nights_min",
    "nights_target",
    "nights_max",
    "hotel_needed",
    "airfare_needed",
    "rental_car_needed",
    "special_accommodations",
    "preferred_airports",
    "alternate_airports",
]


class ManifestValidationError(ValueError):
    pass


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "vacation"


def unique_slug(session: Session, title: str, preferred_slug: str | None = None, exclude_id: int | None = None) -> str:
    base = slugify(preferred_slug or title)
    candidate = base
    suffix = 2
    while True:
        statement = select(Vacation).where(Vacation.slug == candidate)
        existing = session.exec(statement).first()
        if existing is None or existing.id == exclude_id:
            return candidate
        candidate = f"{base}-{suffix}"
        suffix += 1


def parse_optional_date(value: Any, field_name: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ManifestValidationError(f"{field_name} must be an ISO date") from exc
    raise ManifestValidationError(f"{field_name} must be an ISO date")


def parse_optional_int(value: Any, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ManifestValidationError(f"{field_name} must be an integer") from exc
    if parsed < 0:
        raise ManifestValidationError(f"{field_name} must be zero or greater")
    return parsed


def parse_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ManifestValidationError(f"{field_name} must be true or false")


def normalize_travelers(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ManifestValidationError("travelers must be valid JSON") from exc
        if isinstance(parsed, list):
            return parsed
    raise ManifestValidationError("travelers must be a JSON array")


def normalize_manifest(raw_manifest: dict[str, Any]) -> dict[str, Any]:
    missing = sorted(field for field in REQUIRED_FIELDS if field not in raw_manifest)
    if missing:
        raise ManifestValidationError(f"Missing required fields: {', '.join(missing)}")

    title = str(raw_manifest.get("title", "")).strip()
    if not title:
        raise ManifestValidationError("title is required")

    date_mode = str(raw_manifest.get("date_mode", "")).strip()
    if date_mode not in DATE_MODES:
        raise ManifestValidationError("date_mode must be fixed_dates or flexible_window")

    try:
        number_of_travelers = int(raw_manifest["number_of_travelers"])
    except (TypeError, ValueError) as exc:
        raise ManifestValidationError("number_of_travelers must be an integer") from exc
    if number_of_travelers < 1:
        raise ManifestValidationError("number_of_travelers must be at least 1")

    origin = str(raw_manifest.get("origin", "")).strip()
    destination = str(raw_manifest.get("destination", "")).strip()
    if not origin:
        raise ManifestValidationError("origin is required")
    if not destination:
        raise ManifestValidationError("destination is required")

    manifest = {
        "slug": str(raw_manifest.get("slug", "")).strip() or None,
        "title": title,
        "status": str(raw_manifest.get("status") or "active").strip() or "active",
        "number_of_travelers": number_of_travelers,
        "travelers": normalize_travelers(raw_manifest.get("travelers", raw_manifest.get("travelers_json"))),
        "origin": origin,
        "destination": destination,
        "date_mode": date_mode,
        "start_date": parse_optional_date(raw_manifest.get("start_date"), "start_date"),
        "end_date": parse_optional_date(raw_manifest.get("end_date"), "end_date"),
        "nights_min": parse_optional_int(raw_manifest.get("nights_min"), "nights_min"),
        "nights_target": parse_optional_int(raw_manifest.get("nights_target"), "nights_target"),
        "nights_max": parse_optional_int(raw_manifest.get("nights_max"), "nights_max"),
        "hotel_needed": parse_bool(raw_manifest.get("hotel_needed"), "hotel_needed"),
        "airfare_needed": parse_bool(raw_manifest.get("airfare_needed"), "airfare_needed"),
        "rental_car_needed": parse_bool(raw_manifest.get("rental_car_needed"), "rental_car_needed"),
        "special_accommodations": str(raw_manifest.get("special_accommodations") or ""),
        "preferred_airports": normalize_travelers(raw_manifest.get("preferred_airports", raw_manifest.get("preferred_airports_json"))),
        "alternate_airports": normalize_travelers(raw_manifest.get("alternate_airports", raw_manifest.get("alternate_airports_json"))),
    }
    return manifest


def manifest_for_vacation(vacation: Vacation) -> dict[str, Any]:
    travelers = json.loads(vacation.travelers_json or "[]")
    preferred = json.loads(vacation.preferred_airports_json or "[]")
    alternate = json.loads(vacation.alternate_airports_json or "[]")
    return {
        "slug": vacation.slug,
        "title": vacation.title,
        "status": vacation.status,
        "number_of_travelers": vacation.number_of_travelers,
        "travelers": travelers,
        "origin": vacation.origin,
        "destination": vacation.destination,
        "date_mode": vacation.date_mode,
        "start_date": vacation.start_date.isoformat() if vacation.start_date else None,
        "end_date": vacation.end_date.isoformat() if vacation.end_date else None,
        "nights_min": vacation.nights_min,
        "nights_target": vacation.nights_target,
        "nights_max": vacation.nights_max,
        "hotel_needed": vacation.hotel_needed,
        "airfare_needed": vacation.airfare_needed,
        "rental_car_needed": vacation.rental_car_needed,
        "special_accommodations": vacation.special_accommodations,
        "preferred_airports": preferred,
        "alternate_airports": alternate,
        # Phase 5B schedule fields (exported for UI display)
        "schedule_enabled": bool(vacation.schedule_enabled),
        "searches_per_day": vacation.searches_per_day or 2,
        "last_scheduled_run_at": vacation.last_scheduled_run_at,
        "next_scheduled_run_at": vacation.next_scheduled_run_at,
        "schedule_jitter_minutes": vacation.schedule_jitter_minutes or 20,
        "schedule_paused_reason": vacation.schedule_paused_reason,
    }


def snapshot_json(manifest: dict[str, Any]) -> str:
    serializable = {
        key: (value.isoformat() if isinstance(value, date) else value)
        for key, value in manifest.items()
    }
    return json.dumps(serializable, sort_keys=True)


def vacation_from_manifest(session: Session, raw_manifest: dict[str, Any]) -> Vacation:
    manifest = normalize_manifest(raw_manifest)
    # Extract schedule fields if present (Phase 5B), otherwise use defaults
    sched = manifest.get("schedule", {})
    vacation = Vacation(
        slug=unique_slug(session, manifest["title"], manifest["slug"]),
        title=manifest["title"],
        status=manifest["status"],
        number_of_travelers=manifest["number_of_travelers"],
        travelers_json=json.dumps(manifest["travelers"]),
        origin=manifest["origin"],
        destination=manifest["destination"],
        date_mode=manifest["date_mode"],
        start_date=manifest["start_date"],
        end_date=manifest["end_date"],
        nights_min=manifest["nights_min"],
        nights_target=manifest["nights_target"],
        nights_max=manifest["nights_max"],
        hotel_needed=manifest["hotel_needed"],
        airfare_needed=manifest["airfare_needed"],
        rental_car_needed=manifest["rental_car_needed"],
        special_accommodations=manifest["special_accommodations"],
        preferred_airports_json=json.dumps(manifest.get("preferred_airports", [])),
        alternate_airports_json=json.dumps(manifest.get("alternate_airports", [])),
        # Phase 5B schedule fields (defaults to disabled)
        schedule_enabled=int(sched.get("schedule_enabled", False)),
        searches_per_day=sched.get("searches_per_day", 2) or 2,
        last_scheduled_run_at=None,
        next_scheduled_run_at=None,
        schedule_jitter_minutes=sched.get("schedule_jitter_minutes", 20) or 20,
        schedule_paused_reason=sched.get("schedule_paused_reason"),
        manifest_json=snapshot_json(manifest),
    )
    session.add(vacation)
    session.commit()
    session.refresh(vacation)
    return vacation


def update_vacation_from_manifest(session: Session, vacation: Vacation, raw_manifest: dict[str, Any]) -> Vacation:
    manifest = normalize_manifest(raw_manifest)
    vacation.slug = unique_slug(session, manifest["title"], manifest["slug"] or vacation.slug, exclude_id=vacation.id)
    vacation.title = manifest["title"]
    vacation.status = manifest["status"]
    vacation.number_of_travelers = manifest["number_of_travelers"]
    vacation.travelers_json = json.dumps(manifest["travelers"])
    vacation.origin = manifest["origin"]
    vacation.destination = manifest["destination"]
    vacation.date_mode = manifest["date_mode"]
    vacation.start_date = manifest["start_date"]
    vacation.end_date = manifest["end_date"]
    vacation.nights_min = manifest["nights_min"]
    vacation.nights_target = manifest["nights_target"]
    vacation.nights_max = manifest["nights_max"]
    vacation.hotel_needed = manifest["hotel_needed"]
    vacation.airfare_needed = manifest["airfare_needed"]
    vacation.rental_car_needed = manifest["rental_car_needed"]
    vacation.special_accommodations = manifest["special_accommodations"]
    vacation.preferred_airports_json = json.dumps(manifest.get("preferred_airports", []))
    vacation.alternate_airports_json = json.dumps(manifest.get("alternate_airports", []))
    # Phase 5B: update schedule fields if present in manifest
    sched = manifest.get("schedule") or {}
    if "schedule_enabled" in sched:
        vacation.schedule_enabled = int(sched["schedule_enabled"])
    if "searches_per_day" in sched:
        val = sched["searches_per_day"]
        try:
            vint = int(val)
            vacation.searches_per_day = max(1, min(3, vint))
        except (TypeError, ValueError):
            pass
    if "schedule_paused_reason" in sched:
        vacation.schedule_paused_reason = sched["schedule_paused_reason"] or None
    vacation.manifest_json = snapshot_json(manifest)
    vacation.updated_at = utc_now()
    session.add(vacation)
    session.commit()
    session.refresh(vacation)
    return vacation
