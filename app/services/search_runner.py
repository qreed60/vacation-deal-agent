from __future__ import annotations

import json
from typing import Any

from sqlmodel import Session, and_, or_, select

from app.adapters import amadeus, fast_flights_adapter, google_places, mock_travel, searxng, serpapi_travel, trvl_adapter
from app.db.models import DealCandidate, PriceSnapshot, SearchRun, SourceResult, Vacation, utc_now
from app.db.session import get_engine
from app.services.manifest_io import manifest_for_vacation
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


def _flight_summary_diagnostics(plan: dict[str, Any], config: SourceConfig, manifest: dict[str, Any]) -> dict[str, Any]:
    for query_entry in plan.get("queries", []):
        if query_entry.get("result_type") != "flight":
            continue
        manifest_data = manifest or {}
        query_json = trvl_adapter.build_flight_query(
            query_entry["query"],
            currency=config.trvl_currency,
            preferred_airports=manifest_data.get("preferred_airports") or [],
            alternate_airports=manifest_data.get("alternate_airports") or [],
        )
        return {
            "origin_input": query_json.get("origin_value"),
            "destination_input": query_json.get("destination_value"),
            "resolved_origin_airport": query_json.get("origin_airport"),
            "resolved_destination_airport": query_json.get("destination_airport"),
            "origin_resolution_status": query_json.get("origin_resolution_status"),
            "destination_resolution_status": query_json.get("destination_resolution_status"),
            "origin_resolution_source": query_json.get("origin_resolution_source"),
            "destination_resolution_source": query_json.get("destination_resolution_source"),
            "traveler_count": query_json.get("traveler_count"),
            "adult_count": query_json.get("adult_count"),
            "child_count": query_json.get("child_count"),
            "infant_count": query_json.get("infant_count"),
            "trvl_adults_passed": query_json.get("trvl_adults_passed"),
            "trvl_passenger_model": query_json.get("trvl_passenger_model"),
            "departure_date": query_json.get("departure_date"),
            "return_date": query_json.get("return_date"),
        }
    return {}


def _json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# Source status classification constants
SOURCE_STATUS_SUCCESS_WITH_DEALS = "success_with_deals"
SOURCE_STATUS_SUCCESS_NO_DEALS = "success_no_deals"
SOURCE_STATUS_PROVIDER_ERROR = "provider_error"
SOURCE_STATUS_CONFIG_DISABLED = "config_disabled"
SOURCE_STATUS_ROUTE_RESOLUTION_ERROR = "route_resolution_error"
SOURCE_STATUS_RESEARCH_FALLBACK_ONLY = "research_fallback_only"
SOURCE_STATUS_SOURCE_UNAVAILABLE = "source_unavailable"
SOURCE_STATUS_TIMEOUT = "timeout"
SOURCE_STATUS_PARSE_ERROR = "parse_error"

# Structured priced source names (priority order)
STRUCTURED_PRICED_SOURCES = ["trvl", "serpapi_google_flights", "amadeus", "fast_flights"]


def _classify_source_status(result: SourceResult, config: SourceConfig | None = None) -> str:
    """Classify a source result into a standardized status category."""
    normalized = _json_dict(result.normalized_result_json)

    # Check for explicit failure categories first
    category = normalized.get("source_failure_category")
    if category == "provider_error":
        return SOURCE_STATUS_PROVIDER_ERROR
    if category == "route_resolution_error":
        return SOURCE_STATUS_ROUTE_RESOLUTION_ERROR

    # Check result type and offers/hotels count
    result_type = result.result_type
    if result.status == "skipped":
        reason = (normalized.get("reason") or "").lower()
        # Route resolution errors take priority over config disabled check below
        if "resolve" in reason or "airport" in reason:
            return SOURCE_STATUS_ROUTE_RESOLUTION_ERROR
        if "enabled" in reason or "config" in reason:
            return SOURCE_STATUS_CONFIG_DISABLED
        return SOURCE_STATUS_SOURCE_UNAVAILABLE

    if result.status == "error":
        error_msg = (result.error_message or "").lower()
        normalized_reason = (normalized.get("reason") or "").lower()
        combined = f"{error_msg} {normalized_reason}"
        if "429" in combined or "rate limit" in combined:
            return SOURCE_STATUS_PROVIDER_ERROR
        if "timeout" in combined or "timed out" in combined:
            return SOURCE_STATUS_TIMEOUT
        if "parse" in combined or "format" in combined:
            return SOURCE_STATUS_PARSE_ERROR
        # Generic error - check for provider_error category from trvl
        if result.source_name == "trvl":
            trvl_failure = normalized.get("provider_failure_reason")
            if trvl_failure and "provider" in str(trvl_failure).lower():
                return SOURCE_STATUS_PROVIDER_ERROR
        return SOURCE_STATUS_SOURCE_UNAVAILABLE

    # Completed status - check for deals
    offers = normalized.get("offers", []) or []
    hotels = normalized.get("hotels", []) or []
    if result_type == "flight" and len(offers) > 0:
        return SOURCE_STATUS_SUCCESS_WITH_DEALS
    if result_type == "hotel" and len(hotels) > 0:
        return SOURCE_STATUS_SUCCESS_WITH_DEALS
    if offers or hotels:
        return SOURCE_STATUS_SUCCESS_NO_DEALS

    # Web context / research results
    if result_type in ("web_context", "research_fallback"):
        return SOURCE_STATUS_RESEARCH_FALLBACK_ONLY

    return SOURCE_STATUS_SUCCESS_NO_DEALS


