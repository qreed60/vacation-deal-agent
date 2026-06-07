from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlmodel import Session

from app.db.session import get_engine, init_db
from app.services.active_manifest_loader import load_active_vacations
from app.services.search_runner import run_search_once, source_results_for_run


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(description="Run one Phase 2 mock travel search.")
    group = command_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--vacation-id", type=int, help="Vacation ID to search")
    group.add_argument("--all-active", action="store_true", help="Run once for every active vacation")
    return command_parser


def print_run(session: Session, vacation_id: int) -> None:
    search_run = run_search_once(vacation_id, "cli", session=session)
    source_count = len(source_results_for_run(session, search_run.id))
    print(
        f"run_id={search_run.id} "
        f"vacation_id={search_run.vacation_id} "
        f"status={search_run.status} "
        f"source_results={source_count}"
    )


def main() -> int:
    args = parser().parse_args()
    init_db()
    with Session(get_engine()) as session:
        if args.vacation_id is not None:
            print_run(session, args.vacation_id)
            return 0
        for vacation in load_active_vacations(session):
            print_run(session, vacation.id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
