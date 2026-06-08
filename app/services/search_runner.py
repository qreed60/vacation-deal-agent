from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from app.adapters import amadeus, google_places, mock_travel, searxng
from app.db.models import DealCandidate, PriceSnapshot, SearchRun, SourceResult, Vacation, utc_now
from app.db.session import get_engine
from app.services.package_builder import build_deal_candidates
from app.services.quote_normalizer import snapshots_from_source_result
from app.services.search_planner import build_search_plan, deterministic_json
from app.services.source_config import SourceConfig, load_source_config


def _source_result(
    search_run_id: int,
    source_name: str,
    result_type: str,
    query_entry: dict[str, Any],
    adapter_result: dict[str, Any],
) -> SourceResult:
    return SourceResult(
        search_run_id=search_run_id,
        source_name=source_name,
        result_type=result_type,
        status=adapter_result["status"],
        query_json=deterministic_json(query_entry),
        normalized_result_json=deterministic_json(adapter_result.get("normalized_result") or {}),
        raw_result_json=deterministic_json(adapter_result.get("raw_result") or {}),
        error_message=adapter_result.get("error_message"),
    )


def _web_context_query(query_entry: dict[str, Any]) -> str:
    query = query_entry["query"]
    service = query.get("service", query_entry["result_type"])
    destination = query.get("destination", "")
    origin = query.get("origin", "")
    start_date = query.get("start_date") or ""
    end_date = query.get("end_date") or ""
    parts = [service, "travel", origin, "to", destination, start_date, end_date]
    return " ".join(str(part) for part in parts if part).strip()


def _persist_adapter_result(
    session: Session,
    search_run_id: int,
    source_name: str,
    result_type: str,
    query_entry: dict[str, Any],
    adapter_result: dict[str, Any],
) -> str:
    session.add(_source_result(search_run_id, source_name, result_type, query_entry, adapter_result))
    return adapter_result["status"]


def _run_real_sources(
    session: Session,
    search_run_id: int,
    query_entry: dict[str, Any],
    config: SourceConfig,
    amadeus_client: amadeus.AmadeusClient,
) -> list[str]:
    statuses: list[str] = []
    query = query_entry["query"]
    result_type = query_entry["result_type"]

    web_query = {
        "source_name": "searxng",
        "result_type": "web_context",
        "query": {"text": _web_context_query(query_entry), "source_query": query},
    }
    web_result = searxng.search(
        web_query["query"]["text"],
        base_url=config.searxng_base_url,
        timeout_seconds=config.searxng_timeout_seconds,
    )
    statuses.append(_persist_adapter_result(session, search_run_id, "searxng", "web_context", web_query, web_result))

    if result_type == "flight":
        flight_query = {"source_name": "amadeus", "result_type": "flight", "query": query}
        flight_result = amadeus_client.flight_offers_search(query)
        statuses.append(_persist_adapter_result(session, search_run_id, "amadeus", "flight", flight_query, flight_result))
    elif result_type == "hotel":
        hotel_query = {"source_name": "amadeus", "result_type": "hotel", "query": {**query, "operation": "hotel_list"}}
        hotel_result = amadeus_client.hotel_list_search(query)
        statuses.append(_persist_adapter_result(session, search_run_id, "amadeus", "hotel", hotel_query, hotel_result))

        hotel_ids: list[str] = []
        if hotel_result["status"] == "completed":
            for hotel in hotel_result.get("normalized_result", {}).get("hotels", []):
                if hotel.get("hotel_id"):
                    hotel_ids.append(hotel["hotel_id"])
        offers_query = {"source_name": "amadeus", "result_type": "hotel", "query": {**query, "operation": "hotel_offers"}}
        offers_result = amadeus_client.hotel_offers_lookup(hotel_ids, query)
        statuses.append(_persist_adapter_result(session, search_run_id, "amadeus", "hotel", offers_query, offers_result))

        places_text = f"hotels in {query.get('destination', '')}".strip()
        places_query = {
            "source_name": "google_places",
            "result_type": "place_enrichment",
            "query": {"text": places_text, "source_query": query},
        }
        places_result = google_places.text_search(
            places_text,
            api_key=config.google_places_api_key,
            enabled=config.google_places_enabled,
            timeout_seconds=config.google_places_timeout_seconds,
        )
        statuses.append(
            _persist_adapter_result(session, search_run_id, "google_places", "place_enrichment", places_query, places_result)
        )

    return statuses