def _classify_searxng_result(result: SourceResult) -> str:
    """Classify SearXNG result as research_fallback or web_context."""
    normalized = _json_dict(result.normalized_result_json)
    results_list = normalized.get("results", [])
    if not isinstance(results_list, list):
        return SOURCE_STATUS_RESEARCH_FALLBACK_ONLY

    # Check if any results have confident price extraction
    has_confident_price = False
    for r in results_list:
        if not isinstance(r, dict):
            continue
        # Only classify as having confident price if ALL required fields are present
        if (r.get("url") and r.get("title") and r.get("content")
                and "price" in str(r.get("content", "")).lower()):
            has_confident_price = True

    return SOURCE_STATUS_RESEARCH_FALLBACK_ONLY


def _source_failure_summary(source_results: list[SourceResult], config: SourceConfig | None = None) -> dict[str, Any]:
    categories: dict[str, int] = {}
    provider_failures: list[dict[str, Any]] = []
    latest_trvl_error_category = None
    latest_trvl_exit_code = None
    latest_trvl_error_message = None
    source_statuses: dict[str, str] = {}

    for result in source_results:
        normalized = _json_dict(result.normalized_result_json)
        category = normalized.get("source_failure_category")
        reason = normalized.get("provider_failure_reason")

        # Classify status
        status_class = _classify_source_status(result, config)
        source_statuses[result.source_name] = status_class

        if category:
            categories[str(category)] = categories.get(str(category), 0) + 1
        if category or reason:
            entry = {
                "source_name": result.source_name,
                "result_type": result.result_type,
                "status": result.status,
                "source_failure_category": category,
                "provider_failure_reason": reason,
                "error_message": normalized.get("latest_trvl_error_message") or result.error_message,
            }
            provider_failures.append(entry)
        if result.source_name == "trvl" and result.result_type == "flight" and (category or reason):
            latest_trvl_error_category = category
            latest_trvl_exit_code = normalized.get("latest_trvl_exit_code")
            latest_trvl_error_message = normalized.get("latest_trvl_error_message") or result.error_message

    return {
        "source_failure_categories": categories,
        "provider_failure_summary": provider_failures,
        "latest_trvl_error_category": latest_trvl_error_category,
        "latest_trvl_exit_code": latest_trvl_exit_code,
        "latest_trvl_error_message": latest_trvl_error_message,
        "source_statuses": source_statuses,
    }


