from __future__ import annotations

import json
import sys

from sqlmodel import Session

from app.db.models import Vacation
from app.db.session import get_engine, init_db
from app.services.manifest_io import manifest_for_vacation


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python scripts/export_manifest.py VACATION_ID", file=sys.stderr)
        return 2
    init_db()
    with Session(get_engine()) as session:
        vacation = session.get(Vacation, int(sys.argv[1]))
        if vacation is None:
            print("Vacation not found", file=sys.stderr)
            return 1
        print(json.dumps(manifest_for_vacation(vacation), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
