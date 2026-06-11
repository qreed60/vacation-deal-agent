#!/usr/bin/env python3
"""Safely detect and delete mock travel rows.

Default behavior is dry-run. With --execute, mock deal_candidate and
price_snapshot rows are deleted even when legacy is_mock flags were wrong.
Mock-only source_result rows are deleted after their mock snapshots are gone.
SearchRun rows are preserved as audit history and reported separately.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def get_db_path() -> Path:
    db_url = os.environ.get("VACATION_DEAL_DB_URL", "sqlite:///data/vacation_deals.sqlite3")
    if db_url.startswith("sqlite:///"):
        return Path(db_url.replace("sqlite:///", "", 1))
    return Path("data/vacation_deals.sqlite3")


def get_backups_dir() -> Path:
    return get_db_path().parent / "backups"


def create_backup(db_path: Path, backups_dir: Path) -> str:
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backups_dir / f"vacation_deals.{timestamp}.sqlite3"
    counter = 0
    while backup_path.exists():
        counter += 1
        backup_path = backups_dir / f"vacation_deals.{timestamp}.{counter}.sqlite3"
    shutil.copy2(db_path, backup_path)
    return str(backup_path)


def ids(conn: sqlite3.Connection, query: str) -> list[int]:
    return [int(row[0]) for row in conn.execute(query).fetchall()]


def csv(values: list[int] | set[int]) -> str:
    return ",".join(str(value) for value in sorted(values))


def in_clause(values: list[int] | set[int]) -> str:
    return csv(values) if values else "NULL"


def get_mock_search_run_ids(conn: sqlite3.Connection) -> list[int]:
    return ids(
        conn,
        """
        SELECT DISTINCT r.id
        FROM search_run r
        WHERE r.summary_json LIKE '%"mock":true%'
           OR r.summary_json LIKE '%"mock": true%'
           OR (
                (r.summary_json LIKE '%"real_sources":false%'
                 OR r.summary_json LIKE '%"real_sources": false%')
                AND r.summary_json LIKE '%source_status_counts%'
                AND r.summary_json LIKE '%mock%'
           )
           OR EXISTS (
                SELECT 1 FROM source_result sr
                WHERE sr.search_run_id = r.id
                  AND (sr.source_name = 'mock_travel' OR sr.status = 'mock')
           )
        """,
    )


def get_mock_source_result_ids(conn: sqlite3.Connection, mock_run_ids: list[int]) -> list[int]:
    return ids(
        conn,
        f"""
        SELECT DISTINCT sr.id
        FROM source_result sr
        WHERE sr.source_name = 'mock_travel'
           OR sr.status = 'mock'
           OR sr.normalized_result_json LIKE '%"mock":true%'
           OR sr.normalized_result_json LIKE '%"mock": true%'
           OR sr.search_run_id IN ({in_clause(mock_run_ids)})
        """,
    )


def get_mock_candidate_ids(
    conn: sqlite3.Connection,
    mock_run_ids: list[int],
    mock_source_result_ids: list[int],
) -> list[int]:
    return ids(
        conn,
        f"""
        SELECT DISTINCT dc.id
        FROM deal_candidate dc
        WHERE dc.is_mock = 1
           OR dc.title LIKE '%MOCK%'
           OR dc.title LIKE '%Mock Air%'
           OR dc.normalized_result_json LIKE '%mock_travel%'
           OR dc.normalized_result_json LIKE '%"mock":true%'
           OR dc.normalized_result_json LIKE '%"mock": true%'
           OR dc.source_links_json LIKE '%mock_travel%'
           OR dc.source_links_json LIKE '%"source":"mock"%'
           OR dc.source_links_json LIKE '%"source": "mock"%'
           OR dc.component_snapshot_ids_json LIKE '%"source":"mock"%'
           OR dc.component_snapshot_ids_json LIKE '%"source": "mock"%'
           OR dc.search_run_id IN ({in_clause(mock_run_ids)})
           OR EXISTS (
                SELECT 1
                FROM price_snapshot ps
                WHERE ps.search_run_id = dc.search_run_id
                  AND ps.source_result_id IN ({in_clause(mock_source_result_ids)})
           )
        """,
    )


def get_mock_snapshot_ids(
    conn: sqlite3.Connection,
    mock_run_ids: list[int],
    mock_source_result_ids: list[int],
) -> list[int]:
    return ids(
        conn,
        f"""
        SELECT DISTINCT ps.id
        FROM price_snapshot ps
        WHERE ps.is_mock = 1
           OR ps.label LIKE '%MOCK%'
           OR ps.label LIKE '%Mock Air%'
           OR ps.normalized_json LIKE '%mock_travel%'
           OR ps.normalized_json LIKE '%"mock":true%'
           OR ps.normalized_json LIKE '%"mock": true%'
           OR ps.source_name = 'mock_travel'
           OR ps.search_run_id IN ({in_clause(mock_run_ids)})
           OR ps.source_result_id IN ({in_clause(mock_source_result_ids)})
        """,
    )


def get_orphaned_source_result_ids(conn: sqlite3.Connection) -> list[int]:
    return ids(
        conn,
        """
        SELECT DISTINCT sr.id
        FROM source_result sr
        LEFT JOIN price_snapshot ps ON ps.source_result_id = sr.id
        WHERE ps.id IS NULL
          AND (
              sr.source_name = 'mock_travel'
              OR sr.status = 'mock'
              OR sr.normalized_result_json LIKE '%"mock":true%'
              OR sr.normalized_result_json LIKE '%"mock": true%'
          )
        """,
    )


def get_safe_mock_source_result_ids(
    conn: sqlite3.Connection,
    mock_source_result_ids: list[int],
    mock_snapshot_ids: list[int],
) -> list[int]:
    if not mock_source_result_ids:
        return []
    return ids(
        conn,
        f"""
        SELECT DISTINCT sr.id
        FROM source_result sr
        WHERE sr.id IN ({in_clause(mock_source_result_ids)})
          AND NOT EXISTS (
              SELECT 1
              FROM price_snapshot ps
              WHERE ps.source_result_id = sr.id
                AND ps.id NOT IN ({in_clause(mock_snapshot_ids)})
          )
        """,
    )


def delete_ids(conn: sqlite3.Connection, table: str, row_ids: list[int]) -> int:
    if not row_ids:
        return 0
    conn.execute(f"DELETE FROM {table} WHERE id IN ({in_clause(row_ids)})")
    return len(row_ids)


def print_id_sample(label: str, row_ids: list[int]) -> None:
    sample = csv(row_ids[:20])
    suffix = " ..." if len(row_ids) > 20 else ""
    print(f"{label}: {len(row_ids)}" + (f" [{sample}{suffix}]" if row_ids else ""))


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect and delete mock travel data. Default: dry-run.")
    parser.add_argument("--execute", action="store_true", help="Delete detected mock rows")
    parser.add_argument("--no-backup", action="store_true", help="Skip backup before --execute")
    args = parser.parse_args()

    db_path = get_db_path()
    if not db_path.exists():
        print(f"ERROR: Database file not found: {db_path}")
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        mock_run_ids = get_mock_search_run_ids(conn)
        mock_source_ids = get_mock_source_result_ids(conn, mock_run_ids)
        mock_candidate_ids = get_mock_candidate_ids(conn, mock_run_ids, mock_source_ids)
        mock_snapshot_ids = get_mock_snapshot_ids(conn, mock_run_ids, mock_source_ids)
        orphaned_before_ids = get_orphaned_source_result_ids(conn)
        safe_mock_source_ids = get_safe_mock_source_result_ids(conn, mock_source_ids, mock_snapshot_ids)

        print("MOCK DATA CLEANUP REPORT")
        print(f"Database: {db_path}")
        print(f"Mode: {'EXECUTE' if args.execute else 'DRY-RUN'}")
        print_id_sample("mock search_runs preserved", mock_run_ids)
        print_id_sample("mock source_results safe to delete", safe_mock_source_ids)
        print_id_sample("mock deal_candidates", mock_candidate_ids)
        print_id_sample("mock price_snapshots", mock_snapshot_ids)
        print_id_sample("orphaned source_results", orphaned_before_ids)

        if not args.execute:
            print("DRY-RUN: no rows deleted. Re-run with --execute after reviewing counts.")
            return 0

        if not args.no_backup:
            print(f"Backup created: {create_backup(db_path, get_backups_dir())}")
        else:
            print("Backup skipped (--no-backup)")

        deleted_candidates = delete_ids(conn, "deal_candidate", mock_candidate_ids)
        deleted_snapshots = delete_ids(conn, "price_snapshot", mock_snapshot_ids)

        orphaned_after_ids = get_orphaned_source_result_ids(conn)
        source_ids_to_delete = sorted(set(safe_mock_source_ids) | set(orphaned_after_ids))
        deleted_sources = delete_ids(conn, "source_result", source_ids_to_delete)

        conn.commit()

        print(f"Deleted mock deal_candidates: {deleted_candidates}")
        print(f"Deleted mock price_snapshots: {deleted_snapshots}")
        print(f"Deleted mock/orphaned source_results: {deleted_sources}")
        print(f"Preserved mock search_runs as history: {len(mock_run_ids)}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