def _run_real_sources(
    session: Session,
    search_run_id: int,
    query_entry: dict[str, Any],
    config: SourceConfig,
    amadeus_client: amadeus.AmadeusClient,
    manifest: dict[str, Any] | None = None,
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
        max_results=config.searxng_max_results,
    )
    statuses.append(_persist_adapter_result(session, search_run_id, "searxng", "web_context", web_query, web_result))

    if result_type == "flight":
        serpapi_query = {"source_name": "serpapi_google_flights", "result_type": "flight", "query": query}
        serpapi_result = serpapi_travel.search_google_flights(
            query,
            enabled=config.serpapi_enabled,
            api_key=config.serpapi_api_key,
            base_url=config.serpapi_base_url,
            timeout_seconds=config.serpapi_timeout_seconds,
        )
        statuses.append(
            _persist_adapter_result(session, search_run_id, "serpapi_google_flights", "flight", serpapi_query, serpapi_result)
        )

        fast_flights_query = {"source_name": "fast_flights", "result_type": "flight", "query": query}
        manifest_data = manifest or {}
        preferred = manifest_data.get("preferred_airports") or []
        alternate = manifest_data.get("alternate_airports") or []
        trvl_query = trvl_adapter.build_flight_query(
            query,
            currency=config.trvl_currency,
            preferred_airports=preferred,
            alternate_airports=alternate,
        )
        trvl_flight_result = trvl_adapter.search_trvl_flights(
            query,
            enabled=config.trvl_enabled,
            binary_path=config.trvl_binary_path,
            timeout_seconds=config.trvl_timeout_seconds,
            max_results=config.trvl_max_flight_results,
            currency=config.trvl_currency,
            preferred_airports=preferred,
            alternate_airports=alternate,
            broad_discovery_enabled=config.trvl_broad_discovery_enabled,
            broad_include_one_way_fallbacks=config.trvl_broad_include_one_way_fallbacks,
            broad_max_alternatives=config.trvl_broad_max_alternatives,
            broad_allow_risky_alternatives=config.trvl_broad_allow_risky_alternatives,
        )
        statuses.append(_persist_adapter_result(session, search_run_id, "trvl", "flight", trvl_query, trvl_flight_result))

        fast_flights_result = fast_flights_adapter.search_fast_flights(
            query,
            enabled=config.fast_flights_enabled,
            fetch_mode=config.fast_flights_fetch_mode,
            seat=config.fast_flights_seat,
            max_stops=config.fast_flights_max_stops,
            preferred_airports=preferred,
            alternate_airports=alternate,
            max_results=config.fast_flights_max_results,
        )
        # Attach resolved airport metadata to query_entry for SourceResult.query_json.
        nr = fast_flights_result.get("normalized_result") or {}
        if isinstance(nr, dict):
            fast_flights_query["resolved_origin_airport"] = nr.get("resolved_origin_airport")
            fast_flights_query["resolved_destination_airport"] = nr.get("resolved_destination_airport")
        statuses.append(_persist_adapter_result(session, search_run_id, "fast_flights", "flight", fast_flights_query, fast_flights_result))

        flight_query = {"source_name": "amadeus", "result_type": "flight", "query": query}
        flight_result = amadeus_client.flight_offers_search(query)
        statuses.append(_persist_adapter_result(session, search_run_id, "amadeus", "flight", flight_query, flight_result))
    elif result_type == "hotel":
        serpapi_query = {"source_name": "serpapi_google_hotels", "result_type": "hotel", "query": query}
        serpapi_result = serpapi_travel.search_google_hotels(
            query,
            enabled=config.serpapi_enabled,
            api_key=config.serpapi_api_key,
            base_url=config.serpapi_base_url,
            timeout_seconds=config.serpapi_timeout_seconds,
        )
        statuses.append(
            _persist_adapter_result(session, search_run_id, "serpapi_google_hotels", "hotel", serpapi_query, serpapi_result)
        )

        trvl_query = trvl_adapter.build_hotel_query(query, currency=config.trvl_currency)
        trvl_hotel_result = trvl_adapter.search_trvl_hotels(
            query,
            enabled=config.trvl_enabled,
            binary_path=config.trvl_binary_path,
            timeout_seconds=config.trvl_timeout_seconds,
            max_results=config.trvl_max_hotel_results,
            currency=config.trvl_currency,
        )
        statuses.append(_persist_adapter_result(session, search_run_id, "trvl", "hotel", trvl_query, trvl_hotel_result))

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
    elif result_type == "rental_car":
        skipped = {
            "status": "skipped",
            "normalized_result": {
                "source_name": "structured_rental_car",
                "result_type": "rental_car",
                "reason": "No configured structured rental car price source is available",
            },
            "raw_result": {},
            "error_message": "No configured structured rental car price source is available",
        }
        rental_query = {"source_name": "structured_rental_car", "result_type": "rental_car", "query": query}
        statuses.append(_persist_adapter_result(session, search_run_id, "structured_rental_car", "rental_car", rental_query, skipped))

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
        manifest = manifest_for_vacation(vacation)

        # Respect MOCK_SEARCH_ENABLED: never generate mock data when disabled
        effective_use_mock = use_mock and config.mock_search_enabled
        if use_mock and not config.mock_search_enabled:
            status_counts["mock_disabled"] = 1

        for query_entry in plan["queries"]:
            if effective_use_mock:
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
                real_statuses = _run_real_sources(session, search_run.id, query_entry, config, amadeus_client, manifest=manifest)
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

        # Build source policy summary
        source_results = source_results_for_run(session, search_run.id)
        failure_summary = _source_failure_summary(source_results, config)
        source_statuses = failure_summary.get("source_statuses", {})

        # Determine structured source counts
        attempted_structured: list[str] = []
        skipped_structured: list[str] = []
        structured_success_count = 0
        structured_provider_error_count = 0

        for src_name in STRUCTURED_PRICED_SOURCES:
            src_status = source_statuses.get(src_name, "")
            if src_status == SOURCE_STATUS_CONFIG_DISABLED:
                skipped_structured.append(src_name)
            elif src_status == SOURCE_STATUS_PROVIDER_ERROR:
                attempted_structured.append(src_name)
                structured_provider_error_count += 1
            elif src_status in (SOURCE_STATUS_SUCCESS_WITH_DEALS, SOURCE_STATUS_SUCCESS_NO_DEALS):
                attempted_structured.append(src_name)
                if src_status == SOURCE_STATUS_SUCCESS_WITH_DEALS:
                    structured_success_count += 1
            else:
                # source_unavailable, timeout, parse_error - still attempted
                attempted_structured.append(src_name)

        # Determine best available result type
        has_exact_priced = any(
            s in (SOURCE_STATUS_SUCCESS_WITH_DEALS,)
            for s in source_statuses.values()
        )
        has_research_fallback = SOURCE_STATUS_RESEARCH_FALLBACK_ONLY in source_statuses.values()
        latest_error_summary = failure_summary.get("latest_trvl_error_message") or ""

        if has_exact_priced:
            best_available_result_type = "exact_priced_deal"
        elif any(
            s == SOURCE_STATUS_SUCCESS_NO_DEALS
            for s in source_statuses.values()
        ):
            best_available_result_type = "estimated_priced_deal"
        elif has_research_fallback:
            best_available_result_type = "research_fallback"
        else:
            best_available_result_type = "none"

        # Build search plan summary from the stored plan
        plan_data = _json_dict(search_run.search_plan_json)
        ai_planner_enabled = config.ai_search_planner_enabled and config.ai_search_planner_provider not in ("disabled", "none", "")
        research_fallback_used = False
        research_fallback_source = None

        # Check if SearXNG was used as fallback
        searxng_status = source_statuses.get("searxng", "")
        if searxng_status == SOURCE_STATUS_RESEARCH_FALLBACK_ONLY:
            research_fallback_used = True
            research_fallback_source = "searxng"

        summary_payload = {
            "best_deal_currency": best_deal.currency if best_deal else None,
            "best_deal_id": best_deal.id if best_deal else None,
            "best_deal_total_price": best_deal.total_price if best_deal else None,
            "deal_candidate_count": len(deal_candidates),
            "mock": effective_use_mock,
            "priced_snapshot_count": len(price_snapshots),
            "real_sources": use_real_sources,
            "source_result_count": result_count,
            "source_status_counts": status_counts,
            "status": "completed",
            # Source policy fields (Phase 5A)
            "source_policy_version": "phase5a_v1",
            "attempted_sources": attempted_structured,
            "skipped_sources": skipped_structured,
            "source_failure_categories": failure_summary.get("source_failure_categories", {}),
            "structured_source_count": len(attempted_structured) + len(skipped_structured),
            "structured_success_count": structured_success_count,
            "structured_provider_error_count": structured_provider_error_count,
            "research_fallback_used": research_fallback_used,
            "research_fallback_source": research_fallback_source,
            "best_available_result_type": best_available_result_type,
            # Backwards-compatible trvl error fields (kept for existing tests)
            "latest_trvl_error_category": failure_summary.get("latest_trvl_error_category"),
            "latest_trvl_exit_code": failure_summary.get("latest_trvl_exit_code"),
            "latest_trvl_error_message": failure_summary.get("latest_trvl_error_message") or "",
            # Backwards-compatible provider_failure_summary (kept for existing tests)
            "provider_failure_summary": failure_summary.get("provider_failure_summary", []),
            "latest_error_summary": latest_error_summary,
        }

        # Add AI planner info if available
        if plan_data:
            summary_payload["search_plan"] = {
                "planner_version": plan_data.get("planner_version", ""),
                "objective": plan_data.get("objective", ""),
                "reasoning_summary": plan_data.get("reasoning_summary", ""),
                "search_count": len(plan_data.get("searches") or []),
                "fallback_search_count": len(plan_data.get("fallback_searches") or []),
                "research_query_count": len(plan_data.get("research_queries") or []),
            }

        summary_payload.update(_flight_summary_diagnostics(plan, config, manifest))
        search_run.summary_json = deterministic_json(summary_payload)
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


