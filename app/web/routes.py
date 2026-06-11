from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.db.models import DealCandidate, PriceSnapshot, SearchRun, SourceResult, Vacation
from app.db.session import get_session
from app.services.manifest_io import (
    ManifestValidationError,
    manifest_for_vacation,
    update_vacation_from_manifest,
    vacation_from_manifest,
)
from app.services.location_suggestions import suggest_locations_response
from app.services.price_history import (
    aggregate_daily_ohlc,
    get_source_link_label,
    is_google_flights_url,
    svg_ohlc_candles,
    vacation_price_history,
)
from app.services.search_runner import best_deal_for_vacation, deal_candidates_for_vacation, run_search_once, source_results_for_run


router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")
templates.env.globals["get_source_link_label"] = get_source_link_label


SOURCE_NAME_LABELS = {
    "amadeus": "Amadeus",
    "google_places": "Google Places",
    "mock_travel": "mock_travel",
    "searxng": "SearXNG",
    "serpapi_google_flights": "SerpAPI Google Flights",
    "serpapi_google_hotels": "SerpAPI Google Hotels",
    "structured_rental_car": "Structured rental car",
}

COMPONENT_TYPE_LABELS = {
    "flight": "Airfare",
    "hotel": "Hotel",
    "rental_car": "Rental car",
    "package": "Package",
}


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def safe_json_dict(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def safe_json_list(value: str | None) -> list:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _component_type_label(component_type: str | None, fallback: str | None = None) -> str:
    if fallback:
        return str(fallback)
    if not component_type:
        return "Package"
    return COMPONENT_TYPE_LABELS.get(component_type, component_type.replace("_", " ").title())


def _display_price(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_component_value(component: dict, keys: list[str]) -> str | None:
    for key in keys:
        value = component.get(key)
        if value:
            return str(value)
    return None


def _provider_for_component(component: dict) -> str:
    source_name = component.get("source_name")
    provider = _first_component_value(
        component,
        [
            "provider",
            "airline_name",
            "carrier_code",
            "provider_code",
            "hotel_name",
            "rental_company",
            "source_name",
        ],
    )
    return provider or (str(source_name) if source_name else "Unknown provider")


def _link_context(component: dict) -> tuple[str, str | None, str | None]:
    link_type = component.get("link_type")
    source_url = component.get("source_url")
    search_reference_url = component.get("search_reference_url") or component.get("google_maps_uri") or component.get("website_uri")
    if link_type == "exact_source" and source_url:
        return "exact_source", "View source price", str(source_url)
    if link_type == "search_reference" and search_reference_url:
        return "search_reference", "Search reference", str(search_reference_url)
    if source_url:
        return "exact_source", "View source price", str(source_url)
    if search_reference_url:
        return "search_reference", "Search reference", str(search_reference_url)
    return "none", None, None


def _raw_component_candidates(deal: DealCandidate) -> list[dict]:
    normalized = safe_json_dict(deal.normalized_result_json)
    for key in ("component_summary", "components"):
        components = normalized.get(key)
        if isinstance(components, list) and components:
            return [component for component in components if isinstance(component, dict)]
    return [component for component in safe_json_list(deal.source_links_json) if isinstance(component, dict)]


def component_display_rows_for_candidate(deal: DealCandidate | None) -> list[dict]:
    if deal is None:
        return []
    display_components: list[dict] = []
    for component in _raw_component_candidates(deal):
        component_type = component.get("component_type") or component.get("quote_type") or component.get("result_type")
        raw_source_name = component.get("source_name")
        source_name = raw_source_name or "unknown"
        total_price = _display_price(component.get("total_price"))
        currency = component.get("currency") or ("USD" if total_price is not None else None)
        provider = _provider_for_component(component)
        link_type, link_label, display_url = _link_context(component)
        display_component = {
            "airline_name": component.get("airline_name"),
            "captured_at": component.get("captured_at"),
            "component_type": component_type or "package",
            "component_type_label": _component_type_label(component_type, component.get("component_type_label")),
            "currency": currency,
            "google_maps_uri": component.get("google_maps_uri"),
            "is_mock": bool(component.get("is_mock") or component.get("mock") or source_name == "mock_travel"),
            "link_label": link_label,
            "link_type": link_type,
            "link_url": display_url,
            "label": component.get("label") or component.get("itinerary_summary") or component.get("vehicle_label") or "",
            "provider": provider,
            "provider_code": component.get("provider_code") or component.get("carrier_code") or component.get("chain_code"),
            "rating": component.get("rating"),
            "source_name": source_name,
            "source_name_label": component.get("source_name_label") or SOURCE_NAME_LABELS.get(source_name, source_name),
            "source_result_id": component.get("source_result_id"),
            "source_url": component.get("source_url"),
            "search_reference_url": component.get("search_reference_url"),
            "total_price": total_price,
            "vehicle_label": component.get("vehicle_label"),
            "website_uri": component.get("website_uri"),
        }
        display_components.append(display_component)
    return display_components


def component_summary_for_deal(deal: DealCandidate | None) -> list[dict]:
    return component_display_rows_for_candidate(deal)


def debug_json(value: str | None, fallback) -> str:
    if isinstance(fallback, list):
        parsed = safe_json_list(value)
    else:
        parsed = safe_json_dict(value)
    return json.dumps(parsed, indent=2)


def _source_payloads(normalized: dict) -> list[dict]:
    result_type = normalized.get("result_type")
    if result_type == "flight":
        offers = normalized.get("offers")
        return offers if isinstance(offers, list) else [normalized]
    if result_type == "hotel":
        hotels = normalized.get("hotels")
        return hotels if isinstance(hotels, list) else [normalized]
    if result_type == "rental_car":
        cars = normalized.get("cars") or normalized.get("offers")
        return cars if isinstance(cars, list) else [normalized]
    if result_type == "web_context":
        results = normalized.get("results")
        return results if isinstance(results, list) else []
    if result_type == "place_enrichment":
        if isinstance(normalized.get("places"), list):
            return normalized["places"]
        return [normalized["place"]] if isinstance(normalized.get("place"), dict) else []
    return [normalized] if normalized else []


def source_result_display_rows(result: SourceResult) -> list[dict]:
    normalized = safe_json_dict(result.normalized_result_json)
    rows: list[dict] = []
    for payload in _source_payloads(normalized):
        if not isinstance(payload, dict):
            continue
        component_type = payload.get("component_type") or payload.get("quote_type") or payload.get("result_type") or result.result_type
        source_name = payload.get("source_name") or result.source_name or "unknown"
        total_price = _display_price(payload.get("total_price"))
        currency = payload.get("currency") or ("USD" if total_price is not None else None)
        row_payload = {**payload, "source_name": source_name, "source_result_id": result.id}
        link_type, link_label, display_url = _link_context(row_payload)
        rows.append(
            {
                "component_type": component_type,
                "component_type_label": _component_type_label(component_type, payload.get("component_type_label")),
                "provider": _provider_for_component(row_payload),
                "source_name": source_name,
                "source_name_label": SOURCE_NAME_LABELS.get(source_name, source_name),
                "source_result_id": result.id,
                "status": result.status,
                "mock": bool(payload.get("mock") or result.status == "mock" or source_name == "mock_travel"),
                "label": payload.get("label") or payload.get("title") or payload.get("display_name") or payload.get("hotel_name") or payload.get("itinerary_summary") or "",
                "total_price": total_price,
                "currency": currency,
                "link_type": link_type,
                "link_label": link_label,
                "link_url": display_url,
            }
        )
    return rows


def source_result_display_by_id(results: list[SourceResult]) -> dict[int, list[dict]]:
    return {result.id: source_result_display_rows(result) for result in results if result.id is not None}


def component_summary_by_deal_id(deals: list[DealCandidate]) -> dict[int, list[dict]]:
    return {deal.id: component_summary_for_deal(deal) for deal in deals if deal.id is not None}


def form_manifest(
    slug: str | None,
    title: str,
    status: str,
    number_of_travelers: int,
    travelers_json: str,
    origin: str,
    destination: str,
    date_mode: str,
    start_date: str | None,
    end_date: str | None,
    nights_min: str | None,
    nights_target: str | None,
    nights_max: str | None,
    hotel_needed: bool,
    airfare_needed: bool,
    rental_car_needed: bool,
    special_accommodations: str,
) -> dict:
    return {
        "slug": slug,
        "title": title,
        "status": status,
        "number_of_travelers": number_of_travelers,
        "travelers": json.loads(travelers_json or "[]"),
        "origin": origin,
        "destination": destination,
        "date_mode": date_mode,
        "start_date": start_date,
        "end_date": end_date,
        "nights_min": nights_min,
        "nights_target": nights_target,
        "nights_max": nights_max,
        "hotel_needed": hotel_needed,
        "airfare_needed": airfare_needed,
        "rental_car_needed": rental_car_needed,
        "special_accommodations": special_accommodations,
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    vacations = session.exec(select(Vacation).order_by(Vacation.created_at.desc())).all()
    return templates.TemplateResponse(request, "dashboard.html", {"vacations": vacations})


@router.get("/api/locations/suggest")
def location_suggestions(q: str = "", provider: str | None = None, limit: int | None = None) -> JSONResponse:
    return JSONResponse(suggest_locations_response(q, provider=provider, limit=limit))


@router.get("/vacations/new", response_class=HTMLResponse)
def new_vacation(request: Request):
    return templates.TemplateResponse(
        request,
        "vacation_form.html",
        {"vacation": None, "action": "/vacations/new", "error": None},
    )


@router.post("/vacations/new")
def create_vacation(
    request: Request,
    title: str = Form(...),
    status: str = Form("active"),
    number_of_travelers: int = Form(...),
    travelers_json: str = Form("[]"),
    origin: str = Form(...),
    destination: str = Form(...),
    date_mode: str = Form(...),
    start_date: str | None = Form(None),
    end_date: str | None = Form(None),
    nights_min: str | None = Form(None),
    nights_target: str | None = Form(None),
    nights_max: str | None = Form(None),
    hotel_needed: bool = Form(False),
    airfare_needed: bool = Form(False),
    rental_car_needed: bool = Form(False),
    special_accommodations: str = Form(""),
    session: Session = Depends(get_session),
):
    try:
        vacation = vacation_from_manifest(
            session,
            form_manifest(
                None,
                title,
                status,
                number_of_travelers,
                travelers_json,
                origin,
                destination,
                date_mode,
                start_date,
                end_date,
                nights_min,
                nights_target,
                nights_max,
                hotel_needed,
                airfare_needed,
                rental_car_needed,
                special_accommodations,
            ),
        )
    except (ManifestValidationError, json.JSONDecodeError) as exc:
        return templates.TemplateResponse(
            request,
            "vacation_form.html",
            {"vacation": None, "action": "/vacations/new", "error": str(exc)},
            status_code=400,
        )
    return redirect(f"/vacations/{vacation.id}")


@router.get("/vacations/{vacation_id}", response_class=HTMLResponse)
def vacation_detail(vacation_id: int, request: Request, session: Session = Depends(get_session)):
    vacation = session.get(Vacation, vacation_id)
    if vacation is None:
        return HTMLResponse("Vacation not found", status_code=404)
    manifest = manifest_for_vacation(vacation)
    recent_runs = session.exec(
        select(SearchRun)
        .where(SearchRun.vacation_id == vacation_id)
        .order_by(SearchRun.created_at.desc())
        .limit(5)
    ).all()
    best_deal = best_deal_for_vacation(session, vacation_id)
    latest_deals = deal_candidates_for_vacation(session, vacation_id)[:5]
    history = vacation_price_history(session, vacation_id)
    ohlc_data = history["ohlc"]
    history_points = svg_ohlc_candles(ohlc_data) if ohlc_data else ""
    return templates.TemplateResponse(
        request,
        "vacation_detail.html",
        {
            "best_deal": best_deal,
            "best_deal_components": component_summary_for_deal(best_deal),
            "deal_components_by_id": component_summary_by_deal_id(latest_deals),
            "history_points": history_points,
            "latest_deals": latest_deals,
            "vacation": vacation,
            "manifest": json.dumps(manifest, indent=2),
            "recent_runs": recent_runs,
        },
    )


@router.post("/vacations/{vacation_id}/search-runs")
def create_search_run(vacation_id: int, session: Session = Depends(get_session)):
    vacation = session.get(Vacation, vacation_id)
    if vacation is None:
        return HTMLResponse("Vacation not found", status_code=404)
    run_search_once(
        vacation_id,
        "manual",
        use_real_sources=True,
        use_mock=False,
        session=session,
    )
    return redirect(f"/vacations/{vacation_id}")


@router.get("/vacations/{vacation_id}/runs", response_class=HTMLResponse)
def vacation_search_runs(vacation_id: int, request: Request, session: Session = Depends(get_session)):
    vacation = session.get(Vacation, vacation_id)
    if vacation is None:
        return HTMLResponse("Vacation not found", status_code=404)
    runs = session.exec(
        select(SearchRun)
        .where(SearchRun.vacation_id == vacation_id)
        .order_by(SearchRun.created_at.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "search_run_list.html",
        {"vacation": vacation, "runs": runs},
    )


@router.get("/search-runs/{search_run_id}", response_class=HTMLResponse)
def search_run_detail(search_run_id: int, request: Request, session: Session = Depends(get_session)):
    search_run = session.get(SearchRun, search_run_id)
    if search_run is None:
        return HTMLResponse("Search run not found", status_code=404)
    vacation = session.get(Vacation, search_run.vacation_id)
    source_results = source_results_for_run(session, search_run_id)
    price_snapshots = session.exec(
        select(PriceSnapshot)
        .where(PriceSnapshot.search_run_id == search_run_id)
        .order_by(PriceSnapshot.created_at.asc(), PriceSnapshot.id.asc())
    ).all()
    deal_candidates = session.exec(
        select(DealCandidate)
        .where(DealCandidate.search_run_id == search_run_id)
        .order_by(DealCandidate.score.asc(), DealCandidate.total_price.asc(), DealCandidate.id.asc())
    ).all()
    return templates.TemplateResponse(
        request,
        "search_run_detail.html",
        {
            "deal_candidates": deal_candidates,
            "price_snapshots": price_snapshots,
            "search_run": search_run,
            "vacation": vacation,
            "source_results": source_results,
            "source_result_display_by_id": source_result_display_by_id(source_results),
            "search_plan": debug_json(search_run.search_plan_json, {}),
            "summary": debug_json(search_run.summary_json, {}),
        },
    )


@router.get("/vacations/{vacation_id}/deals", response_class=HTMLResponse)
def vacation_deals(vacation_id: int, request: Request, session: Session = Depends(get_session)):
    vacation = session.get(Vacation, vacation_id)
    if vacation is None:
        return HTMLResponse("Vacation not found", status_code=404)
    deals = deal_candidates_for_vacation(session, vacation_id)
    return templates.TemplateResponse(
        request,
        "deal_list.html",
        {
            "vacation": vacation,
            "deals": deals,
            "deal_components_by_id": component_summary_by_deal_id(deals),
        },
    )


@router.get("/deals/{deal_candidate_id}", response_class=HTMLResponse)
def deal_detail(deal_candidate_id: int, request: Request, session: Session = Depends(get_session)):
    deal = session.get(DealCandidate, deal_candidate_id)
    if deal is None:
        return HTMLResponse("Deal candidate not found", status_code=404)
    vacation = session.get(Vacation, deal.vacation_id)
    return templates.TemplateResponse(
        request,
        "deal_detail.html",
        {
            "deal": deal,
            "components": component_summary_for_deal(deal),
            "vacation": vacation,
            "component_snapshot_ids": debug_json(deal.component_snapshot_ids_json, []),
            "source_links": debug_json(deal.source_links_json, []),
            "score_breakdown": debug_json(deal.score_breakdown_json, {}),
            "normalized_result": debug_json(deal.normalized_result_json, {}),
        },
    )


@router.get("/vacations/{vacation_id}/price-history", response_class=HTMLResponse)
def price_history_page(
    vacation_id: int,
    request: Request,
    session: Session = Depends(get_session),
    include_mock: int = 0,
    component: str | None = None,
):
    vacation = session.get(Vacation, vacation_id)
    if vacation is None:
        return HTMLResponse("Vacation not found", status_code=404)
    
    # Parse query params
    show_mock = bool(include_mock)
    comp_filter = component if component and component != "all" else None
    
    history = vacation_price_history(session, vacation_id, include_mock=show_mock, component_type=comp_filter)
    
    # Use OHLC data for the chart
    ohlc_data = history["ohlc"]
    history_points = svg_ohlc_candles(ohlc_data) if ohlc_data else ""
    
    # Get component counts for tabs
    all_snapshots = vacation_price_history(session, vacation_id, include_mock=True)["snapshots"]
    component_counts = {"flight": 0, "hotel": 0, "rental_car": 0, "package": 0}
    for snap in all_snapshots:
        qt = snap.get("quote_type", "")
        if qt in component_counts:
            component_counts[qt] += 1
    
    return templates.TemplateResponse(
        request,
        "price_history.html",
        {
            "vacation": vacation,
            "history": history,
            "history_points": history_points,
            "include_mock": show_mock,
            "component_filter": comp_filter or "all",
            "component_counts": component_counts,
            "vacation_airfare_needed": vacation.airfare_needed,
            "vacation_hotel_needed": vacation.hotel_needed,
            "vacation_rental_car_needed": vacation.rental_car_needed,
        },
    )


@router.get("/vacations/{vacation_id}/edit", response_class=HTMLResponse)
def edit_vacation(vacation_id: int, request: Request, session: Session = Depends(get_session)):
    vacation = session.get(Vacation, vacation_id)
    if vacation is None:
        return HTMLResponse("Vacation not found", status_code=404)
    return templates.TemplateResponse(
        request,
        "vacation_form.html",
        {"vacation": vacation, "action": f"/vacations/{vacation.id}/edit", "error": None},
    )


@router.post("/vacations/{vacation_id}/edit")
def update_vacation(
    vacation_id: int,
    request: Request,
    title: str = Form(...),
    status: str = Form("active"),
    number_of_travelers: int = Form(...),
    travelers_json: str = Form("[]"),
    origin: str = Form(...),
    destination: str = Form(...),
    date_mode: str = Form(...),
    start_date: str | None = Form(None),
    end_date: str | None = Form(None),
    nights_min: str | None = Form(None),
    nights_target: str | None = Form(None),
    nights_max: str | None = Form(None),
    hotel_needed: bool = Form(False),
    airfare_needed: bool = Form(False),
    rental_car_needed: bool = Form(False),
    special_accommodations: str = Form(""),
    session: Session = Depends(get_session),
):
    vacation = session.get(Vacation, vacation_id)
    if vacation is None:
        return HTMLResponse("Vacation not found", status_code=404)
    try:
        update_vacation_from_manifest(
            session,
            vacation,
            form_manifest(
                vacation.slug,
                title,
                status,
                number_of_travelers,
                travelers_json,
                origin,
                destination,
                date_mode,
                start_date,
                end_date,
                nights_min,
                nights_target,
                nights_max,
                hotel_needed,
                airfare_needed,
                rental_car_needed,
                special_accommodations,
            ),
        )
    except (ManifestValidationError, json.JSONDecodeError) as exc:
        return templates.TemplateResponse(
            request,
            "vacation_form.html",
            {
                "vacation": vacation,
                "action": f"/vacations/{vacation.id}/edit",
                "error": str(exc),
            },
            status_code=400,
        )
    return redirect(f"/vacations/{vacation.id}")


@router.post("/vacations/{vacation_id}/delete")
def delete_vacation(vacation_id: int, session: Session = Depends(get_session)):
    vacation = session.get(Vacation, vacation_id)
    if vacation is None:
        return HTMLResponse("Vacation not found", status_code=404)
    session.delete(vacation)
    session.commit()
    return redirect("/")


@router.get("/vacations/{vacation_id}/export")
def export_vacation(vacation_id: int, session: Session = Depends(get_session)):
    vacation = session.get(Vacation, vacation_id)
    if vacation is None:
        return JSONResponse({"error": "Vacation not found"}, status_code=404)
    return JSONResponse(manifest_for_vacation(vacation))


@router.get("/import", response_class=HTMLResponse)
def import_form(request: Request):
    return templates.TemplateResponse(request, "import_form.html", {"error": None})


@router.post("/import")
def import_manifest(
    request: Request,
    manifest_json: str = Form(...),
    session: Session = Depends(get_session),
):
    try:
        raw_manifest = json.loads(manifest_json)
        if not isinstance(raw_manifest, dict):
            raise ManifestValidationError("Manifest must be a JSON object")
        vacation = vacation_from_manifest(session, raw_manifest)
    except (json.JSONDecodeError, ManifestValidationError) as exc:
        return templates.TemplateResponse(
            request,
            "import_form.html",
            {"error": str(exc)},
            status_code=400,
        )
    return redirect(f"/vacations/{vacation.id}")


@router.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})
