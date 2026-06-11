#!/usr/bin/env python3
"""Safe non-USD historical data deletion utility for the vacation-deal-agent database.

Removes historical price_snapshot and deal_candidate rows where currency != 'USD'.
This is for cleaning old bad EUR runs from before TRVL_REQUIRE_CONFIGURED_CURRENCY=true.

Default behavior is dry-run (report only). Use --execute to actually delete rows.
Creates a SQLite backup before executing unless --no-backup is passed.

Usage:
    python scripts/delete_non_usd_history.py                     # Dry run (default)
    python scripts/delete_non_usd_history.py --vacation-id 1     # For specific vacation
    python scripts/delete_non_usd_history.py --execute           # Actually delete
    python scripts/delete_non_usd_history.py --execute --no-backup  # Delete without backup
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def get_db_path() -> Path:
    """Get the database path from environment or default."""
    db_url = os.environ.get("VACATION_DEAL_DB_URL", "sqlite:///data/vacation_deals.sqlite3")
    if db_url.startswith("sqlite:///"):
        return Path(db_url.replace("sqlite:///", "", 1))
    return Path("data/vacation_deals.sqlite3")


def get_backups_dir() -> Path:
    """Get the backups directory path."""
    db_path = get_db_path()
    return db_path.parent / "backups"


def create_backup(db_path: Path, backups_dir: Path) -> str:
    """Create a timestamped SQLite backup. Returns the backup path."""
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_name = f"vacation_deals.{timestamp}.non-usd-cleanup.sqlite3"
    backup_path = backups_dir / backup_name

    # Avoid overwriting existing backups (idempotent safety)
    counter = 0
    while backup_path.exists():
        counter += 1
        backup_path = backups_dir / f"vacation_deals.{timestamp}.non-usd-cleanup.{counter}.sqlite3"

    shutil.copy2(db_path, backup_path)
    return str(backup_path)


def count_rows(conn: sqlite3.Connection, table: str, where: str = "") -> int:
    """Count rows in a table with optional WHERE clause."""
    query = f"SELECT COUNT(*) FROM {table}"
    if where:
        query += f" WHERE {where}"
    result = conn.execute(query).fetchone()
    return result[0] if result else 0


def get_non_usd_snapshots(conn: sqlite3.Connection, vacation_id: int | None) -> list[tuple[int, str]]:
    """Get (id, currency) of price_snapshot rows where currency != 'USD'."""
    query = "SELECT id, currency FROM price_snapshot WHERE currency != 'USD'"
    if vacation_id is not None:
        query += f" AND vacation_id = {vacation_id}"
    return [tuple(row) for row in conn.execute(query).fetchall()]


def get_non_usd_candidates(conn: sqlite3.Connection, vacation_id: int | None) -> list[tuple[int, str]]:
    """Get (id, currency) of deal_candidate rows where currency != 'USD'."""
    query = "SELECT id, currency FROM deal_candidate WHERE currency != 'USD'"
    if vacation_id is not None:
        query += f" AND vacation_id = {vacation_id}"
    return [tuple(row) for row in conn.execute(query).fetchall()]


def get_orphaned_source_results_for_vacation(conn: sqlite3.Connection, vacation_id: int | None) -> list[int]:
    """Get IDs of source_result rows orphaned by removing non-USD snapshots."""
    # First find which price_snapshot ids will remain after cleanup
    remaining_query = "SELECT id FROM price_snapshot WHERE currency = 'USD'"
    if vacation_id is not None:
        remaining_query += f" AND vacation_id = {vacation_id}"
    
    all_snapshots_query = "SELECT id FROM price_snapshot"
    if vacation_id is not None:
        all_snapshots_query += f" WHERE vacation_id = {vacation_id}"
    
    # Get source_result ids that reference non-USD snapshots (will become orphaned)
    query = f"""
        SELECT DISTINCT sr.id FROM source_result sr
        JOIN price_snapshot ps ON sr.id = ps.source_result_id
        WHERE ps.currency != 'USD'
        AND NOT EXISTS (
            SELECT 1 FROM price_snapshot ps2 
            WHERE ps2.source_result_id = sr.id AND ps2.currency = 'USD'
        )
    """
    if vacation_id is not None:
        query += f" AND sr.search_run_id IN (SELECT id FROM search_run WHERE vacation_id = {vacation_id})"
    
    return [row[0] for row in conn.execute(query).fetchall()]


def main():
    parser = argparse.ArgumentParser(
        description="Delete non-USD historical data from the vacation-deal-agent database.",
        epilog="Default: dry-run. Use --execute to actually delete rows.",
    )
    parser.add_argument(
        "--vacation-id",
        type=int,
        default=None,
        help="Filter by specific vacation ID (default: all vacations)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete non-USD data (default is dry-run)",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip creating a backup before executing",
    )
    args = parser.parse_args()

    db_path = get_db_path()
    if not db_path.exists():
        print(f"ERROR: Database file not found: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    try:
        # Get non-USD rows
        non_usd_snapshots = get_non_usd_snapshots(conn, args.vacation_id)
        non_usd_candidates = get_non_usd_candidates(conn, args.vacation_id)

        print("=" * 60)
        print("NON-USD HISTORICAL DATA CLEANUP REPORT")
        print("=" * 60)
        print(f"Database: {db_path}")
        print(f"Mode: {'EXECUTE' if args.execute else 'DRY-RUN'}")
        vacation_filter = f" (vacation_id={args.vacation_id})" if args.vacation_id else " (all vacations)"
        print(f"Vacation filter:{vacation_filter}")
        print("-" * 60)

        # Group by currency for reporting
        snapshot_by_currency: dict[str, list[int]] = {}
        for sid, cur in non_usd_snapshots:
            snapshot_by_currency.setdefault(cur, []).append(sid)

        candidate_by_currency: dict[str, list[int]] = {}
        for cid, cur in non_usd_candidates:
            candidate_by_currency.setdefault(cur, []).append(cid)

        print(f"\nprice_snapshot rows with currency != 'USD': {len(non_usd_snapshots)}")
        for cur, ids in sorted(snapshot_by_currency.items()):
            print(f"  {cur}: {len(ids)} rows (IDs: {ids[:5]}{'...' if len(ids) > 5 else ''})")

        print(f"\ndeal_candidate rows with currency != 'USD': {len(non_usd_candidates)}")
        for cur, ids in sorted(candidate_by_currency.items()):
            print(f"  {cur}: {len(ids)} rows (IDs: {ids[:5]}{'...' if len(ids) > 5 else ''})")

        # Count orphaned source_results that would be cleaned up
        orphaned_source_ids = get_orphaned_source_results_for_vacation(conn, args.vacation_id)
        print(f"\nOrphaned source_result (will become orphaned after cleanup): {len(orphaned_source_ids)}")
        print("-" * 60)

        if not args.execute:
            print()
            print("DRY-RUN MODE: No changes will be made.")
            print("To execute, run with --execute flag.")
            print(f"  python scripts/delete_non_usd_history.py --vacation-id {args.vacation_id or 'N'} --execute")
            return

        # Backup before executing (unless --no-backup)
        if not args.no_backup:
            backups_dir = get_backups_dir()
            backup_path = create_backup(db_path, backups_dir)
            print(f"\nBackup created: {backup_path}")
        else:
            print("\nBackup skipped (--no-backup)")

        # Delete non-USD snapshots first
        if non_usd_snapshots:
            ids_str = ",".join(str(sid) for sid, _ in non_usd_snapshots)
            conn.execute(f"DELETE FROM price_snapshot WHERE id IN ({ids_str})")
            print(f"\nDeleted {len(non_usd_snapshots)} price_snapshot rows (non-USD)")

        # Delete orphaned source_results that only referenced non-USD snapshots
        if orphaned_source_ids:
            ids_str = ",".join(str(i) for i in orphaned_source_ids)
            conn.execute(f"DELETE FROM source_result WHERE id IN ({ids_str})")
            print(f"Deleted {len(orphaned_source_ids)} orphaned source_result rows (non-USD only)")

        # Delete non-USD deal candidates
        if non_usd_candidates:
            ids_str = ",".join(str(cid) for cid, _ in non_usd_candidates)
            conn.execute(f"DELETE FROM deal_candidate WHERE id IN ({ids_str})")
            print(f"Deleted {len(non_usd_candidates)} deal_candidate rows (non-USD)")

        conn.commit()

        # Count after
        remaining_snapshots = count_rows(conn, "price_snapshot", "currency != 'USD'")
        if args.vacation_id is not None:
            remaining_snapshots = count_rows(
                conn, "price_snapshot", f"currency != 'USD' AND vacation_id = {args.vacation_id}"
            )
        
        remaining_candidates = count_rows(conn, "deal_candidate", "currency != 'USD'")
        if args.vacation_id is not None:
            remaining_candidates = count_rows(
                conn, "deal_candidate", f"currency != 'USD' AND vacation_id = {args.vacation_id}"
            )

        print("-" * 60)
        print(f"\nAfter cleanup:")
        print(f"  price_snapshot currency != 'USD': {remaining_snapshots}")
        print(f"  deal_candidate currency != 'USD': {remaining_candidates}")
        print("=" * 60)
        print("Done.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
