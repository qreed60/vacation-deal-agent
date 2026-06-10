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
    preferred_airports_json: str = Field(default="[]")
    alternate_airports_json: str = Field(default="[]")
    manifest_json: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SearchRun(SQLModel, table=True):
    __tablename__ = "search_run"

    id: Optional[int] = Field(default=None, primary_key=True)
    vacation_id: int = Field(foreign_key="vacation.id", index=True)
    status: str = Field(default="queued", index=True)
    trigger_type: str
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    search_plan_json: str = Field(default="{}")
    summary_json: str = Field(default="{}")
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SourceResult(SQLModel, table=True):
    __tablename__ = "source_result"

    id: Optional[int] = Field(default=None, primary_key=True)
    search_run_id: int = Field(foreign_key="search_run.id", index=True)
    source_name: str
    result_type: str
    status: str
    query_json: str
    normalized_result_json: str
    raw_result_json: str
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


class PriceSnapshot(SQLModel, table=True):
    __tablename__ = "price_snapshot"

    id: Optional[int] = Field(default=None, primary_key=True)
    vacation_id: int = Field(foreign_key="vacation.id", index=True)
    search_run_id: int = Field(foreign_key="search_run.id", index=True)
    source_result_id: Optional[int] = Field(default=None, foreign_key="source_result.id", index=True)
    quote_type: str = Field(index=True)
    source_name: str
    provider: Optional[str] = None
    label: str
    total_price: Optional[float] = None
    currency: str = Field(default="USD")
    source_url: Optional[str] = None
    normalized_json: str = Field(default="{}")
    captured_at: datetime = Field(default_factory=utc_now)
    created_at: datetime = Field(default_factory=utc_now)
    is_mock: bool = Field(default=False, index=True)


class DealCandidate(SQLModel, table=True):
    __tablename__ = "deal_candidate"

    id: Optional[int] = Field(default=None, primary_key=True)
    vacation_id: int = Field(foreign_key="vacation.id", index=True)
    search_run_id: int = Field(foreign_key="search_run.id", index=True)
    candidate_type: str = Field(index=True)
    title: str
    status: str = Field(index=True)
    total_price: Optional[float] = None
    currency: str = Field(default="USD")
    score: Optional[float] = Field(default=None, index=True)
    score_breakdown_json: str = Field(default="{}")
    component_snapshot_ids_json: str = Field(default="[]")
    source_links_json: str = Field(default="[]")
    normalized_result_json: str = Field(default="{}")
    created_at: datetime = Field(default_factory=utc_now)
    is_mock: bool = Field(default=False, index=True)