def _run_with_session(
    session: Session,
    vacation_id: int,
    trigger_type: str,
    *,
    use_real_sources: bool = False,
    use_mock: bool = True,
) -> SearchRun:
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
        status_counts: dict[str, int] = {}
        config = load_source_config()
        amadeus_client = amadeus.AmadeusClient(
            base_url=config.amadeus_base_url,
            client_id=config.amadeus_client_id,
            client_secret=config.amadeus_client_secret,
            enabled=config.amadeus_enabled,
            timeout_seconds=config.amadeus_timeout_seconds,
        )
        for query_entry in plan["queries"]:
            if use_mock:
                adapter_result = mock_travel.search(query_entry)
                _persist_adapter_result(
                    session,
                    search_run.id,
                    query_entry["source_name"],
                    query_entry["result_type"],
                    query_entry,
                    adapter_result,
                )
                status_counts[adapter_result["status"]] = status_counts.get(adapter_result["status"], 0) + 1
                result_count += 1
            if use_real_sources:
                real_statuses = _run_real_sources(session, search_run.id, query_entry, config, amadeus_client)
                result_count += len(real_statuses)
                for status in real_statuses:
                    status_counts[status] = status_counts.get(status, 0) + 1

        source_results = source_results_for_run(session, search_run.id)
        price_snapshots: list[PriceSnapshot] = []
        for source_result in source_results:
            price_snapshots.extend(snapshots_from_source_result(vacation, source_result))
        for snapshot in price_snapshots:
            session.add(snapshot)
        session.commit()
        for snapshot in price_snapshots:
            session.refresh(snapshot)

        deal_candidates = build_deal_candidates(session, vacation, search_run.id, price_snapshots)
        for candidate in deal_candidates:
            session.add(candidate)
        session.commit()
        for candidate in deal_candidates:
            session.refresh(candidate)

        best_deal = best_deal_for_run(session, search_run.id)
        search_run.status = "completed"
        completed_at = utc_now()
        search_run.completed_at = completed_at
        search_run.updated_at = completed_at
        search_run.summary_json = deterministic_json(
            {
                "best_deal_currency": best_deal.currency if best_deal else None,
                "best_deal_id": best_deal.id if best_deal else None,
                "best_deal_total_price": best_deal.total_price if best_deal else None,
                "deal_candidate_count": len(deal_candidates),
                "mock": use_mock,
                "priced_snapshot_count": len(price_snapshots),
                "real_sources": use_real_sources,
                "source_result_count": result_count,
                "source_status_counts": status_counts,
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


def run_search_once(
    vacation_id: int,
    trigger_type: str,
    session: Session | None = None,
    *,
    use_real_sources: bool = False,
    use_mock: bool = True,
) -> SearchRun:
    if session is not None:
        return _run_with_session(
            session,
            vacation_id,
            trigger_type,
            use_real_sources=use_real_sources,
            use_mock=use_mock,
        )
    with Session(get_engine()) as local_session:
        return _run_with_session(
            local_session,
            vacation_id,
            trigger_type,
            use_real_sources=use_real_sources,
            use_mock=use_mock,
        )


def source_results_for_run(session: Session, search_run_id: int) -> list[SourceResult]:
    statement = (
        select(SourceResult)
        .where(SourceResult.search_run_id == search_run_id)
        .order_by(SourceResult.created_at.asc(), SourceResult.id.asc())
    )
    return list(session.exec(statement).all())


def price_snapshots_for_run(session: Session, search_run_id: int) -> list[PriceSnapshot]:
    statement = (
        select(PriceSnapshot)
        .where(PriceSnapshot.search_run_id == search_run_id)
        .order_by(PriceSnapshot.created_at.asc(), PriceSnapshot.id.asc())
    )
    return list(session.exec(statement).all())


def deal_candidates_for_vacation(session: Session, vacation_id: int) -> list[DealCandidate]:
    statement = (
        select(DealCandidate)
        .where(DealCandidate.vacation_id == vacation_id)
        .order_by(DealCandidate.id.asc())
    )
    candidates = list(session.exec(statement).all())
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate.score is None,
            candidate.score if candidate.score is not None else float("inf"),
            candidate.total_price is None,
            candidate.total_price if candidate.total_price is not None else float("inf"),
            candidate.id or 0,
        ),
    )


def best_deal_for_vacation(session: Session, vacation_id: int) -> DealCandidate | None:
    candidates = session.exec(
        select(DealCandidate)
        .where(DealCandidate.vacation_id == vacation_id)
        .where(DealCandidate.status == "valid")
        .where(DealCandidate.score.is_not(None))
        .order_by(DealCandidate.score.asc(), DealCandidate.total_price.asc(), DealCandidate.id.asc())
        .limit(1)
    ).all()
    return candidates[0] if candidates else None


def best_deal_for_run(session: Session, search_run_id: int) -> DealCandidate | None:
    candidates = session.exec(
        select(DealCandidate)
        .where(DealCandidate.search_run_id == search_run_id)
        .where(DealCandidate.status == "valid")
        .where(DealCandidate.score.is_not(None))
        .order_by(DealCandidate.score.asc(), DealCandidate.total_price.asc(), DealCandidate.id.asc())
        .limit(1)
    ).all()
    return candidates[0] if candidates else None
