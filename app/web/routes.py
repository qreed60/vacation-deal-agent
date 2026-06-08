from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.db.models import DealCandidate, PriceSnapshot, SearchRun, Vacation
from app.db.session import get_session
from app.services.manifest_io import (
    ManifestValidationError,
    manifest_for_vacation,
    update_vacation_from_manifest,
    vacation_from_manifest,
)
from app.services.price_history import svg_line_points, vacation_price_history
from app.services.search_runner import best_deal_for_vacation, deal_candidates_for_vacation, run_search_once, source_results_for_run


router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def component_summary_for_deal(deal: DealCandidate | None) -> list[dict]:
    if deal is None:
        return []
    try:
        normalized = json.loads(deal.normalized_result_json or "{}")
    except json.JSONDecodeError:
        normalized = {}
    components = normalized.get("component_summary") if isinstance(normalized, dict) else []
    if not isinstance(components, list):
        return []
    return [component for component in components if isinstance(component, dict)]


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
    history_rows = history["deals"] or history["snapshots"]
    return templates.TemplateResponse(
        request,
        "vacation_detail.html",
        {
            "best_deal": best_deal,
            "best_deal_components": component_summary_for_deal(best_deal),
            "deal_components_by_id": component_summary_by_deal_id(latest_deals),
            "history_points": svg_line_points(history_rows),
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
    run_search_once(vacation_id, "manual", session=session)
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
            "search_plan": json.dumps(json.loads(search_run.search_plan_json or "{}"), indent=2),
            "summary": json.dumps(json.loads(search_run.summary_json or "{}"), indent=2),
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
            "component_snapshot_ids": json.dumps(json.loads(deal.component_snapshot_ids_json or "[]"), indent=2),
            "source_links": json.dumps(json.loads(deal.source_links_json or "[]"), indent=2),
            "score_breakdown": json.dumps(json.loads(deal.score_breakdown_json or "{}"), indent=2),
            "normalized_result": json.dumps(json.loads(deal.normalized_result_json or "{}"), indent=2),
        },
    )


@router.get("/vacations/{vacation_id}/price-history", response_class=HTMLResponse)
def price_history_page(vacation_id: int, request: Request, session: Session = Depends(get_session)):
    vacation = session.get(Vacation, vacation_id)
    if vacation is None:
        return HTMLResponse("Vacation not found", status_code=404)
    history = vacation_price_history(session, vacation_id)
    rows = history["deals"] or history["snapshots"]
    return templates.TemplateResponse(
        request,
        "price_history.html",
        {
            "vacation": vacation,
            "history": history,
            "history_points": svg_line_points(rows),
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
