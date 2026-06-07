from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlmodel import Session

from app.db.session import get_engine, init_db
from app.services.manifest_io import ManifestValidationError, vacation_from_manifest


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python scripts/import_manifest.py MANIFEST_JSON_PATH", file=sys.stderr)
        return 2
    init_db()
    try:
        raw_manifest = json.loads(Path(sys.argv[1]).read_text())
        if not isinstance(raw_manifest, dict):
            raise ManifestValidationError("Manifest must be a JSON object")
        with Session(get_engine()) as session:
            vacation = vacation_from_manifest(session, raw_manifest)
            print(f"Imported vacation {vacation.id}: {vacation.title}")
    except (OSError, json.JSONDecodeError, ManifestValidationError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
