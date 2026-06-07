import json

import pytest

from app.services.manifest_io import ManifestValidationError, normalize_manifest, snapshot_json


def valid_manifest():
    return {
        "title": "Family beach trip",
        "number_of_travelers": 4,
        "origin": "BOS",
        "destination": "San Juan",
        "date_mode": "fixed_dates",
        "start_date": "2026-08-01",
        "end_date": "2026-08-08",
        "hotel_needed": True,
        "airfare_needed": True,
        "rental_car_needed": False,
    }


def test_normalize_manifest_accepts_required_fields():
    normalized = normalize_manifest(valid_manifest())

    assert normalized["title"] == "Family beach trip"
    assert normalized["status"] == "active"
    assert normalized["start_date"].isoformat() == "2026-08-01"


def test_normalize_manifest_rejects_missing_required_fields():
    manifest = valid_manifest()
    del manifest["destination"]

    with pytest.raises(ManifestValidationError, match="Missing required fields: destination"):
        normalize_manifest(manifest)


def test_snapshot_json_is_complete_json():
    normalized = normalize_manifest(valid_manifest())
    payload = json.loads(snapshot_json(normalized))

    assert payload["destination"] == "San Juan"
    assert payload["hotel_needed"] is True
