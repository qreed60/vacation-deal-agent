from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.db.models import Vacation
from app.db.session import get_session
from app.services.manifest_io import (
    ManifestValidationError,
    manifest_for_vacation,
    update_vacation_from_manifest,
    vacation_from_manifest,
)


router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


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
    return templates.TemplateResponse(
        request,
        "vacation_detail.html",
        {"vacation": vacation, "manifest": json.dumps(manifest, indent=2)},
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