def _legacy_mock_candidate_filter():
    mock_search_run = (
        select(SearchRun.id)
        .where(SearchRun.id == DealCandidate.search_run_id)
        .where(
            or_(
                SearchRun.summary_json.contains('"mock":true'),
                SearchRun.summary_json.contains('"mock": true'),
                and_(
                    or_(
                        SearchRun.summary_json.contains('"real_sources":false'),
                        SearchRun.summary_json.contains('"real_sources": false'),
                    ),
                    SearchRun.summary_json.contains('"mock"'),
                ),
            )
        )
        .exists()
    )
    real_source_result_in_run = (
        select(SourceResult.id)
        .where(SourceResult.search_run_id == DealCandidate.search_run_id)
        .where(SourceResult.source_name != "mock_travel")
        .where(SourceResult.status != "mock")
        .exists()
    )
    mock_only_source_result_run = (
        select(SourceResult.id)
        .where(SourceResult.search_run_id == DealCandidate.search_run_id)
        .where(or_(SourceResult.source_name == "mock_travel", SourceResult.status == "mock"))
        .where(~real_source_result_in_run)
        .exists()
    )
    return or_(
        DealCandidate.title.contains("MOCK"),
        DealCandidate.title.contains("Mock Air"),
        DealCandidate.source_links_json.contains("mock_travel"),
        DealCandidate.source_links_json.contains('"source":"mock"'),
        DealCandidate.source_links_json.contains('"source": "mock"'),
        DealCandidate.normalized_result_json.contains("mock_travel"),
        DealCandidate.normalized_result_json.contains('"mock":true'),
        DealCandidate.normalized_result_json.contains('"mock": true'),
        mock_search_run,
        mock_only_source_result_run,
    )


