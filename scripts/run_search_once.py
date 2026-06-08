from __future__ import annotations

import argparse
import json
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
    command_parser = argparse.ArgumentParser(description="Run one vacation source search.")
    group = command_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--vacation-id", type=int, help="Vacation ID to search")
    group.add_argument("--all-active", action="store_true", help="Run once for every active vacation")
    command_parser.add_argument("--use-real-sources", action="store_true", help="Call configured Phase 3 real sources")
    command_parser.add_argument("--use-mock", action="store_true", help="Include deterministic mock source results")
    return command_parser


def print_run(session: Session, vacation_id: int, *, use_real_sources: bool, use_mock: bool) -> None:
    search_run = run_search_once(
        vacation_id,
        "cli",
        session=session,
        use_real_sources=use_real_sources,
        use_mock=use_mock,
    )
    source_count = len(source_results_for_run(session, search_run.id))
    summary = json.loads(search_run.summary_json or "{}")
    print(
        f"run_id={search_run.id} "
        f"vacation_id={search_run.vacation_id} "
        f"status={search_run.status} "
        f"source_results={source_count} "
        f"price_snapshots={summary.get('priced_snapshot_count', 0)} "
        f"deal_candidates={summary.get('deal_candidate_count', 0)} "
        f"best_deal_total_price={summary.get('best_deal_total_price')}"
    )


def main() -> int:
    args = parser().parse_args()
    use_mock = args.use_mock or not args.use_real_sources
    init_db()
    with Session(get_engine()) as session:
        if args.vacation_id is not None:
            print_run(session, args.vacation_id, use_real_sources=args.use_real_sources, use_mock=use_mock)
            return 0
        for vacation in load_active_vacations(session):
            print_run(session, vacation.id, use_real_sources=args.use_real_sources, use_mock=use_mock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
