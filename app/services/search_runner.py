from __future__ import annotations

from sqlmodel import Session, select

from app.adapters import mock_travel
from app.db.models import SearchRun, SourceResult, Vacation, utc_now
from app.db.session import get_engine
from app.services.search_planner import build_search_plan, deterministic_json


def _run_with_session(session: Session, vacation_id: int, trigger_type: str) -> SearchRun:
    now = utc_now()
    search_run = SearchRun(
        vacation_id=vacation_id,
        status="queued",
        trigger_type=trigger_type,
        created_at=now,
        updated_at=now,
    )
    session.add(search_run)
    session.commit()
    session.refresh(search_run)

    try:
        vacation = session.get(Vacation, vacation_id)
        if vacation is None:
            raise ValueError(f"Vacation {vacation_id} not found")

        search_run.status = "running"
        search_run.started_at = utc_now()
        search_run.updated_at = search_run.started_at
        plan = build_search_plan(vacation)
        search_run.search_plan_json = deterministic_json(plan)
        session.add(search_run)
        session.commit()
        session.refresh(search_run)

        result_count = 0
        for query_entry in plan["queries"]:
            adapter_result = mock_travel.search(query_entry)
            source_result = SourceResult(
                search_run_id=search_run.id,
                source_name=query_entry["source_name"],
                result_type=query_entry["result_type"],
                status=adapter_result["status"],
                query_json=deterministic_json(query_entry),
                normalized_result_json=deterministic_json(adapter_result["normalized_result"]),
                raw_result_json=deterministic_json(adapter_result["raw_result"]),
            )
            session.add(source_result)
            result_count += 1

        completed_at = utc_now()
        search_run.status = "completed"
        search_run.completed_at = completed_at
        search_run.updated_at = completed_at
        search_run.summary_json = deterministic_json(
            {
                "mock": True,
                "source_result_count": result_count,
                "status": "completed",
            }
        )
        session.add(search_run)
        session.commit()
        session.refresh(search_run)
        return search_run
    except Exception as exc:
        failed_at = utc_now()
        search_run.status = "failed"
        search_run.completed_at = failed_at
        search_run.updated_at = failed_at
        search_run.error_message = str(exc)
        session.add(search_run)
        session.commit()
        session.refresh(search_run)
        return search_run


def run_search_once(vacation_id: int, trigger_type: str, session: Session | None = None) -> SearchRun:
    if session is not None:
        return _run_with_session(session, vacation_id, trigger_type)
    with Session(get_engine()) as local_session:
        return _run_with_session(local_session, vacation_id, trigger_type)


def source_results_for_run(session: Session, search_run_id: int) -> list[SourceResult]:
    statement = (
        select(SourceResult)
        .where(SourceResult.search_run_id == search_run_id)
        .order_by(SourceResult.created_at.asc(), SourceResult.id.asc())
    )
    return list(session.exec(statement).all())