def deal_candidates_for_vacation(
    session: Session,
    vacation_id: int,
    include_mock: bool = False,
) -> list[DealCandidate]:
    statement = (
        select(DealCandidate)
        .where(DealCandidate.vacation_id == vacation_id)
        .where(DealCandidate.status == "valid")
        .where(DealCandidate.score.is_not(None))
    )
    if not include_mock:
        statement = statement.where(DealCandidate.is_mock == False).where(~_legacy_mock_candidate_filter())
    candidates = list(session.exec(statement).all())
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate.score is None,
            candidate.score if candidate.score is not None else float("inf"),
            candidate.total_price is None,
            candidate.total_price if candidate.total_price is not None else float("inf"),
            -(candidate.id or 0),
        ),
    )


def best_deal_for_vacation(
    session: Session,
    vacation_id: int,
    include_mock: bool = False,
) -> DealCandidate | None:
    statement = (
        select(DealCandidate)
        .where(DealCandidate.vacation_id == vacation_id)
        .where(DealCandidate.status == "valid")
        .where(DealCandidate.score.is_not(None))
        .order_by(DealCandidate.score.asc(), DealCandidate.total_price.asc(), DealCandidate.id.desc())
        .limit(1)
    )
    if not include_mock:
        statement = statement.where(DealCandidate.is_mock == False).where(~_legacy_mock_candidate_filter())
    candidates = list(session.exec(statement).all())
    return candidates[0] if candidates else None


def best_deal_for_run(session: Session, search_run_id: int) -> DealCandidate | None:
    candidates = session.exec(
        select(DealCandidate)
        .where(DealCandidate.search_run_id == search_run_id)
        .where(DealCandidate.status == "valid")
        .where(DealCandidate.score.is_not(None))
        .where(DealCandidate.is_mock == False)
        .where(~_legacy_mock_candidate_filter())
        .order_by(DealCandidate.score.asc(), DealCandidate.total_price.asc(), DealCandidate.id.desc())
        .limit(1)
    ).all()
    return candidates[0] if candidates else None
