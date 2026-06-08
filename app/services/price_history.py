from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from app.db.models import DealCandidate, PriceSnapshot


def price_snapshot_history(session: Session, vacation_id: int) -> list[dict[str, Any]]:
    snapshots = session.exec(
        select(PriceSnapshot)
        .where(PriceSnapshot.vacation_id == vacation_id)
        .where(PriceSnapshot.total_price.is_not(None))
        .order_by(PriceSnapshot.captured_at.asc(), PriceSnapshot.id.asc())
    ).all()
    return [
        {
            "timestamp": snapshot.captured_at,
            "quote_type": snapshot.quote_type,
            "total_price": snapshot.total_price,
            "currency": snapshot.currency,
            "source_name": snapshot.source_name,
            "label": snapshot.label,
        }
        for snapshot in snapshots
    ]


def deal_candidate_history(session: Session, vacation_id: int) -> list[dict[str, Any]]:
    candidates = session.exec(
        select(DealCandidate)
        .where(DealCandidate.vacation_id == vacation_id)
        .where(DealCandidate.total_price.is_not(None))
        .order_by(DealCandidate.created_at.asc(), DealCandidate.id.asc())
    ).all()
    return [
        {
            "timestamp": candidate.created_at,
            "quote_type": candidate.candidate_type,
            "total_price": candidate.total_price,
            "currency": candidate.currency,
            "source_name": "deal_candidate",
            "label": candidate.title,
        }
        for candidate in candidates
    ]


def vacation_price_history(session: Session, vacation_id: int) -> dict[str, list[dict[str, Any]]]:
    return {
        "snapshots": price_snapshot_history(session, vacation_id),
        "deals": deal_candidate_history(session, vacation_id),
    }


def svg_line_points(history_rows: list[dict[str, Any]], *, width: int = 640, height: int = 180) -> str:
    priced_rows = [row for row in history_rows if row.get("total_price") is not None]
    if not priced_rows:
        return ""
    prices = [float(row["total_price"]) for row in priced_rows]
    min_price = min(prices)
    max_price = max(prices)
    left = 24
    right = width - 16
    top = 16
    bottom = height - 24
    x_span = max(1, right - left)
    y_span = max(1, bottom - top)
    price_span = max(1.0, max_price - min_price)
    points = []
    for index, price in enumerate(prices):
        x = left + (x_span * index / max(1, len(prices) - 1))
        y = bottom - ((price - min_price) / price_span * y_span)
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)
