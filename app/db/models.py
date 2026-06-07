from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Vacation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True)
    title: str
    status: str = Field(default="active")
    number_of_travelers: int
    travelers_json: str = Field(default="[]")
    origin: str
    destination: str
    date_mode: str
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    nights_min: Optional[int] = None
    nights_target: Optional[int] = None
    nights_max: Optional[int] = None
    hotel_needed: bool = True
    airfare_needed: bool = True
    rental_car_needed: bool = False
    special_accommodations: str = ""
    manifest_json: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
