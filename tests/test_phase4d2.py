"""Phase 4D-2 tests: mock filtering, cleanup scripts, candlestick chart corrections."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlmodel import Session, select

# Ensure project root is on path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.models import DealCandidate, PriceSnapshot, Vacation
from app.db.session import get_engine, init_db
from app.services.price_history import (
    aggregate_daily_ohlc,
    svg_ohlc_candles,
    vacation_price_history,
)
from app.services.search_runner import best_deal_for_vacation, deal_candidates_for_vacation


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_session(tmp_path):
    """Create an in-memory-like SQLite DB for testing."""
    db_file = tmp_path / "test_vacation_deals.sqlite3"
    engine_url = f"sqlite:///{db_file}"

    with patch("app.db.session.database_url", return_value=engine_url):
        from app.db.session import get_engine as _get_engine, init_db as _init_db

        # Reset the global engine so it picks up our new URL
        import app.db.session as session_mod

        session_mod._engine = None
        session_mod._engine_url = None

        _init_db()
        with Session(_get_engine()) as s:
            yield s


@pytest.fixture()
def vacation(db_session):
    """Create a test vacation."""
    v = Vacation(
        slug="test-vacation-4d2",
        title="Test Vacation 4D-2",
        status="active",
        number_of_travelers=2,
        travelers_json=json.dumps([]),
        origin="PIT",
        destination="MIA",
        date_mode="range",
        start_date=datetime(2026, 7, 1).date(),
        end_date=datetime(2026, 7, 7).date(),
        hotel_needed=True,
        airfare_needed=True,
        rental_car_needed=False,
        manifest_json=json.dumps({}),
    )
    db_session.add(v)
    db_session.commit()
    db_session.refresh(v)
    return v


# ---------------------------------------------------------------------------
# A. Best deal filtering tests
# ---------------------------------------------------------------------------

class TestBestDealFiltering:
    """Tests for mock exclusion in best_deal_for_vacation and deal_candidates_for_vacation."""

    def test_mock_best_deal_excluded_by_default(self, db_session, vacation):
        """A mock candidate with lower price/score does NOT win over a real candidate."""
        # Create a real (non-mock) deal candidate with higher score (worse = higher score in this system)
        real_deal = DealCandidate(
            vacation_id=vacation.id,
            search_run_id=1,
            candidate_type="package",
            title="Real Air Flight Pittsburgh to Miami",
            status="valid",
            total_price=450.0,
            currency="USD",
            score=80.0,
            score_breakdown_json=json.dumps({"quality": 80}),
            component_snapshot_ids_json=json.dumps([]),
            source_links_json=json.dumps([{"source": "trvl"}]),
            normalized_result_json=json.dumps({}),
        )

        # Create a mock deal candidate with better (lower) score — should NOT be selected
        mock_deal = DealCandidate(
            vacation_id=vacation.id,
            search_run_id=1,
            candidate_type="package",
            title="Mock Air Flight Pittsburgh, PA to Miami, FL",
            status="valid",
            total_price=200.0,
            currency="USD",
            score=50.0,  # Better score but is_mock=1
            score_breakdown_json=json.dumps({"quality": 50}),
            component_snapshot_ids_json=json.dumps([]),
            source_links_json=json.dumps([{"source": "mock_travel"}]),
            normalized_result_json=json.dumps({"mock": True}),
            is_mock=True,
        )

        db_session.add(real_deal)
        db_session.add(mock_deal)
        db_session.commit()

        # Default call (include_mock=False) should return the real deal
        best = best_deal_for_vacation(db_session, vacation.id)
        assert best is not None
        assert best.is_mock is False
        assert "Mock" not in best.title

    def test_best_deal_returns_none_when_only_mock_exists(self, db_session, vacation):
        """If only mock candidates exist, normal best_deal should be None."""
        mock_deal = DealCandidate(
            vacation_id=vacation.id,
            search_run_id=1,
            candidate_type="package",
            title="Mock Air Flight Pittsburgh to Miami",
            status="valid",
            total_price=200.0,
            currency="USD",
            score=50.0,
            score_breakdown_json=json.dumps({}),
            component_snapshot_ids_json=json.dumps([]),
            source_links_json=json.dumps([]),
            normalized_result_json=json.dumps({"mock": True}),
            is_mock=True,
        )
        db_session.add(mock_deal)
        db_session.commit()

        # Default call should return None (no real candidates)
        best = best_deal_for_vacation(db_session, vacation.id)
        assert best is None

    def test_include_mock_true_returns_mock(self, db_session, vacation):
        """When include_mock=True, mock candidates are included."""
        mock_deal = DealCandidate(
            vacation_id=vacation.id,
            search_run_id=1,
            candidate_type="package",
            title="Mock Air Flight Pittsburgh to Miami",
            status="valid",
            total_price=200.0,
            currency="USD",
            score=50.0,
            score_breakdown_json=json.dumps({}),
            component_snapshot_ids_json=json.dumps([]),
            source_links_json=json.dumps([]),
            normalized_result_json=json.dumps({"mock": True}),
            is_mock=True,
        )
        db_session.add(mock_deal)
        db_session.commit()

        best = best_deal_for_vacation(db_session, vacation.id, include_mock=True)
        assert best is not None
        assert best.is_mock is True

    def test_deal_candidates_excludes_mock_by_default(self, db_session, vacation):
        """deal_candidates_for_vacation excludes mock by default."""
        real_deal = DealCandidate(
            vacation_id=vacation.id, search_run_id=1, candidate_type="package",
            title="Real Deal", status="valid", total_price=450.0, currency="USD",
            score=80.0, score_breakdown_json=json.dumps({}),
            component_snapshot_ids_json=json.dumps([]), source_links_json=json.dumps([]),
            normalized_result_json=json.dumps({}),
        )
        mock_deal = DealCandidate(
            vacation_id=vacation.id, search_run_id=1, candidate_type="package",
            title="Mock Deal", status="valid", total_price=200.0, currency="USD",
            score=50.0, score_breakdown_json=json.dumps({}),
            component_snapshot_ids_json=json.dumps([]), source_links_json=json.dumps([]),
            normalized_result_json=json.dumps({"mock": True}), is_mock=True,
        )
        db_session.add_all([real_deal, mock_deal])
        db_session.commit()

        candidates = deal_candidates_for_vacation(db_session, vacation.id)
        assert len(candidates) == 1
        assert candidates[0].is_mock is False


# ---------------------------------------------------------------------------
# B. delete_mock_data.py tests
# ---------------------------------------------------------------------------

class TestDeleteMockData:
    """Tests for the mock data deletion utility."""

    def test_dry_run_does_not_delete(self, db_session, vacation, tmp_path):
        """Dry-run mode reports counts but does not delete rows."""
        mock_snapshot = PriceSnapshot(
            vacation_id=vacation.id, search_run_id=1, quote_type="flight",
            source_name="mock_travel", label="Mock Flight", total_price=200.0,
            currency="USD", is_mock=True,
        )
        mock_candidate = DealCandidate(
            vacation_id=vacation.id, search_run_id=1, candidate_type="package",
            title="Mock Air Flight Pittsburgh to Miami", status="valid",
            total_price=200.0, currency="USD", score=50.0,
            score_breakdown_json=json.dumps({}), component_snapshot_ids_json=json.dumps([]),
            source_links_json=json.dumps([{"source": "mock_travel"}]),
            normalized_result_json=json.dumps({"mock": True}), is_mock=True,
        )
        db_session.add_all([mock_snapshot, mock_candidate])
        db_session.commit()

        # Set env var so scripts use the test DB
        env_patch = patch.dict(os.environ, {"VACATION_DEAL_DB_URL": f"sqlite:///{tmp_path}/test_vacation_deals.sqlite3"})
        env_patch.start()
        try:
            from scripts.delete_mock_data import main as delete_main
            with patch("sys.argv", ["delete_mock_data.py"]):
                delete_main()
        finally:
            env_patch.stop()

        # Verify rows still exist
        remaining_snapshots = db_session.exec(
            select(PriceSnapshot).where(PriceSnapshot.is_mock == True)
        ).all()
        assert len(remaining_snapshots) >= 1

    def test_execute_deletes_mock_snapshots_and_candidates(self, db_session, vacation, tmp_path):
        """--execute deletes mock snapshots and candidates."""
        mock_snapshot = PriceSnapshot(
            vacation_id=vacation.id, search_run_id=1, quote_type="flight",
            source_name="mock_travel", label="Mock Flight", total_price=200.0,
            currency="USD", is_mock=True,
        )
        mock_candidate = DealCandidate(
            vacation_id=vacation.id, search_run_id=1, candidate_type="package",
            title="Mock Air Flight Pittsburgh to Miami", status="valid",
            total_price=200.0, currency="USD", score=50.0,
            score_breakdown_json=json.dumps({}), component_snapshot_ids_json=json.dumps([]),
            source_links_json=json.dumps([{"source": "mock_travel"}]),
            normalized_result_json=json.dumps({"mock": True}), is_mock=True,
        )
        db_session.add_all([mock_snapshot, mock_candidate])
        db_session.commit()

        env_patch = patch.dict(os.environ, {"VACATION_DEAL_DB_URL": f"sqlite:///{tmp_path}/test_vacation_deals.sqlite3"})
        env_patch.start()
        try:
            from scripts.delete_mock_data import main as delete_main
            with patch("sys.argv", ["delete_mock_data.py", "--execute", "--no-backup"]):
                delete_main()
        finally:
            env_patch.stop()

        remaining_snapshots = db_session.exec(
            select(PriceSnapshot).where(PriceSnapshot.is_mock == True)
        ).all()
        remaining_candidates = db_session.exec(
            select(DealCandidate).where(DealCandidate.is_mock == True)
        ).all()
        assert len(remaining_snapshots) == 0
        assert len(remaining_candidates) == 0

    def test_preserves_real_trvl_rows(self, db_session, vacation, tmp_path):
        """Mock delete script preserves real trvl rows."""
        # Create a real (non-mock) deal candidate from trvl source
        real_candidate = DealCandidate(
            vacation_id=vacation.id, search_run_id=1, candidate_type="package",
            title="Real Air Flight Pittsburgh to Miami via United", status="valid",
            total_price=450.0, currency="USD", score=80.0,
            score_breakdown_json=json.dumps({"quality": 80}),
            component_snapshot_ids_json=json.dumps([]),
            source_links_json=json.dumps([{"source": "trvl"}]),
            normalized_result_json=json.dumps({}),
        )

        # Create a mock deal candidate to delete
        mock_candidate = DealCandidate(
            vacation_id=vacation.id, search_run_id=1, candidate_type="package",
            title="Mock Air Flight Pittsburgh to Miami", status="valid",
            total_price=200.0, currency="USD", score=50.0,
            score_breakdown_json=json.dumps({}), component_snapshot_ids_json=json.dumps([]),
            source_links_json=json.dumps([{"source": "mock_travel"}]),
            normalized_result_json=json.dumps({"mock": True}), is_mock=True,
        )
        db_session.add_all([real_candidate, mock_candidate])
        db_session.commit()

        real_id = real_candidate.id

        env_patch = patch.dict(os.environ, {"VACATION_DEAL_DB_URL": f"sqlite:///{tmp_path}/test_vacation_deals.sqlite3"})
        env_patch.start()
        try:
            from scripts.delete_mock_data import main as delete_main
            with patch("sys.argv", ["delete_mock_data.py", "--execute", "--no-backup"]):
                delete_main()
        finally:
            env_patch.stop()

        # Real candidate should still exist
        remaining = db_session.exec(
            select(DealCandidate).where(DealCandidate.id == real_id)
        ).first()
        assert remaining is not None
        assert "Mock" not in remaining.title


# ---------------------------------------------------------------------------
# C. delete_non_usd_history.py tests
# ---------------------------------------------------------------------------

class TestDeleteNonUsdHistory:
    """Tests for the non-USD historical data deletion utility."""

    def test_dry_run_does_not_delete(self, db_session, vacation, tmp_path):
        """Dry-run mode reports counts but does not delete rows."""
        eur_snapshot = PriceSnapshot(
            vacation_id=vacation.id, search_run_id=1, quote_type="flight",
            source_name="trvl", label="EUR Flight", total_price=350.0,
            currency="EUR", is_mock=False,
        )
        db_session.add(eur_snapshot)
        db_session.commit()

        env_patch = patch.dict(os.environ, {"VACATION_DEAL_DB_URL": f"sqlite:///{tmp_path}/test_vacation_deals.sqlite3"})
        env_patch.start()
        try:
            from scripts.delete_non_usd_history import main as delete_main
            with patch("sys.argv", ["delete_non_usd_history.py", "--vacation-id", str(vacation.id)]):
                delete_main()
        finally:
            env_patch.stop()

        # Verify rows still exist
        remaining = db_session.exec(
            select(PriceSnapshot).where(
                PriceSnapshot.vacation_id == vacation.id,
                PriceSnapshot.currency == "EUR",
            )
        ).all()
        assert len(remaining) >= 1

    def test_execute_deletes_non_usd(self, db_session, vacation, tmp_path):
        """--execute deletes non-USD price_snapshot and deal_candidate rows."""
        eur_snapshot = PriceSnapshot(
            vacation_id=vacation.id, search_run_id=1, quote_type="flight",
            source_name="trvl", label="EUR Flight", total_price=350.0,
            currency="EUR", is_mock=False,
        )
        eur_candidate = DealCandidate(
            vacation_id=vacation.id, search_run_id=1, candidate_type="package",
            title="EUR Air Flight", status="valid", total_price=350.0,
            currency="EUR", score=60.0, score_breakdown_json=json.dumps({}),
            component_snapshot_ids_json=json.dumps([]), source_links_json=json.dumps([]),
            normalized_result_json=json.dumps({}),
        )
        db_session.add_all([eur_snapshot, eur_candidate])
        db_session.commit()

        env_patch = patch.dict(os.environ, {"VACATION_DEAL_DB_URL": f"sqlite:///{tmp_path}/test_vacation_deals.sqlite3"})
        env_patch.start()
        try:
            from scripts.delete_non_usd_history import main as delete_main
            with patch("sys.argv", ["delete_non_usd_history.py", "--vacation-id", str(vacation.id), "--execute", "--no-backup"]):
                delete_main()
        finally:
            env_patch.stop()

        remaining_snapshots = db_session.exec(
            select(PriceSnapshot).where(
                PriceSnapshot.vacation_id == vacation.id,
                PriceSnapshot.currency != "USD",
            )
        ).all()
        assert len(remaining_snapshots) == 0


# ---------------------------------------------------------------------------
# D. Candlestick chart tests
# ---------------------------------------------------------------------------

class TestCandlestickChart:
    """Tests for true candlestick chart rendering and OHLC aggregation."""

    def test_ohlc_uses_daily_aggregation_not_raw_rows(self):
        """OHLC data uses daily aggregation, not raw lookup rows."""
        base_date = datetime(2026, 6, 1, tzinfo=timezone.utc)

        # Multiple prices on the same day should be aggregated into one OHLC record
        history_rows = [
            {
                "timestamp": base_date + timedelta(hours=i),
                "total_price": 100.0 + i * 5,
                "currency": "USD",
                "quote_type": "flight",
                "is_mock": False,
            }
            for i in range(4)  # 4 prices on the same day
        ]

        ohlc = aggregate_daily_ohlc(history_rows, exclude_mock=True)

        # All 4 rows should be aggregated into exactly 1 daily OHLC record
        assert len(ohlc) == 1
        candle = ohlc[0]

        # Verify OHLC values
        prices = [r["total_price"] for r in history_rows]
        assert candle["open"] == prices[0]   # First price of the day
        assert candle["high"] == max(prices)  # Max price
        assert candle["low"] == min(prices)   # Min price
        assert candle["close"] == prices[-1]  # Last price of the day
        assert candle["count"] == 4           # All 4 rows counted

    def test_ohlc_multiple_days(self):
        """OHLC correctly handles multiple days."""
        base_date = datetime(2026, 6, 1, tzinfo=timezone.utc)

        history_rows = [
            {"timestamp": base_date, "total_price": 100.0, "currency": "USD", "quote_type": "flight", "is_mock": False},
            {"timestamp": base_date + timedelta(days=1), "total_price": 120.0, "currency": "USD", "quote_type": "flight", "is_mock": False},
            {"timestamp": base_date + timedelta(days=2), "total_price": 95.0, "currency": "USD", "quote_type": "flight", "is_mock": False},
        ]

        ohlc = aggregate_daily_ohlc(history_rows, exclude_mock=True)
        assert len(ohlc) == 3

    def test_usd_preferred_when_mixed_currencies(self):
        """USD is preferred when mixed currencies exist in OHLC data."""
        base_date = datetime(2026, 6, 1, tzinfo=timezone.utc)

        # USD and EUR rows on the same days
        history_rows = [
            {"timestamp": base_date, "total_price": 100.0, "currency": "USD", "quote_type": "flight", "is_mock": False},
            {"timestamp": base_date + timedelta(days=1), "total_price": 120.0, "currency": "USD", "quote_type": "flight", "is_mock": False},
            {"timestamp": base_date, "total_price": 90.0, "currency": "EUR", "quote_type": "flight", "is_mock": False},
            {"timestamp": base_date + timedelta(days=1), "total_price": 110.0, "currency": "EUR", "quote_type": "flight", "is_mock": False},
        ]

        ohlc = aggregate_daily_ohlc(history_rows, exclude_mock=True)
        svg = svg_ohlc_candles(ohlc)

        # USD should be preferred; EUR excluded
        assert "Excluded non-USD currencies" in svg
        # Only USD candles (2 days)
        assert 'class="candle-up"' in svg or 'class="candle-down"' in svg or 'class="candle-flat"' in svg

    def test_svg_contains_candle_body_and_wick_elements(self):
        """Chart template contains rect body and line wick elements with candle classes."""
        base_date = datetime(2026, 6, 1, tzinfo=timezone.utc)

        history_rows = [
            {"timestamp": base_date, "total_price": 100.0, "currency": "USD", "quote_type": "flight", "is_mock": False},
            {"timestamp": base_date + timedelta(hours=6), "total_price": 120.0, "currency": "USD", "quote_type": "flight", "is_mock": False},
        ]

        ohlc = aggregate_daily_ohlc(history_rows, exclude_mock=True)
        svg = svg_ohlc_candles(ohlc)

        # Must contain rect elements (candle bodies)
        assert '<rect' in svg
        assert 'class="candle-up"' in svg or 'class="candle-down"' in svg or 'class="candle-flat"' in svg

        # Must contain line elements with wick class
        assert 'class="wick"' in svg

    def test_svg_contains_axis_labels(self):
        """Chart has Date (X) and Price (Y) axis labels."""
        base_date = datetime(2026, 6, 1, tzinfo=timezone.utc)

        history_rows = [
            {"timestamp": base_date, "total_price": 100.0, "currency": "USD", "quote_type": "flight", "is_mock": False},
        ]

        ohlc = aggregate_daily_ohlc(history_rows, exclude_mock=True)
        svg = svg_ohlc_candles(ohlc)

        assert ">Date</text>" in svg
        assert ">Price</text>" in svg

    def test_svg_contains_grid_lines(self):
        """Chart has faint horizontal grid lines."""
        base_date = datetime(2026, 6, 1, tzinfo=timezone.utc)

        history_rows = [
            {"timestamp": base_date, "total_price": 100.0, "currency": "USD", "quote_type": "flight", "is_mock": False},
            {"timestamp": base_date + timedelta(hours=6), "total_price": 200.0, "currency": "USD", "quote_type": "flight", "is_mock": False},
        ]

        ohlc = aggregate_daily_ohlc(history_rows, exclude_mock=True)
        svg = svg_ohlc_candles(ohlc)

        # Grid lines with faint color
        assert 'stroke="#e0e0e0"' in svg

    def test_svg_contains_date_ticks(self):
        """X-axis has date ticks visible."""
        base_date = datetime(2026, 6, 1, tzinfo=timezone.utc)

        history_rows = [
            {"timestamp": base_date, "total_price": 100.0, "currency": "USD", "quote_type": "flight", "is_mock": False},
            {"timestamp": base_date + timedelta(days=5), "total_price": 120.0, "currency": "USD", "quote_type": "flight", "is_mock": False},
        ]

        ohlc = aggregate_daily_ohlc(history_rows, exclude_mock=True)
        svg = svg_ohlc_candles(ohlc)

        # Date labels in MM-DD format should be present
        assert "06-01" in svg
        assert "06-06" in svg

    def test_flat_candle_class(self):
        """When open == close, candle-flat class is used."""
        base_date = datetime(2026, 6, 1, tzinfo=timezone.utc)

        # Same price at start and end of day (open == close after aggregation)
        history_rows = [
            {"timestamp": base_date, "total_price": 100.0, "currency": "USD", "quote_type": "flight", "is_mock": False},
            {"timestamp": base_date + timedelta(hours=6), "total_price": 100.0, "currency": "USD", "quote_type": "flight", "is_mock": False},
        ]

        ohlc = aggregate_daily_ohlc(history_rows, exclude_mock=True)
        svg = svg_ohlc_candles(ohlc)

        assert 'class="candle-flat"' in svg


# ---------------------------------------------------------------------------
# E. Price history defaults tests
# ---------------------------------------------------------------------------

class TestPriceHistoryDefaults:
    """Tests for price history default behavior."""

    def test_vacation_price_history_excludes_mock_by_default(self, db_session, vacation):
        """Default price history excludes mock rows."""
        real_snapshot = PriceSnapshot(
            vacation_id=vacation.id, search_run_id=1, quote_type="flight",
            source_name="trvl", label="Real Flight", total_price=450.0,
            currency="USD", is_mock=False,
        )
        mock_snapshot = PriceSnapshot(
            vacation_id=vacation.id, search_run_id=1, quote_type="flight",
            source_name="mock_travel", label="Mock Flight", total_price=200.0,
            currency="USD", is_mock=True,
        )
        db_session.add_all([real_snapshot, mock_snapshot])
        db_session.commit()

        history = vacation_price_history(db_session, vacation.id)

        # Snapshots should only include non-mock by default
        assert len(history["snapshots"]) == 1
        assert history["snapshots"][0]["is_mock"] is False

    def test_vacation_price_history_includes_mock_when_requested(self, db_session, vacation):
        """include_mock=True includes mock rows."""
        mock_snapshot = PriceSnapshot(
            vacation_id=vacation.id, search_run_id=1, quote_type="flight",
            source_name="mock_travel", label="Mock Flight", total_price=200.0,
            currency="USD", is_mock=True,
        )
        db_session.add(mock_snapshot)
        db_session.commit()

        history = vacation_price_history(db_session, vacation.id, include_mock=True)
        assert len(history["snapshots"]) == 1
        assert history["snapshots"][0]["is_mock"] is True


# ---------------------------------------------------------------------------
# F. Integration: routes use candlestick chart
# ---------------------------------------------------------------------------

class TestRoutesIntegration:
    """Tests that routes correctly use the updated visualization."""

    def test_vacation_detail_uses_candlestick_chart(self, db_session, vacation, tmp_path):
        """vacation_detail endpoint uses svg_ohlc_candles (candlestick) not line chart."""
        # Create some real snapshots to generate OHLC data
        base_date = datetime(2026, 6, 1, tzinfo=timezone.utc)
        for i in range(3):
            snap = PriceSnapshot(
                vacation_id=vacation.id, search_run_id=1, quote_type="flight",
                source_name="trvl", label=f"Flight {i}", total_price=400.0 + i * 20,
                currency="USD", is_mock=False, captured_at=base_date + timedelta(days=i),
            )
            db_session.add(snap)
        db_session.commit()

        from app.web.routes import vacation_detail

        response = vacation_detail(vacation.id, request=None, session=db_session)

        assert response.status_code == 200
        text = response.body.decode()
        assert '<rect' in text or 'class="candle-' in text, "Expected candlestick chart elements"


# ---------------------------------------------------------------------------
# G. Existing tests still pass (sanity check)
# ---------------------------------------------------------------------------

class TestExistingFunctionality:
    """Sanity checks that existing functionality is not broken."""

    def test_health_endpoint(self):
        """Health endpoint returns ok."""
        from app.web.routes import health

        response = health()
        assert response.status_code == 200
        assert response.body == b'{"status":"ok"}'

    def test_vacation_crud(self, db_session):
        """Vacation CRUD still works."""
        v = Vacation(
            slug="crud-test", title="CRUD Test", status="active",
            number_of_travelers=1, travelers_json=json.dumps([]),
            origin="PIT", destination="LAX", date_mode="range",
            start_date=datetime(2026, 8, 1).date(), end_date=datetime(2026, 8, 5).date(),
            hotel_needed=True, airfare_needed=True, rental_car_needed=False,
            manifest_json=json.dumps({}),
        )
        db_session.add(v)
        db_session.commit()

        loaded = db_session.get(Vacation, v.id)
        assert loaded is not None
        assert loaded.slug == "crud-test"
