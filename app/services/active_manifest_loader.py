from __future__ import annotations

from sqlmodel import Session, select

from app.db.models import Vacation


def load_active_vacations(session: Session) -> list[Vacation]:
    statement = select(Vacation).where(Vacation.status == "active").order_by(Vacation.created_at.asc())
    return list(session.exec(statement).all())
