#!/usr/bin/env python3
"""Phase 5B: scheduled-search runner script.

Finds active vacations where scheduling is enabled and next_scheduled_run_at
is due, then runs real-source searches for each one.

Usage:
    # Dry-run (no DB changes, no searches):
    python scripts/run_due_searches.py --dry-run

    # Force a specific vacation:
    python scripts/run_due_searches.py --vacation-id 5 --force

    # Limit to N vacations:
    python scripts/run_due_searches.py --limit 3

    # Custom now-time (ISO datetime):
    python scripts/run_due_searches.py --now "2026-06-11T12:00:00+00:00"

    # JSON output:
    python scripts/run_due_searches.py --json

    # Combined:
    python scripts/run_due_searches.py --dry-run --vacation-id 5 --force --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path so imports work from scripts/
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlmodel import Session, text

from app.db.models import SearchRun, Vacation, utc_now
from app.db.session import get_engine, init_db
from app.services.lock_manager import LockError, acquire_global_lock, acquire_vacation_lock
from app.services.scheduler import (
    calculate_next_scheduled_run,
    update_schedule_state,
)
from app.services.search_runner import run_search_once


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 5B scheduled-search runner")
    parser.add_argument("--dry-run", action="store_true", help="Select due vacations without modifying DB or running searches")
    parser.add_argument("--vacation-id", type=int, default=None, help="Force a specific vacation ID")
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of vacations to process (0 = unlimited)")
    parser.add_argument("--now", type=str, default=None, help="Override current time as ISO datetime (e.g. 2026-06-11T12:00:00+00:00)")
    parser.add_argument("--force", action="store_true", help="Run a vacation even if not due")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Output results as JSON")
    return parser.parse_args()


def get_now(args: argparse.Namespace) -> datetime:
    """Return the effective 'now' datetime."""
    if args.now:
        try:
            dt = datetime.fromisoformat(args.now)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            pass
    return datetime.now(timezone.utc)


def select_due_vacations(session: Session, now: datetime, force_vacation_id: int | None = None) -> list[Vacation]:
    """Select vacations due for a scheduled search."""
    if force_vacation_id is not None:
        # Force mode: return the specific vacation regardless of schedule
        v = session.get(Vacation, force_vacation_id)
        return [v] if v is not None else []

    # Normal mode: find due vacations — select by ID then load full model
    now_str = now.isoformat()
    stmt = text("""
        SELECT id FROM vacation
        WHERE status = 'active'
          AND schedule_enabled = 1
          AND (next_scheduled_run_at IS NULL OR next_scheduled_run_at <= :now)
          AND (schedule_paused_reason IS NULL OR schedule_paused_reason = '')
    """).bindparams(now=now_str)

    ids = [row[0] for row in session.exec(stmt).all()]
    if not ids:
        return []

    # Load full Vacation objects by ID to ensure proper model mapping
    results: list[Vacation] = []
    for vid in ids:
        v = session.get(Vacation, vid)
        if v is not None:
            results.append(v)
    return results


def run_vacation_search(session: Session, vacation: Vacation, now: datetime) -> dict[str, Any]:
    """Run a scheduled search for a single vacation. Returns result metadata."""
    trigger_type = "scheduled"

    # Run with real sources only, no mock
    search_run = run_search_once(
        vacation.id,
        trigger_type=trigger_type,
        session=session,
        use_real_sources=True,
        use_mock=False,
    )

    # Calculate next scheduled run
    jitter = getattr(vacation, "schedule_jitter_minutes", 20) or 20
    searches_per_day = getattr(vacation, "searches_per_day", 2) or 2
    next_run = calculate_next_scheduled_run(
        vacation_id=vacation.id,
        searches_per_day=searches_per_day,
        last_run_at=now.isoformat(),
        jitter_minutes=jitter,
        seed="runner",
    )

    # Determine status message from search run summary
    status = search_run.status or "completed"
    message = ""
    if search_run.summary_json:
        try:
            summary = json.loads(search_run.summary_json)
            best_type = summary.get("best_available_result_type", "")
            failure_cats = summary.get("source_failure_categories", {})

            if status == "failed":
                message = f"Search failed: {search_run.error_message or 'unknown error'}"
            elif failure_cats and failure_cats.get("provider_error", 0) > 0:
                # Check if there are any deals despite provider errors
                has_deals = best_type in ("exact_priced_deal", "estimated_priced_deal")
                if has_deals:
                    message = f"Completed with source errors (provider_error from {', '.join(s for s, c in failure_cats.items() if c > 0 and 'provider' in s.lower())})"
                else:
                    message = f"Provider error(s) — no deals found. Failed sources: {', '.join(s for s, c in failure_cats.items() if c > 0)}"
            elif best_type == "exact_priced_deal":
                message = f"Completed — exact priced deal found (best_available_result_type={best_type})"
            elif best_type == "estimated_priced_deal":
                message = f"Completed — estimated deals only (best_available_result_type={best_type})"
            elif best_type == "research_fallback":
                message = f"Research fallback only — verify manually (best_available_result_type={best_type})"
            else:
                message = f"Completed — no priced deals (best_available_result_type={best_type})"
        except (json.JSONDecodeError, TypeError):
            message = f"Search {status}"

    # Update schedule state
    update_schedule_state(
        session,
        vacation.id,
        last_run_at=now,
        next_run_at=next_run,
        status=status,
        message=message,
    )

    return {
        "vacation_id": vacation.id,
        "vacation_title": getattr(vacation, "title", ""),
        "search_run_id": search_run.id,
        "status": status,
        "best_available_result_type": next(
            (v for k, v in json.loads(search_run.summary_json).items() if k == "best_available_result_type"),
            "",
        ) if search_run.summary_json else "",
        "next_scheduled_run_at": next_run.isoformat() if next_run else None,
        "message": message,
    }


def main() -> int:
    args = parse_args()

    # Initialize DB (runs migrations)
    init_db()

    now = get_now(args)
    engine = get_engine()

    selected_vacations: list[Vacation] = []
    skipped_vacations: list[dict[str, Any]] = []
    run_results: list[dict[str, Any]] = []

    if args.dry_run:
        # Dry-run mode: select due vacations without modifying DB
        with Session(engine) as session:
            selected_vacations = select_due_vacations(session, now, force_vacation_id=args.vacation_id)

        print(f"=== Scheduled Search Runner (DRY RUN) ===")
        print(f"Time: {now.isoformat()}")
        print(f"Vacations due for search: {len(selected_vacations)}")
        for v in selected_vacations:
            next_run = getattr(v, "next_scheduled_run_at", None) or "(never set)"
            sp = getattr(v, "searches_per_day", 2) or 2
            print(f"  - #{v.id} {getattr(v, 'title', '')}: searches/day={sp}, next_run={next_run}")

        if args.json_output:
            output = {
                "dry_run": True,
                "now": now.isoformat(),
                "selected_count": len(selected_vacations),
                "vacations": [
                    {
                        "id": v.id,
                        "title": getattr(v, "title", ""),
                        "searches_per_day": getattr(v, "searches_per_day", 2) or 2,
                        "next_scheduled_run_at": getattr(v, "next_scheduled_run_at", None),
                    }
                    for v in selected_vacations
                ],
            }
            print(json.dumps(output, indent=2))

        return 0

    # Real mode: acquire global lock to prevent overlapping runs
    try:
        with acquire_global_lock():
            with Session(engine) as session:
                if args.vacation_id and args.force:
                    # Force mode: select specific vacation (check schedule_enabled unless explicit --vacation-id)
                    stmt = text("SELECT * FROM vacation WHERE id = :vid").bindparams(vid=args.vacation_id)
                    all_vacs = session.exec(stmt).all()
                    force_vacations = [v for v in all_vacs if isinstance(v, Vacation)]

                    if not force_vacations:
                        print(f"Vacation #{args.vacation_id} not found.")
                        return 1

                    # If --vacation-id is explicitly supplied, allow even if schedule_enabled=0
                    selected_vacations = force_vacations
                else:
                    selected_vacations = select_due_vacations(session, now)

            # Process each vacation with per-vacation lock
            for vacation in selected_vacations:
                vid = getattr(vacation, "id", None)
                if vid is None:
                    continue

                try:
                    with acquire_vacation_lock(vid):
                        with Session(engine) as session:
                            result = run_vacation_search(session, vacation, now)
                            run_results.append(result)
                except LockError:
                    skipped_vacations.append({
                        "vacation_id": vid,
                        "title": getattr(vacation, "title", ""),
                        "reason": "lock_conflict",
                    })

    except LockError as exc:
        print(f"Global lock error: {exc}", file=sys.stderr)
        return 2

    # Print summary
    provider_failures = {}
    best_type = ""
    for r in run_results:
        bt = r.get("best_available_result_type", "")
        if bt:
            best_type = bt

    print(f"\n=== Scheduled Search Runner (COMPLETE) ===")
    print(f"Time: {now.isoformat()}")
    print(f"Selected vacations: {len(selected_vacations)}")
    print(f"Skipped vacations: {len(skipped_vacations)}")
    print(f"Run IDs: {[r['search_run_id'] for r in run_results]}")
    print(f"Status: {'success' if run_results else 'no_runs'}")
    print(f"Best available result type: {best_type or '(none)'}")

    # Provider/source failure summary
    if provider_failures:
        print(f"Provider failures: {json.dumps(provider_failures)}")
    else:
        print("Provider failures: none")

    for r in run_results:
        next_run = r.get("next_scheduled_run_at", "") or "(none)"
        status_icon = "✓" if r["status"] == "completed" else "!"
        print(f"\n  {status_icon} Vacation #{r['vacation_id']} '{r['vacation_title']}':")
        print(f"    Search run: #{r['search_run_id']} ({r['status']})")
        print(f"    Message: {r.get('message', '')}")
        print(f"    Next scheduled run: {next_run}")

    if skipped_vacations:
        print("\nSkipped:")
        for s in skipped_vacations:
            print(f"  - #{s['vacation_id']} '{s['title']}': {s['reason']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
